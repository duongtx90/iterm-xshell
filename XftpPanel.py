#!/usr/bin/env python3
"""
XftpPanel — panel duyệt file SFTP trong Toolbelt của iTerm2 (giống Xftp).

- Tự nhận host của tab đang chạy `ssh ...` (đọc ~/.ssh/config qua `ssh -G`,
  hỗ trợ alias, Port, User, IdentityFile, ProxyJump).
- Nếu server có cài iTerm2 Shell Integration, panel đi theo thư mục hiện tại
  của terminal.
- Duyệt thư mục, kéo thả để upload, tải về ~/Downloads, tạo thư mục,
  đổi tên, xoá, "cd" terminal tới thư mục đang xem.

Phụ thuộc: iterm2, aiohttp, asyncssh
"""
import asyncio
import getpass
import os
import posixpath
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import aiohttp  # noqa: F401  (aiohttp.web cần package gốc)
from aiohttp import web
import asyncssh

# ---------------------------------------------------------------- cấu hình
DOWNLOAD_DIR = Path.home() / "Downloads"
STRICT_HOST_KEY_CHECKING = True   # dùng ~/.ssh/known_hosts như ssh
CHUNK = 256 * 1024
TOOL_NAME = "Xftp"
TOOL_ID = "local.xftp-panel"

_LOST = tuple(
    c for c in (getattr(asyncssh, n, None)
                for n in ("ConnectionLost", "SFTPConnectionLost", "SFTPNoConnection"))
    if c
) + (BrokenPipeError, ConnectionResetError)


class ApiError(Exception):
    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status, self.msg, self.code = status, msg, code


# ------------------------------------------------------- nhận diện lệnh ssh
_SSH_OPTS_WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")


@dataclass(frozen=True)
class Target:
    alias: str
    user: str | None = None
    port: int | None = None
    jump: str | None = None

    @property
    def key(self):
        return (self.alias, self.user or "", self.port or 0, self.jump or "")

    def to_json(self):
        label = (f"{self.user}@" if self.user else "") + self.alias
        if self.port:
            label += f":{self.port}"
        return {"alias": self.alias, "user": self.user, "port": self.port,
                "jump": self.jump, "label": label}


def parse_ssh_command(cmd: str):
    """'ssh -p 2222 deploy@web1 -i k' -> Target('web1','deploy',2222). None nếu không phải ssh."""
    try:
        argv = shlex.split(cmd or "")
    except ValueError:
        return None
    if not argv or os.path.basename(argv[0]) != "ssh":
        return None
    user = port = dest = jump = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            dest = argv[i + 1] if i + 1 < len(argv) else None
            break
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in _SSH_OPTS_WITH_ARG:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = argv[i] if i < len(argv) else ""
                    if c == "l":
                        user = val
                    elif c == "p":
                        port = val
                    elif c == "J":
                        jump = val
                    elif c == "o":
                        k, _, v = val.replace("=", " ", 1).partition(" ")
                        k = k.strip().lower()
                        if k == "port":
                            port = v.strip()
                        elif k == "user":
                            user = v.strip()
                    break
                j += 1
            i += 1
            continue
        dest = a
        break
    if not dest:
        return None
    if dest.startswith("ssh://"):
        dest = dest[6:].rstrip("/")
        if "@" in dest:
            user, dest = dest.rsplit("@", 1)
        if dest.count(":") == 1:
            dest, port = dest.split(":")
    elif "@" in dest:
        user, dest = dest.rsplit("@", 1)
    try:
        port = int(port) if port else None
    except ValueError:
        port = None
    return Target(dest, user or None, port, jump or None)


def target_from(d) -> Target:
    alias = (d.get("alias") or "").strip()
    if not alias:
        raise ApiError(400, "Chưa chọn host")
    port = d.get("port")
    try:
        port = int(port) if port not in (None, "", "null") else None
    except ValueError:
        raise ApiError(400, "Port không hợp lệ")
    user = d.get("user")
    user = user if user not in (None, "", "null") else None
    jump = d.get("jump")
    jump = jump if jump not in (None, "", "null") else None
    return Target(alias, user, port, jump)


async def resolve(t: Target) -> dict:
    """Dùng `ssh -G` để có đúng HostName/Port/User/IdentityFile/ProxyJump như ssh thật."""
    cmd = ["ssh", "-G"]
    if t.user:
        cmd += ["-l", t.user]
    if t.port:
        cmd += ["-p", str(t.port)]
    if t.jump:
        cmd += ["-J", t.jump]
    cmd.append(t.alias)
    out = b""
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
    except Exception:
        pass
    cfg = {}
    for line in out.decode(errors="replace").splitlines():
        k, _, v = line.partition(" ")
        cfg.setdefault(k.lower(), []).append(v.strip())

    def first(k, default=None):
        return (cfg.get(k) or [default])[0]

    opts = {
        "host": first("hostname", t.alias),
        "port": int(first("port", t.port or 22)),
        "username": first("user", t.user or getpass.getuser()),
    }
    keys = [os.path.expanduser(x) for x in cfg.get("identityfile", [])]
    keys = [k for k in keys if os.path.isfile(k)]
    if keys:
        opts["client_keys"] = keys
    proxy = first("proxyjump")
    if proxy and proxy.lower() != "none":
        opts["tunnel"] = proxy
    agent = first("identityagent")
    if agent and agent.lower() not in ("none", "ssh_auth_sock"):
        opts["agent_path"] = os.path.expanduser(agent)
    return opts


# ------------------------------------------------------------ pool kết nối
class Pool:
    def __init__(self):
        self._conns = {}
        self._pw = {}
        self._locks = {}

    def set_password(self, t: Target, pw: str):
        self._pw[t.key] = pw
        self.drop(t.key)

    def drop(self, key):
        entry = self._conns.pop(key, None)
        if entry:
            try:
                entry[0].close()
            except Exception:
                pass

    async def get(self, t: Target):
        k = t.key
        if k in self._conns:
            return self._conns[k][1]
        lock = self._locks.setdefault(k, asyncio.Lock())
        async with lock:
            if k in self._conns:
                return self._conns[k][1]
            opts = await resolve(t)
            if k in self._pw:
                opts["password"] = self._pw[k]
            if not STRICT_HOST_KEY_CHECKING:
                opts["known_hosts"] = None
            try:
                conn = await asyncssh.connect(
                    **opts, ignore_encrypted=True,
                    connect_timeout=15, keepalive_interval=30)
            except asyncssh.PermissionDenied:
                had_pw = self._pw.pop(k, None) is not None
                raise ApiError(401, "Sai mật khẩu" if had_pw else
                               "Không đăng nhập được bằng key — nhập mật khẩu", "auth")
            except asyncssh.HostKeyNotVerifiable:
                raise ApiError(400, f"Host key của {opts['host']} chưa có trong "
                               "~/.ssh/known_hosts. Hãy ssh vào máy này một lần "
                               "bằng chính hostname đó.", "hostkey")
            sftp = await conn.start_sftp_client()
            self._conns[k] = (conn, sftp)
            return sftp

    async def run(self, t: Target, fn):
        """Chạy fn(sftp); nếu kết nối cũ đã chết thì nối lại và thử thêm 1 lần."""
        for attempt in (0, 1):
            sftp = await self.get(t)
            try:
                return await fn(sftp)
            except _LOST:
                self.drop(t.key)
                if attempt:
                    raise


# ------------------------------------------------ ngữ cảnh terminal (iTerm2)
_LOCAL_NAMES = {n for n in (socket.gethostname(), socket.gethostname().split(".")[0],
                            "localhost") if n}


class TerminalContext:
    def __init__(self, app):
        self.app = app

    def _session(self, session_id=None):
        if session_id:
            return self.app.get_session_by_id(session_id)
        win = self.app.current_terminal_window
        if win and win.current_tab:
            return win.current_tab.current_session
        return None

    async def current(self):
        s = self._session()
        if not s:
            return {"target": None, "path": None, "session_id": None}
        cmd = await s.async_get_variable("commandLine") or ""
        if not cmd:
            pid = await s.async_get_variable("jobPid")
            if pid:
                try:
                    cmd = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                                         capture_output=True, text=True,
                                         timeout=2).stdout.strip()
                except Exception:
                    cmd = ""
        t = parse_ssh_command(cmd)
        path = None
        if t:
            host = await s.async_get_variable("hostname") or ""
            # Shell Integration trên server báo hostname khác máy local -> path là path remote
            if host and host not in _LOCAL_NAMES and host.split(".")[0] not in _LOCAL_NAMES:
                path = await s.async_get_variable("path")
        return {"target": t.to_json() if t else None, "path": path,
                "session_id": s.session_id}

    async def cd(self, session_id, path):
        s = self._session(session_id)
        if not s:
            raise ApiError(404, "Không tìm thấy tab terminal")
        await s.async_send_text("cd " + shlex.quote(path) + "\n")


# ------------------------------------------------------------------ web app
def _base(name):
    name = posixpath.basename(str(name).replace("\\", "/"))
    if name in ("", ".", ".."):
        raise ApiError(400, "Tên không hợp lệ")
    return name


def _unique_local(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf, n = path.stem, path.suffix, 1
    while True:
        p = path.with_name(f"{stem} ({n}){suf}")
        if not p.exists():
            return p
        n += 1


def _is_dir(attrs):
    mode = attrs.permissions or 0
    if mode:
        return stat.S_ISDIR(mode)
    return getattr(attrs, "type", None) == getattr(asyncssh, "FILEXFER_TYPE_DIRECTORY", 2)


def _is_link(attrs):
    mode = attrs.permissions or 0
    if mode:
        return stat.S_ISLNK(mode)
    return getattr(attrs, "type", None) == getattr(asyncssh, "FILEXFER_TYPE_SYMLINK", 3)


def make_app(ctx, token: str) -> web.Application:
    pool = Pool()

    @web.middleware
    async def guard(request, handler):
        if request.path.startswith("/api/"):
            if request.headers.get("X-Token") != token:
                return web.json_response({"error": "forbidden"}, status=403)
        elif request.query.get("t") != token:
            return web.Response(status=403, text="forbidden")
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({"error": e.msg, "code": e.code}, status=e.status)
        except web.HTTPException:
            raise
        except asyncssh.SFTPError as e:
            return web.json_response({"error": getattr(e, "reason", str(e))}, status=400)
        except asyncssh.Error as e:
            return web.json_response({"error": getattr(e, "reason", str(e))}, status=502)
        except (OSError, asyncio.TimeoutError) as e:
            return web.json_response({"error": str(e) or "Hết thời gian chờ"}, status=502)
        except Exception as e:
            traceback.print_exc()
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def index(_):
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def context(_):
        return web.json_response(await ctx.current())

    async def list_dir(req):
        t = target_from(req.query)
        path = req.query.get("path") or "."

        async def fn(sftp):
            p = path
            if p == "~" or p.startswith("~/"):
                p = posixpath.join(await sftp.realpath("."), p[2:])
            p = await sftp.realpath(p)
            out, links = [], []
            for n in await sftp.readdir(p):
                if n.filename in (".", ".."):
                    continue
                a = n.attrs
                e = {"name": n.filename, "size": a.size or 0, "mtime": a.mtime or 0,
                     "dir": _is_dir(a), "link": _is_link(a),
                     "mode": stat.filemode(a.permissions) if a.permissions else ""}
                out.append(e)
                if e["link"]:
                    links.append(e)

            async def follow(e):
                try:
                    st = await sftp.stat(posixpath.join(p, e["name"]))
                    e["dir"] = _is_dir(st)
                except asyncssh.SFTPError:
                    pass
            await asyncio.gather(*(follow(e) for e in links))
            return {"path": p, "entries": out}

        return web.json_response(await pool.run(t, fn))

    async def password(req):
        d = await req.json()
        pool.set_password(target_from(d["target"]), d.get("password") or "")
        return web.json_response({"ok": True})

    async def upload(req):
        t = target_from(req.query)
        dirpath = req.query.get("path") or "."
        sftp = await pool.get(t)
        reader = await req.multipart()
        count = 0
        while True:
            part = await reader.next()
            if part is None:
                break
            if not part.filename:
                continue
            dest = posixpath.join(dirpath, _base(part.filename))
            async with sftp.open(dest, "wb") as f:
                while True:
                    chunk = await part.read_chunk(CHUNK)
                    if not chunk:
                        break
                    await f.write(chunk)
            count += 1
        return web.json_response({"uploaded": count})

    async def download(req):
        d = await req.json()
        t = target_from(d["target"])
        base = d["dir"]
        names = [_base(n) for n in d.get("names", [])]
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved = []

        async def fn(sftp):
            for n in names:
                local = _unique_local(DOWNLOAD_DIR / n)
                await sftp.get(posixpath.join(base, n), str(local),
                               recurse=True, preserve=True)
                saved.append(str(local))
        await pool.run(t, fn)
        if saved and sys.platform == "darwin":
            subprocess.Popen(["open", "-R", saved[-1]])
        return web.json_response({"saved": saved})

    async def mkdir(req):
        d = await req.json()
        t = target_from(d["target"])
        path = posixpath.join(d["dir"], _base(d["name"]))
        await pool.run(t, lambda s: s.mkdir(path))
        return web.json_response({"ok": True})

    async def rename(req):
        d = await req.json()
        t = target_from(d["target"])
        src = posixpath.join(d["dir"], _base(d["old"]))
        dst = posixpath.join(d["dir"], _base(d["new"]))
        await pool.run(t, lambda s: s.rename(src, dst))
        return web.json_response({"ok": True})

    async def delete(req):
        d = await req.json()
        t = target_from(d["target"])
        names = [_base(n) for n in d.get("names", [])]

        async def fn(sftp):
            for n in names:
                p = posixpath.join(d["dir"], n)
                st = await sftp.lstat(p)
                if _is_dir(st):
                    await sftp.rmtree(p)
                else:
                    await sftp.remove(p)
        await pool.run(t, fn)
        return web.json_response({"ok": True})

    async def cd(req):
        d = await req.json()
        await ctx.cd(d.get("session_id"), d["path"])
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard], client_max_size=1024 ** 4)
    app.add_routes([
        web.get("/", index),
        web.get("/api/context", context),
        web.get("/api/list", list_dir),
        web.post("/api/password", password),
        web.post("/api/upload", upload),
        web.post("/api/download", download),
        web.post("/api/mkdir", mkdir),
        web.post("/api/rename", rename),
        web.post("/api/delete", delete),
        web.post("/api/cd", cd),
    ])
    return app


# --------------------------------------------------------------------- UI
INDEX_HTML = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Xftp</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1f;--mut:#86868b;--line:#e3e3e0;--hov:#f0f0ee;--sel:#dbe7ff;--acc:#2f6fed;--err:#c4382c;--bar:#f3f3f1}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#e8e8ea;--mut:#8e8e93;--line:#333336;--hov:#2a2a2d;--sel:#1f3a6b;--acc:#5b8dff;--err:#ff6b5e;--bar:#232326}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:12px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;-webkit-user-select:none;user-select:none}
#app{display:flex;flex-direction:column;height:100%}
.top{padding:8px 8px 6px;border-bottom:1px solid var(--line);background:var(--bar)}
.host{display:flex;gap:6px;align-items:center;margin-bottom:6px}
.host b{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;background:var(--mut);flex:none}
.dot.on{background:#34c759}
.tools{display:flex;gap:2px;align-items:center}
button{font:inherit;color:var(--fg);background:none;border:1px solid transparent;border-radius:5px;padding:3px 6px;cursor:pointer;line-height:1.2}
button:hover:not(:disabled){background:var(--hov);border-color:var(--line)}
button:disabled{opacity:.35;cursor:default}
button.on{color:var(--acc)}
.grow{flex:1}
input[type=text],input[type=password]{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:4px 6px;width:100%;outline:none;-webkit-user-select:text;user-select:text}
input:focus{border-color:var(--acc)}
.pathrow{margin-top:6px}
#list{flex:1;overflow:auto;position:relative;padding:2px 0}
#list.drag{outline:2px dashed var(--acc);outline-offset:-6px;background:color-mix(in srgb,var(--acc) 7%,transparent)}
.row{display:flex;align-items:center;gap:6px;padding:3px 10px;cursor:default;white-space:nowrap}
.row:hover{background:var(--hov)}
.row.sel{background:var(--sel)}
.ic{width:16px;text-align:center;flex:none}
.nm{flex:1;overflow:hidden;text-overflow:ellipsis}
.sz,.tm{color:var(--mut);font-variant-numeric:tabular-nums;flex:none}
.tm{width:92px;text-align:right}
@media (max-width:300px){.tm{display:none}}
.empty{color:var(--mut);padding:24px 14px;text-align:center;line-height:1.6}
.acts{display:flex;gap:2px;padding:5px 6px;border-top:1px solid var(--line);background:var(--bar);flex-wrap:wrap}
.status{padding:4px 10px 6px;color:var(--mut);background:var(--bar);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status.err{color:var(--err)}
.prog{height:3px;background:var(--line);display:none}
.prog i{display:block;height:100%;width:0;background:var(--acc);transition:width .15s}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.25);display:none;align-items:flex-start;justify-content:center;padding-top:60px;z-index:9}
.box{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:12px;width:calc(100% - 24px);max-width:320px;box-shadow:0 8px 30px rgba(0,0,0,.2)}
.box p{margin:0 0 8px;white-space:pre-wrap;word-break:break-word}
.box .btns{display:flex;justify-content:flex-end;gap:6px;margin-top:10px}
.box .ok{background:var(--acc);color:#fff}
.box .ok.danger{background:var(--err)}
label.chk{display:flex;gap:4px;align-items:center;color:var(--mut);cursor:pointer}
</style></head>
<body><div id="app">
 <div class="top">
  <div class="host"><span class="dot" id="dot"></span><b id="hostLabel">Chưa có phiên SSH</b>
   <button id="bManual" title="Kết nối tới host khác">⇄</button></div>
  <div class="tools">
   <button id="bUp" title="Lên thư mục cha">↑</button>
   <button id="bHome" title="Thư mục home">⌂</button>
   <button id="bRefresh" title="Tải lại">⟳</button>
   <button id="bFollow" class="on" title="Đi theo thư mục của terminal">⇵ theo terminal</button>
   <span class="grow"></span>
   <button id="bMkdir" title="Thư mục mới">＋📁</button>
   <button id="bUpload" title="Upload file">⤒</button>
  </div>
  <div class="pathrow"><input type="text" id="path" spellcheck="false" placeholder="/đường/dẫn"></div>
 </div>
 <div id="list"><div class="empty">Mở một tab đang <b>ssh</b> vào server,<br>hoặc bấm ⇄ để nhập host.</div></div>
 <div class="prog" id="prog"><i></i></div>
 <div class="acts">
  <button id="aDown" disabled>⤓ Tải về</button>
  <button id="aRen" disabled>Đổi tên</button>
  <button id="aDel" disabled>Xoá</button>
  <span class="grow"></span>
  <label class="chk"><input type="checkbox" id="hidden" checked> file ẩn</label>
  <button id="aCd" title="cd terminal tới thư mục này">cd ↵</button>
 </div>
 <div class="status" id="status">&nbsp;</div>
</div>
<input type="file" id="picker" multiple hidden>
<div class="modal" id="modal"><div class="box"><p id="mMsg"></p>
 <input type="text" id="mInput"><div class="btns"><button id="mCancel">Huỷ</button><button class="ok" id="mOk">OK</button></div></div></div>
<script>
const T = new URLSearchParams(location.search).get('t');
const $ = id => document.getElementById(id);
const enc = encodeURIComponent;
let target = null, manual = false, cwd = null, entries = [], selected = new Set();
let lastClick = null, follow = true, termPath = null, sessionId = null, busy = false;

async function api(path, opts = {}) {
  const o = {method: opts.method || 'GET', headers: {'X-Token': T}};
  if (opts.json !== undefined) { o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.json); }
  const r = await fetch(path, o);
  let d = {}; try { d = await r.json(); } catch (_) {}
  if (!r.ok) { const e = new Error(d.error || ('HTTP ' + r.status)); e.code = d.code; throw e; }
  return d;
}
function tq() { return 'alias=' + enc(target.alias) + '&user=' + enc(target.user || '') + '&port=' + enc(target.port || '') + '&jump=' + enc(target.jump || ''); }
function join(a, b) { return a === '/' ? '/' + b : a + '/' + b; }
function parent(p) { if (!p || p === '/') return '/'; const i = p.replace(/\/+$/, '').lastIndexOf('/'); return i <= 0 ? '/' : p.slice(0, i); }
function setStatus(m, err) { const s = $('status'); s.textContent = m || ' '; s.className = 'status' + (err ? ' err' : ''); }
function fmtSize(n) { if (n < 1024) return n + ' B'; const u = ['KB','MB','GB','TB']; let i = -1; do { n /= 1024; i++; } while (n >= 1024 && i < 3); return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i]; }
function fmtTime(t) { if (!t) return ''; const d = new Date(t * 1000), p = x => String(x).padStart(2, '0'); return p(d.getDate()) + '/' + p(d.getMonth() + 1) + '/' + String(d.getFullYear()).slice(2) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()); }

function modal(msg, {input = null, ok = 'OK', danger = false, password = false} = {}) {
  return new Promise(res => {
    $('mMsg').textContent = msg;
    const inp = $('mInput');
    inp.style.display = input === null ? 'none' : '';
    inp.type = password ? 'password' : 'text';
    inp.value = input || '';
    $('mOk').textContent = ok; $('mOk').className = 'ok' + (danger ? ' danger' : '');
    $('modal').style.display = 'flex';
    if (input !== null) setTimeout(() => { inp.focus(); inp.select(); }, 0);
    const done = v => { $('modal').style.display = 'none'; $('mOk').onclick = $('mCancel').onclick = inp.onkeydown = null; res(v); };
    $('mOk').onclick = () => done(input === null ? true : inp.value);
    $('mCancel').onclick = () => done(null);
    inp.onkeydown = e => { if (e.key === 'Enter') done(inp.value); if (e.key === 'Escape') done(null); };
  });
}

async function handleErr(e, retry) {
  if (e.code === 'auth') {
    const pw = await modal(e.message + '\n' + target.label, {input: '', ok: 'Kết nối', password: true});
    if (pw !== null) { await api('/api/password', {json: {target, password: pw}}); return retry(); }
    setStatus('Chưa kết nối', true);
  } else setStatus('Lỗi: ' + e.message, true);
}

async function load(path) {
  if (!target) return;
  setStatus('Đang tải…');
  try {
    const d = await api('/api/list?' + tq() + '&path=' + enc(path || ''));
    if (d.path !== cwd) selected.clear();
    cwd = d.path; entries = d.entries;
    render(); setStatus(entries.length + ' mục');
  } catch (e) { await handleErr(e, () => load(path)); }
}

function visible() {
  const show = $('hidden').checked;
  return entries.filter(e => show || !e.name.startsWith('.'))
    .sort((a, b) => (b.dir - a.dir) || a.name.localeCompare(b.name, undefined, {numeric: true, sensitivity: 'base'}));
}

function render() {
  if (document.activeElement !== $('path')) $('path').value = cwd || '';
  const list = $('list'); list.innerHTML = '';
  const vis = visible();
  if (!target) list.innerHTML = '<div class="empty">Mở một tab đang <b>ssh</b> vào server,<br>hoặc bấm ⇄ để nhập host.</div>';
  else if (!vis.length && cwd) list.innerHTML = '<div class="empty">Thư mục trống.<br>Kéo thả file vào đây để upload.</div>';
  vis.forEach((e, idx) => {
    const r = document.createElement('div');
    r.className = 'row' + (selected.has(e.name) ? ' sel' : '');
    r.title = e.mode + '  ' + e.name;
    r.innerHTML = '<span class="ic"></span><span class="nm"></span><span class="sz"></span><span class="tm"></span>';
    r.children[0].textContent = e.dir ? '📁' : (e.link ? '🔗' : '📄');
    r.children[1].textContent = e.name;
    r.children[2].textContent = e.dir ? '' : fmtSize(e.size);
    r.children[3].textContent = fmtTime(e.mtime);
    r.onclick = ev => {
      if (ev.metaKey || ev.ctrlKey) { selected.has(e.name) ? selected.delete(e.name) : selected.add(e.name); }
      else if (ev.shiftKey && lastClick !== null) {
        const [a, b] = [Math.min(lastClick, idx), Math.max(lastClick, idx)];
        selected = new Set(vis.slice(a, b + 1).map(x => x.name));
      } else selected = new Set([e.name]);
      if (!ev.shiftKey) lastClick = idx;
      render();
    };
    r.ondblclick = () => e.dir ? load(join(cwd, e.name)) : download([e.name]);
    list.appendChild(r);
  });
  const n = selected.size, has = !!(target && cwd);
  $('aDown').disabled = !n; $('aDel').disabled = !n; $('aRen').disabled = n !== 1;
  ['bUp','bHome','bRefresh','bMkdir','bUpload','aCd'].forEach(id => $(id).disabled = !has);
  $('aCd').disabled = !has || manual;
  $('bFollow').disabled = manual;
  $('bFollow').classList.toggle('on', follow && !manual);
  $('hostLabel').textContent = target ? target.label : 'Chưa có phiên SSH';
  $('dot').classList.toggle('on', !!target);
}

async function setTarget(t, path) {
  target = t; cwd = null; entries = []; selected.clear(); render();
  if (t) await load(path || '');
}

async function poll() {
  if (busy) return; busy = true;
  try {
    const c = await api('/api/context');
    sessionId = c.session_id;
    if (!manual) {
      if (JSON.stringify(c.target) !== JSON.stringify(target)) { termPath = c.path; await setTarget(c.target, c.path); }
      else if (follow && c.path && c.path !== termPath) { termPath = c.path; await load(c.path); }
    }
  } catch (_) {} finally { busy = false; }
}

function uploadFiles(files) {
  files = [...files];
  if (!target || !cwd || !files.length) return;
  const names = new Set(entries.map(e => e.name));
  const clash = files.filter(f => names.has(f.name)).map(f => f.name);
  const go = () => {
    const fd = new FormData(); files.forEach(f => fd.append('file', f, f.name));
    const x = new XMLHttpRequest();
    x.open('POST', '/api/upload?' + tq() + '&path=' + enc(cwd));
    x.setRequestHeader('X-Token', T);
    $('prog').style.display = 'block'; $('prog').firstChild.style.width = '0';
    setStatus('Đang upload ' + files.length + ' file…');
    x.upload.onprogress = e => { if (e.lengthComputable) { $('prog').firstChild.style.width = (100 * e.loaded / e.total) + '%'; setStatus('Đang upload… ' + Math.round(100 * e.loaded / e.total) + '%'); } };
    x.onloadend = async () => {
      $('prog').style.display = 'none';
      let d = {}; try { d = JSON.parse(x.responseText); } catch (_) {}
      if (x.status === 200) { setStatus('Đã upload ' + d.uploaded + ' file'); await load(cwd); }
      else { const e = new Error(d.error || 'Upload lỗi (' + x.status + ')'); e.code = d.code; await handleErr(e, go); }
    };
    x.send(fd);
  };
  if (clash.length) modal('Ghi đè ' + clash.length + ' file đã có?\n' + clash.slice(0, 6).join('\n'), {ok: 'Ghi đè', danger: true}).then(ok => ok && go());
  else go();
}

async function download(names) {
  setStatus('Đang tải về ' + names.length + ' mục…');
  try { const d = await api('/api/download', {json: {target, dir: cwd, names}}); setStatus('Đã lưu vào Downloads: ' + d.saved.map(s => s.split('/').pop()).join(', ')); }
  catch (e) { await handleErr(e, () => download(names)); }
}

async function action(fn, okMsg) {
  try { await fn(); if (okMsg) setStatus(okMsg); await load(cwd); }
  catch (e) { await handleErr(e, () => action(fn, okMsg)); }
}

// ---- events
$('bUp').onclick = () => load(parent(cwd));
$('bHome').onclick = () => load('~');
$('bRefresh').onclick = () => load(cwd);
$('bFollow').onclick = () => { follow = !follow; render(); if (follow) { termPath = null; poll(); } };
$('hidden').onchange = render;
$('path').onkeydown = e => { if (e.key === 'Enter') { load($('path').value.trim()); $('path').blur(); } if (e.key === 'Escape') { $('path').value = cwd || ''; $('path').blur(); } };
$('bUpload').onclick = () => $('picker').click();
$('picker').onchange = () => { uploadFiles($('picker').files); $('picker').value = ''; };
$('bMkdir').onclick = async () => { const n = await modal('Tên thư mục mới', {input: '', ok: 'Tạo'}); if (n) action(() => api('/api/mkdir', {json: {target, dir: cwd, name: n}}), 'Đã tạo ' + n); };
$('aDown').onclick = () => download([...selected]);
$('aRen').onclick = async () => { const old = [...selected][0]; const n = await modal('Đổi tên', {input: old, ok: 'Đổi'}); if (n && n !== old) action(() => api('/api/rename', {json: {target, dir: cwd, old, new: n}}), 'Đã đổi tên'); };
$('aDel').onclick = async () => {
  const names = [...selected];
  const ok = await modal('Xoá vĩnh viễn ' + names.length + ' mục trên server?\n' + names.slice(0, 6).join('\n') + (names.length > 6 ? '\n…' : ''), {ok: 'Xoá', danger: true});
  if (ok) action(() => api('/api/delete', {json: {target, dir: cwd, names}}).then(() => selected.clear()), 'Đã xoá');
};
$('aCd').onclick = () => api('/api/cd', {json: {session_id: sessionId, path: cwd}}).then(() => setStatus('Đã cd terminal tới ' + cwd)).catch(e => setStatus('Lỗi: ' + e.message, true));
$('bManual').onclick = async () => {
  const v = await modal('Kết nối tới (alias trong ~/.ssh/config hoặc user@host:port).\nĐể trống để quay về theo terminal.', {input: manual && target ? target.label : '', ok: 'Kết nối'});
  if (v === null) return;
  if (!v.trim()) { manual = false; target = null; termPath = null; render(); poll(); return; }
  let s = v.trim(), user = null, port = null;
  if (s.includes('@')) { user = s.slice(0, s.lastIndexOf('@')); s = s.slice(s.lastIndexOf('@') + 1); }
  const m = s.match(/^(.*):(\d+)$/); if (m) { s = m[1]; port = +m[2]; }
  manual = true;
  await setTarget({alias: s, user, port, label: v.trim()}, '');
};
const list = $('list');
list.addEventListener('dragover', e => { e.preventDefault(); if (target && cwd) list.classList.add('drag'); });
list.addEventListener('dragleave', e => { if (e.target === list) list.classList.remove('drag'); });
list.addEventListener('drop', e => {
  e.preventDefault(); list.classList.remove('drag');
  const items = [...(e.dataTransfer.items || [])];
  if (items.some(i => i.webkitGetAsEntry && i.webkitGetAsEntry() && i.webkitGetAsEntry().isDirectory)) {
    setStatus('Chưa hỗ trợ kéo cả thư mục — hãy chọn các file bên trong', true); return;
  }
  uploadFiles(e.dataTransfer.files);
});
document.addEventListener('keydown', e => {
  if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
  if ((e.key === 'Backspace' || e.key === 'Delete') && selected.size) $('aDel').click();
  else if (e.key === 'Enter' && selected.size === 1) { const en = entries.find(x => x.name === [...selected][0]); if (en && en.dir) load(join(cwd, en.name)); }
  else if (e.metaKey && e.key === 'ArrowUp') $('bUp').click();
  else if (e.metaKey && e.key === 'a') { e.preventDefault(); selected = new Set(visible().map(x => x.name)); render(); }
});

render(); poll(); setInterval(poll, 1500);
</script></body></html>
"""


# ------------------------------------------------------------- iTerm2 entry
_keep = []


async def main(connection):
    import iterm2
    app = await iterm2.async_get_app(connection)
    token = secrets.token_urlsafe(24)
    runner = web.AppRunner(make_app(TerminalContext(app), token))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    _keep.extend([runner, site])
    await iterm2.tool.async_register_web_view_tool(
        connection, TOOL_NAME, TOOL_ID, False,
        f"http://127.0.0.1:{port}/?t={token}")


if __name__ == "__main__":
    import iterm2
    iterm2.run_forever(main)
