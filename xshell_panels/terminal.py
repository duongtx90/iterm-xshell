"""Lớp bao iTerm2 Python API: ngữ cảnh tab hiện tại, mở kênh SSH, cd terminal."""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import socket
import subprocess

from . import ApiError
from .ssh_parse import SSH_BIN, build_ssh_command, parse_ssh_command, target_for_channel

log = logging.getLogger(__name__)

VAR_CHANNEL = "user.xshellSessionId"

_LOCAL_NAMES = {n.lower() for n in (socket.gethostname(), socket.gethostname().split(".")[0],
                                    "localhost") if n}

TAB_COLORS = {
    "red": (230, 76, 60), "orange": (240, 150, 50), "yellow": (230, 200, 60),
    "green": (70, 180, 90), "blue": (60, 130, 230), "purple": (150, 90, 210),
    "gray": (140, 140, 140),
}

OPEN_MODES = ("tab", "vsplit", "hsplit", "current")

RPC_TIMEOUT = 4
BUSY_MSG = "iTerm2 không phản hồi (có hộp thoại đang mở trong iTerm2?)"

_PASSWORD_PROMPT = re.compile(r"(?i)password[^\n:]{0,40}:\s*$")


def is_remote_hostname(host: str | None) -> bool:
    if not host:
        return False
    h = host.lower()
    return h not in _LOCAL_NAMES and h.split(".")[0] not in _LOCAL_NAMES


# iTerm2 hiện cảnh báo MODAL "A session ended very soon after starting" khi session dùng
# custom command kết thúc trong vài giây (advanced setting shortLivedSessionDuration). Modal đó
# chặn luôn Python API (kiểm chứng trên iTerm2 3.7.3), nên giữ session sống tối thiểu ngần này.
MIN_SESSION_SECONDS = 5


def wrapped_command(ssh_cmd: str) -> str:
    """Lệnh cho profile Command của tab mới.

    Tab đóng bình thường khi ssh thoát; riêng khi ssh lỗi (mã 255: sai host, từ chối kết nối…)
    thì giữ tab lại để người dùng đọc được lỗi.
    """
    m = MIN_SESSION_SECONDS
    script = (f"t0=$(date +%s); {ssh_cmd}; s=$?; if [ $s -eq 255 ]; then "
              "printf '\\n[ssh lỗi (mã 255) — nhấn Enter để đóng] '; read _; fi; "
              f"d=$(( $(date +%s) - t0 )); if [ $d -lt {m} ]; then sleep $(( {m} - d + 1 )); fi; exit $s")
    return shlex.join(["/bin/sh", "-c", script])


class TerminalContext:
    def __init__(self, app, connection, store=None, keychain=None):
        self.app = app
        self.connection = connection
        self.store = store
        self.keychain = keychain
        self._open_cache = (0.0, {})

    # ----------------------------------------------------------- helpers
    def _session(self, session_id=None):
        if session_id:
            return self.app.get_session_by_id(session_id)
        win = self.app.current_terminal_window
        if win and win.current_tab:
            return win.current_tab.current_session
        return None

    @staticmethod
    async def _var(s, name):
        try:
            return await asyncio.wait_for(s.async_get_variable(name), RPC_TIMEOUT)
        except asyncio.TimeoutError:
            raise ApiError(503, BUSY_MSG, "busy")
        except Exception:
            return None

    async def _command_line(self, s) -> str:
        cmd = await self._var(s, "commandLine") or ""
        if not cmd:
            pid = await self._var(s, "jobPid")
            if pid:
                try:
                    p = await asyncio.create_subprocess_exec(
                        "/bin/ps", "-o", "args=", "-p", str(int(pid)),
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    out, _ = await asyncio.wait_for(p.communicate(), 2)
                    cmd = out.decode(errors="replace").strip()
                except (OSError, ValueError, asyncio.TimeoutError):
                    cmd = ""
        return cmd

    # ------------------------------------------------------- files panel
    async def current(self) -> dict:
        s = self._session()
        if not s:
            return {"target": None, "path": None, "session_id": None, "channel": None}
        t = parse_ssh_command(await self._command_line(s))
        channel = None
        if t:
            cid = await self._var(s, VAR_CHANNEL)
            ch = self.store.get(cid) if (cid and self.store) else None
            if ch:
                t = target_for_channel(ch)
                channel = {"id": ch["id"], "name": ch["name"], "color": ch.get("color", ""),
                           "remote_dir": ch.get("remote_dir") or None}
        path = None
        if t:
            # Shell Integration trên server báo hostname khác máy local -> path là path remote.
            if is_remote_hostname(await self._var(s, "hostname")):
                path = await self._var(s, "path")
        tj = t.to_json() if t else None
        if tj and channel:
            tj["channel"] = channel["id"]
        return {"target": tj, "path": path,
                "session_id": s.session_id, "channel": channel}

    async def cd(self, session_id, path):
        s = self._session(session_id)
        if not s:
            raise ApiError(404, "Không tìm thấy tab terminal")
        if not parse_ssh_command(await self._command_line(s)):
            raise ApiError(409, "Tab terminal không còn đang ssh")
        await s.async_send_text("cd " + shlex.quote(path) + "\n")

    # ---------------------------------------------------- sessions panel
    async def open_channels(self, max_age=1.5) -> dict[str, list[str]]:
        """{channel_id: [session_id,...]} của các tab đang ssh theo kênh."""
        now = asyncio.get_running_loop().time()
        if now - self._open_cache[0] < max_age:
            return self._open_cache[1]
        sessions = [s for w in self.app.terminal_windows for t in w.tabs for s in t.sessions]

        async def probe(s):
            cid = await self._var(s, VAR_CHANNEL)
            if not cid:
                return None
            if (await self._var(s, "jobName")) != "ssh":
                return None
            return cid, s.session_id

        res: dict[str, list[str]] = {}
        for r in await asyncio.gather(*(probe(s) for s in sessions)):
            if r:
                res.setdefault(r[0], []).append(r[1])
        self._open_cache = (now, res)
        return res

    async def focus(self, session_id):
        s = self._session(session_id)
        if not s:
            raise ApiError(404, "Tab đã đóng")
        await s.async_activate(select_tab=True, order_window_front=True)
        await self.app.async_activate()

    def _customizations(self, ch, command: str):
        import iterm2
        c = iterm2.LocalWriteOnlyProfile()
        c.set_use_custom_command("Yes")
        c.set_command(command)
        rgb = TAB_COLORS.get(ch.get("color") or "")
        if rgb:
            c.set_use_tab_color(True)
            c.set_tab_color(iterm2.Color(*rgb))
        return c

    async def open_channel(self, ch: dict, mode: str = "tab"):
        import iterm2
        if mode not in OPEN_MODES:
            raise ApiError(400, "Kiểu mở không hợp lệ")
        if ch.get("missing"):
            raise ApiError(409, "Alias này không còn trong ~/.ssh/config")
        win = self.app.current_terminal_window
        cur = self._session()
        if mode == "current":
            if not cur:
                mode = "tab"
            else:
                await cur.async_send_text(build_ssh_command(ch) + "\n")
                session = cur
        if mode != "current":
            cust = self._customizations(ch, wrapped_command(build_ssh_command(ch, SSH_BIN)))
            if mode == "tab" or not cur:
                if win:
                    tab = await win.async_create_tab(profile_customizations=cust)
                else:
                    win = await iterm2.Window.async_create(self.connection,
                                                           profile_customizations=cust)
                    tab = win.current_tab
                session = tab.current_session
            else:
                session = await cur.async_split_pane(vertical=(mode == "vsplit"),
                                                     profile_customizations=cust)
        if session is None:
            raise ApiError(500, "iTerm2 không tạo được session")
        await session.async_set_variable(VAR_CHANNEL, ch["id"])
        if mode != "current":
            try:
                tab = session.tab
                if tab is not None and len(tab.sessions) == 1:
                    await tab.async_set_title(ch["name"])
                await session.async_set_name(ch["name"])
            except Exception:
                log.exception("set title")
        await session.async_activate(select_tab=True, order_window_front=True)
        await self.app.async_activate()
        self._open_cache = (0.0, {})
        if ch.get("autotype_password") and ch.get("keychain") and self.keychain:
            asyncio.ensure_future(self._autotype_password(session, ch))
        return session.session_id

    async def _autotype_password(self, session, ch, timeout=30):
        """Chờ dấu nhắc 'password:' trong tab vừa mở rồi gõ mật khẩu từ Keychain (một lần)."""
        import iterm2
        pw = await self.keychain.get_password(ch["id"])
        if not pw:
            return
        try:
            async with iterm2.ScreenStreamer(self.connection, session.session_id,
                                             want_contents=True) as streamer:
                async def wait():
                    while True:
                        contents = await streamer.async_get()
                        lines = [contents.line(i).string for i in range(contents.number_of_lines)]
                        tail = "\n".join(x for x in lines if x.strip())[-200:]
                        if _PASSWORD_PROMPT.search(tail):
                            return True
                if await asyncio.wait_for(wait(), timeout):
                    await session.async_send_text(pw + "\r")
        except (asyncio.TimeoutError, Exception) as e:  # noqa: B014
            log.info("autotype skipped: %s", type(e).__name__)
