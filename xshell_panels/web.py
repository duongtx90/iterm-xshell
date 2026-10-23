"""aiohttp app: phục vụ hai panel (files, sessions) và các API /api/*."""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path

import asyncssh
from aiohttp import web

from . import ApiError
from .sessions_store import SessionStore, ValidationError
from . import server_info
from .server_stats import build_command as stats_command, parse as parse_stats
from .sftp_pool import Pool
from .ssh_parse import build_ssh_command, ssh_G, target_for_channel, target_from

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).with_name("ui")
CHUNK = 256 * 1024
WRITE_WINDOW = 8                 # số request ghi SFTP chạy song song khi upload
GET_WINDOW = 256                 # số request đọc SFTP đang bay khi tải về (block_size=-1: tối đa server cho phép)
VIEW_LIMIT = 256 * 1024
POOL_KEY = web.AppKey("pool", Pool)


# --------------------------------------------------------------- helpers
def base_name(name) -> str:
    """Tên file từ client: chỉ lấy basename, chặn '', '.', '..'."""
    name = posixpath.basename(str(name).replace("\\", "/"))
    if name in ("", ".", "..") or "\0" in name:
        raise ApiError(400, "Tên không hợp lệ")
    return name


def rel_parts(rel: str) -> list[str]:
    """'a/b/c' (thư mục con khi upload cả thư mục) -> ['a','b','c']; chặn '..', tuyệt đối."""
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return []
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", "..") or "\0" in p:
            raise ApiError(400, "Đường dẫn không hợp lệ")
    return parts


def unique_local(path: Path) -> Path:
    if not path.exists() and not path.is_symlink():
        return path
    stem, suf, n = path.stem, path.suffix, 1
    if path.is_dir():
        stem, suf = path.name, ""
    while True:
        p = path.with_name(f"{stem} ({n}){suf}")
        if not p.exists():
            return p
        n += 1


def is_dir(attrs) -> bool:
    mode = attrs.permissions or 0
    if mode:
        return stat.S_ISDIR(mode)
    return getattr(attrs, "type", None) == asyncssh.FILEXFER_TYPE_DIRECTORY


def is_link(attrs) -> bool:
    mode = attrs.permissions or 0
    if mode:
        return stat.S_ISLNK(mode)
    return getattr(attrs, "type", None) == asyncssh.FILEXFER_TYPE_SYMLINK


async def write_stream(f, read_chunk):
    """Ghi stream vào file SFTP với nhiều request ghi song song (nhanh hơn ghi tuần tự nhiều lần)."""
    pending: set[asyncio.Future] = set()
    offset = 0
    try:
        while True:
            chunk = await read_chunk(CHUNK)
            if not chunk:
                break
            pending.add(asyncio.ensure_future(f.write(chunk, offset)))
            offset += len(chunk)
            if len(pending) >= WRITE_WINDOW:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    d.result()
        if pending:
            await asyncio.gather(*pending)
            pending = set()
    finally:
        for p in pending:
            p.cancel()
    return offset


async def _exists(sftp, path) -> bool:
    try:
        await sftp.lstat(path)
        return True
    except asyncssh.SFTPNoSuchFile:
        return False


# ------------------------------------------------------------------- app
def make_app(*, ctx, store: SessionStore, token: str, pool: Pool | None = None,
             keychain=None, watcher=None, download_dir: str | None = None) -> web.Application:
    settings = store.settings
    dl_dir = Path(os.path.expanduser(download_dir or settings["download_dir"]))

    async def keychain_password(t):
        if not keychain or not store.settings.get("keychain_enabled"):
            return None
        for ch in store.list():
            if ch.get("keychain") and target_for_channel(ch).key == t.key:
                return await keychain.get_password(ch["id"])
        return None

    def target_of(d):
        """Target từ request: ưu tiên kênh đã lưu (đủ identity/-o), không thì từ các trường."""
        cid = d.get("channel") if hasattr(d, "get") else None
        if cid:
            ch = store.get(cid)
            if ch:
                return target_for_channel(ch)
        return target_from(d)

    if pool is None:
        pool = Pool(password_provider=keychain_password,
                    strict=settings["strict_host_key_checking"],
                    config_file=os.path.expanduser(settings["ssh_config"])
                    if settings["ssh_config"] != "~/.ssh/config" else None)

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
        except ValidationError as e:
            return web.json_response({"error": str(e), "code": "invalid"}, status=400)
        except web.HTTPException:
            raise
        except asyncssh.SFTPNoSuchFile as e:
            return web.json_response({"error": f"Không tồn tại: {e.reason}", "code": "nofile"},
                                     status=404)
        except asyncssh.SFTPPermissionDenied as e:
            return web.json_response({"error": f"Không có quyền: {e.reason}", "code": "perm"},
                                     status=403)
        except asyncssh.SFTPError as e:
            return web.json_response({"error": e.reason or str(e)}, status=400)
        except asyncssh.Error as e:
            return web.json_response({"error": getattr(e, "reason", str(e))}, status=502)
        except (OSError, asyncio.TimeoutError) as e:
            return web.json_response({"error": str(e) or "Hết thời gian chờ"}, status=502)
        except Exception as e:
            traceback.print_exc()
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    def page(name):
        async def handler(_):
            html = (UI_DIR / name).read_text(encoding="utf-8")
            return web.Response(text=html, content_type="text/html",
                                headers={"Cache-Control": "no-store"})
        return handler

    async def body(req) -> dict:
        try:
            d = await req.json()
        except ValueError:
            raise ApiError(400, "JSON không hợp lệ")
        if not isinstance(d, dict):
            raise ApiError(400, "JSON không hợp lệ")
        return d

    # ============================================================ files
    async def context(_):
        return web.json_response(await ctx.current())

    cpu_prev: dict = {}

    async def stats(req):
        t = target_of(req.query)
        out = await pool.exec(t, stats_command())
        res, cur = parse_stats(out, cpu_prev.get(t.key))
        if cur:
            cpu_prev[t.key] = cur
        return web.json_response(res)

    async def server_section(req):
        section = req.match_info["section"]
        if section not in server_info.SECTIONS:
            raise ApiError(404, "Mục không tồn tại")
        out = await pool.exec(target_of(req.query), server_info.build_command(section), timeout=20)
        return web.json_response(server_info.parse(section, out))

    async def list_dir(req):
        t = target_of(req.query)
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
                     "dir": is_dir(a), "link": is_link(a),
                     "mode": stat.filemode(a.permissions) if a.permissions else ""}
                out.append(e)
                if e["link"]:
                    links.append(e)

            async def follow(e):
                try:
                    st = await sftp.stat(posixpath.join(p, e["name"]))
                    e["dir"] = is_dir(st)
                    e["size"] = st.size or 0
                except asyncssh.SFTPError:
                    e["broken"] = True
            await asyncio.gather(*(follow(e) for e in links))
            return {"path": p, "entries": out}

        return web.json_response(await pool.run(t, fn))

    async def password(req):
        d = await body(req)
        pool.set_password(target_of(d.get("target") or {}), d.get("password") or "")
        return web.json_response({"ok": True})

    async def upload(req):
        t = target_of(req.query)
        dirpath = req.query.get("path") or "."
        overwrite = req.query.get("overwrite") == "1"
        sftp = await pool.get(t)
        reader = await req.multipart()
        count, total, rel, fname = 0, 0, "", None
        made: set[str] = set()
        while True:
            part = await reader.next()
            if part is None:
                break
            # Mỗi file có thể đi kèm field "rel" (thư mục con) và "name" (tên gốc UTF-8;
            # filename= trong multipart bị trình duyệt/thư viện escape không thống nhất).
            if part.name == "rel":
                rel = (await part.text()).strip()
                continue
            if part.name == "name":
                fname = await part.text()
                continue
            if part.name != "file":
                continue
            name = fname if fname is not None else part.filename
            parts = rel_parts(rel)
            rel, fname = "", None
            if not name:
                continue
            d = dirpath
            for sub in parts:
                d = posixpath.join(d, sub)
                if d not in made:
                    try:
                        await sftp.mkdir(d)
                    except asyncssh.SFTPError:
                        if not await sftp.isdir(d):
                            raise
                    made.add(d)
            dest = posixpath.join(d, base_name(name))
            if not overwrite and await _exists(sftp, dest):
                raise ApiError(409, f"Đã tồn tại: {dest}", "exists")
            try:
                async with sftp.open(dest, "wb") as f:
                    total += await write_stream(f, part.read_chunk)
            except BaseException:
                # Upload bị huỷ/lỗi giữa chừng: xoá file dở dang.
                try:
                    await asyncio.shield(sftp.remove(dest))
                except Exception:
                    pass
                raise
            count += 1
        return web.json_response({"uploaded": count, "bytes": total})

    jobs: dict[str, dict] = {}       # job tải về: sống trong daemon, độc lập với panel/phiên chat

    def prune_jobs():
        now = time.time()
        for k in [k for k, j in jobs.items() if j["state"] != "running" and now - j["finished"] > 3600]:
            del jobs[k]

    async def remote_size(sftp, path) -> int:
        """Tổng dung lượng (đệ quy) để tính % tiến trình; lỗi thì coi như 0."""
        try:
            st = await sftp.stat(path)
            if not is_dir(st):
                return st.size or 0
            total = 0
            for n in await sftp.readdir(path):
                if n.filename in (".", ".."):
                    continue
                total += await remote_size(sftp, posixpath.join(path, n.filename))
            return total
        except (asyncssh.SFTPError, RecursionError):
            return 0

    async def do_download(t, base, names, dest_dir, job=None):
        saved = []

        async def fn(sftp):
            saved.clear()
            seen: dict[str, int] = {}
            if job:
                job.update(done=0, total=0, current="", state="running", phase="scan")
                job["total"] = sum([await remote_size(sftp, posixpath.join(base, n)) for n in names])
                job["phase"] = "get"

            def progress(src, dst, copied, size):
                key = str(dst)
                job["done"] += copied - seen.get(key, 0)
                seen[key] = copied
                job["current"] = posixpath.basename(str(src))
                job["updated"] = time.time()

            for n in names:
                local = unique_local(dest_dir / n)
                try:
                    await sftp.get(posixpath.join(base, n), str(local), recurse=True, preserve=True,
                                   follow_symlinks=True,
                                   block_size=-1, max_requests=GET_WINDOW,
                                   progress_handler=progress if job else None)
                except BaseException:
                    # Huỷ/lỗi giữa chừng: xoá bản tải dở dang.
                    if local.is_dir() and not local.is_symlink():
                        shutil.rmtree(local, ignore_errors=True)
                    else:
                        local.unlink(missing_ok=True)
                    raise
                saved.append(str(local))
        await pool.run(t, fn)
        return saved

    def job_view(j):
        return {k: v for k, v in j.items() if k != "task"}

    async def download(req):
        d = await body(req)
        t = target_of(d.get("target") or {})
        base = d["dir"]
        names = [base_name(n) for n in d.get("names", [])]
        dest_dir = Path(os.path.expanduser(d.get("dest") or "")) if d.get("dest") else dl_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        reveal = sys.platform == "darwin" and d.get("reveal", True)

        if not d.get("background"):
            saved = await do_download(t, base, names, dest_dir)
            if saved and reveal:
                subprocess.Popen(["/usr/bin/open", "-R", saved[-1]])
            return web.json_response({"saved": saved})

        # Chạy nền: trả về ngay, tiến trình xem ở /api/jobs (còn nguyên khi đổi phiên/đóng panel).
        prune_jobs()
        # Mật khẩu có thể cần nhập: kiểm tra kết nối trước để lỗi 401 trả về cho UI hỏi.
        await pool.get(t)
        now = time.time()
        job = {"id": os.urandom(6).hex(), "kind": "download", "state": "running", "phase": "scan",
               "host": t.label, "dir": base, "names": names, "dest": str(dest_dir),
               "done": 0, "total": 0, "current": "", "saved": [], "error": None,
               "started": now, "updated": now, "finished": 0}

        async def runner():
            try:
                job["saved"] = await do_download(t, base, names, dest_dir, job)
                job["state"] = "done"
                if job["saved"] and reveal:
                    subprocess.Popen(["/usr/bin/open", "-R", job["saved"][-1]])
            except asyncio.CancelledError:
                job["state"] = "cancelled"
            except ApiError as e:
                job.update(state="error", error=e.msg)
            except Exception as e:
                log.warning("download job lỗi: %r", e)
                job.update(state="error", error=getattr(e, "reason", None) or str(e) or type(e).__name__)
            finally:
                job["finished"] = time.time()
        jobs[job["id"]] = job
        job["task"] = asyncio.ensure_future(runner())
        return web.json_response({"job": job_view(job)})

    async def jobs_list(_):
        prune_jobs()
        return web.json_response({"jobs": [job_view(j) for j in jobs.values()]})

    async def jobs_cancel(req):
        j = jobs.get((await body(req)).get("id"))
        if j and j["state"] == "running":
            j["task"].cancel()
        return web.json_response({"ok": True})

    async def jobs_dismiss(req):
        j = jobs.get((await body(req)).get("id"))
        if j and j["state"] != "running":
            del jobs[j["id"]]
        return web.json_response({"ok": True})

    async def view(req):
        t = target_of(req.query)
        path = req.query.get("path") or ""

        async def fn(sftp):
            st = await sftp.stat(path)
            if is_dir(st):
                raise ApiError(400, "Đây là thư mục")
            async with sftp.open(path, "rb") as f:
                data = await f.read(VIEW_LIMIT)
            if b"\0" in data[:8192]:
                raise ApiError(415, "File nhị phân, không xem nhanh được", "binary")
            return {"text": data.decode("utf-8", "replace"), "size": st.size or 0,
                    "truncated": (st.size or 0) > len(data)}
        return web.json_response(await pool.run(t, fn))

    async def mkdir(req):
        d = await body(req)
        t = target_of(d.get("target") or {})
        path = posixpath.join(d["dir"], base_name(d["name"]))
        await pool.run(t, lambda s: s.mkdir(path))
        return web.json_response({"ok": True})

    async def rename(req):
        d = await body(req)
        t = target_of(d.get("target") or {})
        src = posixpath.join(d["dir"], base_name(d["old"]))
        dst = posixpath.join(d["dir"], base_name(d["new"]))

        async def fn(s):
            if await _exists(s, dst):
                raise ApiError(409, f"Đã có mục tên {posixpath.basename(dst)}", "exists")
            await s.rename(src, dst)
        await pool.run(t, fn)
        return web.json_response({"ok": True})

    async def delete(req):
        d = await body(req)
        t = target_of(d.get("target") or {})
        names = [base_name(n) for n in d.get("names", [])]

        async def fn(sftp):
            for n in names:
                p = posixpath.join(d["dir"], n)
                st = await sftp.lstat(p)
                if is_dir(st):          # lstat: symlink tới thư mục chỉ xoá link
                    await sftp.rmtree(p)
                else:
                    await sftp.remove(p)
        await pool.run(t, fn)
        return web.json_response({"ok": True})

    async def cd(req):
        d = await body(req)
        await ctx.cd(d.get("session_id"), d["path"])
        return web.json_response({"ok": True})

    # ========================================================= sessions
    resolved_cache: dict = {}

    async def resolve_info(ch):
        """HostName/User/Port của kênh import (chỉ lưu alias) qua `ssh -G`, cache 60 giây."""
        key = (ch["host"], ch.get("user"), ch.get("port"))
        hit = resolved_cache.get(key)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        cfg = await ssh_G(target_for_channel(ch), settings_file())
        info = {}
        if cfg:
            info = {"hostname": (cfg.get("hostname") or [""])[0],
                    "user": (cfg.get("user") or [""])[0],
                    "port": int((cfg.get("port") or ["22"])[0] or 22)}
        resolved_cache[key] = (time.monotonic(), info)
        return info

    def settings_file():
        f = os.path.expanduser(store.settings["ssh_config"])
        return None if store.settings["ssh_config"] == "~/.ssh/config" else f

    async def ch_json(ch, open_map):
        if ch.get("source") == "ssh_config" and not ch.get("missing"):
            ch["resolved"] = await resolve_info(ch)
        ch["open"] = len(open_map.get(ch["id"], []))
        ch["open_sessions"] = open_map.get(ch["id"], [])
        ch["command"] = build_ssh_command(ch)
        return ch

    def need(sid) -> dict:
        ch = store.get(sid)
        if not ch:
            raise ApiError(404, "Không tìm thấy kênh")
        return ch

    async def sessions_list(_):
        open_map = await ctx.open_channels()
        chs = list(await asyncio.gather(*(ch_json(c, open_map) for c in store.list())))
        st = store.settings
        info = watcher.info() if watcher else {}
        return web.json_response({
            "sessions": chs,
            "settings": {"collapsed_groups": st.get("collapsed_groups", []),
                         "keychain_enabled": bool(st.get("keychain_enabled") and keychain)},
            "ssh_config": info,
        })

    async def sessions_add(req):
        d = await body(req)
        ch = store.add(d, source="manual")
        await _apply_password(ch, d)
        return web.json_response(ch)

    async def _apply_password(ch, d):
        """d['password']: chuỗi -> lưu Keychain; '' / None -> giữ nguyên; d['clear_password'] -> xoá."""
        if not keychain:
            return
        if d.get("clear_password") or not ch.get("keychain"):
            await keychain.delete_password(ch["id"])
        elif d.get("password"):
            if not store.settings.get("keychain_enabled"):
                raise ApiError(400, "Đang tắt lưu mật khẩu vào Keychain")
            if not await keychain.set_password(ch["id"], d["password"]):
                raise ApiError(500, "Không lưu được mật khẩu vào Keychain", "keychain")
            pool.forget_password(target_for_channel(ch))

    async def sessions_update(req):
        sid = req.match_info["id"]
        need(sid)
        d = await body(req)
        ch = store.update(sid, d)
        await _apply_password(ch, d)
        return web.json_response(ch)

    async def sessions_delete(req):
        sid = req.match_info["id"]
        need(sid)
        store.delete(sid)
        if keychain:
            await keychain.delete_password(sid)
        return web.json_response({"ok": True})

    async def sessions_duplicate(req):
        ch = store.duplicate(need(req.match_info["id"])["id"])
        return web.json_response(ch)

    async def sessions_open(req):
        ch = need(req.match_info["id"])
        d = await body(req)
        try:
            sid = await asyncio.wait_for(ctx.open_channel(ch, d.get("mode") or "tab"), 20)
        except asyncio.TimeoutError:
            raise ApiError(503, "iTerm2 không phản hồi (có hộp thoại đang mở trong iTerm2?)", "busy")
        return web.json_response({"ok": True, "session_id": sid})

    async def sessions_focus(req):
        d = await body(req)
        await ctx.focus(d.get("session_id"))
        return web.json_response({"ok": True})

    async def sessions_has_password(req):
        ch = need(req.match_info["id"])
        has = bool(keychain) and await keychain.has_password(ch["id"])
        return web.json_response({"has_password": has})

    async def sessions_sync(_):
        if not watcher:
            raise ApiError(400, "Không có trình đồng bộ ~/.ssh/config")
        return web.json_response(await watcher.sync(force=True))

    async def ui_state(req):
        d = await body(req)
        groups = d.get("collapsed_groups")
        if isinstance(groups, list):
            store.update_settings(collapsed_groups=[str(g) for g in groups][:200])
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard], client_max_size=1024 ** 4)
    app[POOL_KEY] = pool
    app.add_routes([
        web.get("/", page("files.html")),
        web.get("/files", page("files.html")),
        web.get("/sessions", page("sessions.html")),
        web.get("/panel", page("combo.html")),
        web.get("/api/context", context),
        web.get("/api/list", list_dir),
        web.get("/api/stats", stats),
        web.get("/api/server/{section}", server_section),
        web.get("/server", page("server.html")),
        web.get("/api/view", view),
        web.post("/api/password", password),
        web.post("/api/upload", upload),
        web.post("/api/download", download),
        web.get("/api/jobs", jobs_list),
        web.post("/api/jobs/cancel", jobs_cancel),
        web.post("/api/jobs/dismiss", jobs_dismiss),
        web.post("/api/mkdir", mkdir),
        web.post("/api/rename", rename),
        web.post("/api/delete", delete),
        web.post("/api/cd", cd),
        web.get("/api/sessions", sessions_list),
        web.post("/api/sessions", sessions_add),
        web.post("/api/sessions/sync", sessions_sync),
        web.post("/api/sessions/focus", sessions_focus),
        web.put("/api/sessions/{id}", sessions_update),
        web.delete("/api/sessions/{id}", sessions_delete),
        web.post("/api/sessions/{id}/duplicate", sessions_duplicate),
        web.post("/api/sessions/{id}/open", sessions_open),
        web.get("/api/sessions/{id}/password", sessions_has_password),
        web.post("/api/ui", ui_state),
    ])

    async def on_cleanup(_):
        for j in jobs.values():
            if j["state"] == "running":
                j["task"].cancel()
        pool.close_all()
    app.on_cleanup.append(on_cleanup)
    return app
