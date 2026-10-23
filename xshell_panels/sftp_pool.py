"""Pool kết nối SFTP (asyncssh) theo Target; tự nối lại khi mất kết nối."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import asyncssh

from . import ApiError
from .ssh_parse import Hop, Target, resolve_chain

log = logging.getLogger(__name__)

LOST = tuple(
    c for c in (getattr(asyncssh, n, None)
                for n in ("ConnectionLost", "SFTPConnectionLost", "SFTPNoConnection"))
    if c
) + (BrokenPipeError, ConnectionResetError, asyncssh.ChannelOpenError)

IDLE_CLOSE_SECONDS = 15 * 60

Resolver = Callable[[Target], Awaitable[list[Hop]]]
PasswordProvider = Callable[[Target], Awaitable[Optional[str]]]


class _Entry:
    __slots__ = ("conns", "sftp", "used")

    def __init__(self, conns, sftp):
        self.conns, self.sftp, self.used = conns, sftp, time.monotonic()

    def alive(self):
        return not self.conns[-1].is_closed()

    def close(self):
        for c in reversed(self.conns):
            try:
                c.close()
            except Exception:
                pass


class Pool:
    def __init__(self, resolver: Resolver | None = None,
                 password_provider: PasswordProvider | None = None,
                 strict: bool = True, config_file: str | None = None):
        self._entries: dict[tuple, _Entry] = {}
        self._pw: dict[tuple, str] = {}           # mật khẩu chỉ giữ trong RAM
        self._locks: dict[tuple, asyncio.Lock] = {}
        self.strict = strict
        self.config_file = config_file
        self._resolver = resolver
        self._password_provider = password_provider

    async def _resolve(self, t: Target) -> list[Hop]:
        if self._resolver:
            return await self._resolver(t)
        return await resolve_chain(t, self.config_file, self.strict)

    def set_password(self, t: Target, pw: str):
        self._pw[t.key] = pw
        self.drop(t.key)

    def forget_password(self, t: Target):
        self._pw.pop(t.key, None)
        self.drop(t.key)

    def drop(self, key):
        e = self._entries.pop(key, None)
        if e:
            e.close()

    def close_all(self):
        for k in list(self._entries):
            self.drop(k)

    def reap_idle(self):
        now = time.monotonic()
        for k, e in list(self._entries.items()):
            if now - e.used > IDLE_CLOSE_SECONDS or not e.alive():
                self.drop(k)

    async def _connect(self, t: Target):
        hops = await self._resolve(t)
        pw = self._pw.get(t.key)
        from_keychain = False
        if pw is None and self._password_provider:
            pw = await self._password_provider(t)
            from_keychain = pw is not None
        conns = []
        tunnel = None
        try:
            for i, hop in enumerate(hops):
                last = i == len(hops) - 1
                opts = dict(hop.opts)
                if tunnel is not None:
                    opts["tunnel"] = tunnel
                if last and pw is not None:
                    opts["password"] = pw
                try:
                    conn = await asyncssh.connect(
                        **opts, ignore_encrypted=True, connect_timeout=15,
                        keepalive_interval=30, keepalive_count_max=3)
                except asyncssh.PermissionDenied:
                    if not last:
                        raise ApiError(401, f"Không đăng nhập được máy trung gian {hop.label} "
                                       "(ProxyJump) bằng key/agent", "auth_jump")
                    had_pw = self._pw.pop(t.key, None) is not None or from_keychain
                    raise ApiError(401, "Sai mật khẩu" if had_pw else
                                   "Không đăng nhập được bằng key — nhập mật khẩu", "auth")
                except asyncssh.HostKeyNotVerifiable:
                    h = opts.get("host_key_alias") or opts["host"]
                    if opts.get("port", 22) != 22:
                        h = f"[{h}]:{opts['port']}"
                    raise ApiError(400, f"Host key của {h} chưa có (hoặc không khớp) trong "
                                   "~/.ssh/known_hosts. Hãy ssh vào máy này một lần trong "
                                   "terminal để xác nhận host key.", "hostkey")
                except (OSError, asyncio.TimeoutError) as e:
                    raise ApiError(502, f"Không kết nối được {hop.label}: "
                                   f"{e.strerror or e or 'hết thời gian chờ'}", "connect")
                except asyncssh.DisconnectError as e:
                    raise ApiError(502, f"{hop.label}: {e.reason}", "connect")
                conns.append(conn)
                tunnel = conn
            sftp = await conns[-1].start_sftp_client()
        except BaseException:
            for c in reversed(conns):
                c.close()
            raise
        return _Entry(conns, sftp)

    async def get(self, t: Target):
        k = t.key
        e = self._entries.get(k)
        if e and e.alive():
            e.used = time.monotonic()
            return e.sftp
        lock = self._locks.setdefault(k, asyncio.Lock())
        async with lock:
            e = self._entries.get(k)
            if e and e.alive():
                return e.sftp
            if e:
                self.drop(k)
            log.info("connect %s", t.label)
            e = await self._connect(t)
            self._entries[k] = e
            return e.sftp

    async def exec(self, t: Target, cmd: str, timeout: float = 10) -> str:
        """Chạy 1 lệnh trên server qua kết nối trong pool (nối nếu chưa có). Không làm mới
        'used' của kết nối đang có, nên thăm dò định kỳ không giữ nó sống mãi."""
        e = self._entries.get(t.key)
        if not e or not e.alive():
            await self.get(t)
            e = self._entries[t.key]
        try:
            r = await asyncio.wait_for(e.conns[-1].run(cmd, check=False), timeout)
        except asyncio.TimeoutError:
            raise ApiError(504, "Server phản hồi chậm", "timeout")
        except LOST:
            raise ApiError(502, "Mất kết nối tới server", "lost")
        return r.stdout if isinstance(r.stdout, str) else (r.stdout or b"").decode("utf-8", "replace")

    async def run(self, t: Target, fn):
        """Chạy fn(sftp); nếu kết nối cũ đã chết thì nối lại và thử thêm 1 lần."""
        for attempt in (0, 1):
            sftp = await self.get(t)
            try:
                return await fn(sftp)
            except LOST:
                log.info("connection lost %s, retry=%s", t.label, not attempt)
                self.drop(t.key)
                if attempt:
                    raise ApiError(502, "Mất kết nối tới server", "lost")
