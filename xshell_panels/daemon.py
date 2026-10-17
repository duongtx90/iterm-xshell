"""Daemon chạy trong iTerm2: web server + đăng ký tool Toolbelt + cửa sổ kênh SSH bên trái."""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import secrets
import sys

from aiohttp import web

from . import __version__, keychain
from .config_sync import SshConfigSync
from .sessions_store import SessionStore
from .terminal import TerminalContext
from .web import POOL_KEY, make_app

log = logging.getLogger("iterm-xshell")

LOG_FILE = os.path.expanduser("~/Library/Logs/iterm-xshell.log")
# giữ id cũ để trạng thái tick trong Toolbelt không mất
FILES_TOOL = ("Xshell", "local.iterm-xshell.files")
SESSIONS_TOOL = ("Kênh SSH", "local.iterm-xshell.sessions")

_keep = []


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class Dock:
    """Quản lý process con sessions_window (cửa sổ kênh SSH bên trái)."""

    def __init__(self, app, url: str, make_room: bool = True):
        self.app = app
        self.url = url
        self.make_room = make_room
        self.proc: asyncio.subprocess.Process | None = None
        self.stopping = False
        self.restarts = 0
        self.visible = True

    async def start(self):
        env = dict(os.environ)
        env.pop("ITERM2_COOKIE", None)
        env.pop("ITERM2_KEY", None)
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["XSHELL_SESSIONS_URL"] = self.url
        args = [sys.executable, "-m", "xshell_panels.sessions_window",
                "--make-room", "1" if self.make_room else "0"]
        if not self.visible:
            args.append("--hidden")
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env,
            cwd=pkg_root)
        asyncio.ensure_future(self._read(self.proc))

    async def _read(self, proc):
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            ev = msg.get("ev")
            if ev == "need_room":
                await self._make_room(int(msg.get("dx", 0)))
            elif ev == "state":
                self.visible = bool(msg.get("visible"))
            elif ev == "hidden":
                self.visible = False
            elif ev == "error":
                log.warning("sessions window: %s", msg.get("msg"))
        rc = await proc.wait()
        log.info("sessions window exited rc=%s", rc)
        if not self.stopping and proc is self.proc and self.restarts < 5:
            self.restarts += 1
            await asyncio.sleep(2)
            await self.start()

    async def _make_room(self, dx: int):
        import iterm2
        w = self.app.current_terminal_window
        if not w or dx <= 0:
            return
        try:
            f = await w.async_get_frame()
            if f.size.width - dx < 480:
                return
            await w.async_set_frame(iterm2.Frame(iterm2.Point(f.origin.x + dx, f.origin.y),
                                                 iterm2.Size(f.size.width - dx, f.size.height)))
        except Exception:
            log.exception("make room")

    def send(self, **cmd):
        if self.proc and self.proc.returncode is None and self.proc.stdin:
            self.proc.stdin.write((json.dumps(cmd) + "\n").encode())

    def stop(self):
        self.stopping = True
        self.send(cmd="quit")


async def main(connection):
    import iterm2
    setup_logging()
    log.info("start v%s python=%s", __version__, sys.executable)
    app = await iterm2.async_get_app(connection)
    store = SessionStore()
    settings = store.settings
    sync = SshConfigSync(store)
    sync.start()
    ctx = TerminalContext(app, connection, store, keychain)
    token = secrets.token_urlsafe(24)
    webapp = make_app(ctx=ctx, store=store, token=token, keychain=keychain, watcher=sync)
    runner = web.AppRunner(webapp, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    _keep.extend([runner, site, sync])
    log.info("listening on 127.0.0.1:%s", port)

    async def register(tool, url):
        try:
            await iterm2.tool.async_register_web_view_tool(connection, tool[0], tool[1], False, url)
            log.info("registered tool %s", tool[1])
        except Exception:
            log.exception("register tool %s", tool[1])

    await register(FILES_TOOL, f"{base}/panel?t={token}")

    dock = None
    if settings.get("sessions_window"):
        dock = Dock(app, f"{base}/panel?t={token}",
                    make_room=bool(settings.get("sessions_window_make_room", True)))
        await dock.start()
        _keep.append(dock)

    # Hàm gọi được từ phím tắt: Preferences > Keys > Key Bindings > "Invoke Script Function".
    @iterm2.RPC
    async def xshell_toggle_sessions():
        if dock:
            dock.send(cmd="toggle")

    @iterm2.RPC
    async def xshell_focus_sessions():
        if dock:
            dock.send(cmd="focus")

    await xshell_toggle_sessions.async_register(connection)
    await xshell_focus_sessions.async_register(connection)

    async def reaper():
        while True:
            await asyncio.sleep(60)
            webapp[POOL_KEY].reap_idle()
    _keep.append(asyncio.ensure_future(reaper()))


def run():
    import iterm2
    pid_file = os.path.expanduser("~/.config/iterm-xshell/daemon.pid")
    os.makedirs(os.path.dirname(pid_file), mode=0o700, exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    # retry: lúc iTerm2 vừa khởi động (hoặc đang có hộp thoại modal) API chưa nhận kết nối ngay.
    iterm2.run_forever(main, retry=True)
