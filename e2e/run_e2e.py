#!/usr/bin/env python3
"""E2E trên iTerm2 thật: lái iTerm2 qua Python API + gọi HTTP API của daemon đã cài.

Yêu cầu: đã chạy install.sh (daemon đang chạy), có host test trong ~/.ssh/config:
  - XSHELL_E2E_CFG_HOST (mặc định xshell-e2e-si): server CÓ iTerm2 Shell Integration
  - kênh tay tới 127.0.0.1:XSHELL_E2E_PLAIN_PORT (mặc định 2222), key ~/.ssh/xshell-e2e/id_e2e

Mọi thao tác diễn ra trong một cửa sổ iTerm2 riêng do script tạo và đóng khi xong.
Chạy: ~/Library/Application\\ Support/iterm-xshell/venv/bin/python e2e/run_e2e.py
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

import aiohttp
import iterm2

CFG_HOST = os.environ.get("XSHELL_E2E_CFG_HOST", "xshell-e2e-si")
PLAIN_PORT = int(os.environ.get("XSHELL_E2E_PLAIN_PORT", "2222"))
E2E_DIR = os.path.expanduser("~/.ssh/xshell-e2e")
SSH_CONFIG = os.path.expanduser("~/.ssh/config")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def daemon_url():
    """Lấy base URL + token từ biến môi trường của process cửa sổ kênh SSH (cùng user)."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "xshell_panels.sessions_window" in line:
            pid = line.split()[0]
            env = subprocess.run(["ps", "eww", "-p", pid, "-o", "command="],
                                 capture_output=True, text=True).stdout
            m = re.search(r"XSHELL_SESSIONS_URL=(\S+)", env)
            if m:
                u = urllib.parse.urlparse(m.group(1))
                return f"{u.scheme}://{u.netloc}", urllib.parse.parse_qs(u.query)["t"][0]
    raise SystemExit("Không tìm thấy daemon iterm-xshell đang chạy")


class Api:
    def __init__(self, base, token):
        self.base, self.h = base, {"X-Token": token}
        self.s = aiohttp.ClientSession()

    async def req(self, method, url, json_=None, params=None, data=None):
        async with self.s.request(method, self.base + url, json=json_, params=params, data=data,
                                  headers=self.h) as r:
            try:
                return r.status, await r.json()
            except Exception:
                return r.status, await r.text()

    async def get(self, url, **params):
        return await self.req("GET", url, params=params)

    async def post(self, url, j=None, method="POST"):
        return await self.req(method, url, json_=j if j is not None else {})


async def wait_for(fn, timeout=15, interval=0.3):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = await fn()
        if last:
            return last
        await asyncio.sleep(interval)
    return last


async def screen_text(conn, session):
    c = await session.async_get_screen_contents()
    return "\n".join(c.line(i).string for i in range(c.number_of_lines))


async def main(conn):
    base, token = daemon_url()
    api = Api(base, token)
    app = await iterm2.async_get_app(conn)
    print("daemon:", base)
    win = await iterm2.Window.async_create(conn)
    await win.async_activate()
    await asyncio.sleep(1)
    created = []
    cfg_backup = open(SSH_CONFIG, encoding="utf-8").read()
    try:
        await run(conn, app, api, win, created)
    finally:
        with open(SSH_CONFIG, "w", encoding="utf-8") as f:
            f.write(cfg_backup)
        for sid in created:
            await api.post(f"/api/sessions/{sid}", method="DELETE")
        try:
            await win.async_close(force=True)
        except Exception:
            pass
        await api.s.close()
    n_fail = sum(not ok for _, ok, _ in RESULTS)
    print(f"\n{len(RESULTS) - n_fail}/{len(RESULTS)} PASS")
    with open(os.path.join(os.path.dirname(__file__), "last_run.json"), "w") as f:
        json.dump([{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS], f,
                  ensure_ascii=False, indent=1)
    sys.exit(1 if n_fail else 0)


async def run(conn, app, api, win, created):
    # ---- 2. kênh từ ~/.ssh/config + kênh tay
    st, d = await api.get("/api/sessions")
    cfg_ch = next((c for c in d["sessions"] if c["host"] == CFG_HOST and c["source"] == "ssh_config"), None)
    check("2a kênh từ ~/.ssh/config xuất hiện", cfg_ch, f"{sum(c['source'] == 'ssh_config' for c in d['sessions'])} kênh import")
    st, man = await api.post("/api/sessions", {
        "name": "E2E thủ công", "group": "E2E", "host": "127.0.0.1", "port": PLAIN_PORT, "user": "tester",
        "identity_file": f"{E2E_DIR}/id_e2e", "color": "green", "remote_dir": "/config",
        "extra_args": f"-o UserKnownHostsFile={E2E_DIR}/known_hosts -o IdentitiesOnly=yes"})
    check("2b thêm kênh tay", st == 200, man.get("command") if st == 200 else man)
    created.append(man["id"])

    n_tabs = len(win.tabs)
    st, r = await api.post(f"/api/sessions/{man['id']}/open", {"mode": "tab"})
    check("2c click kênh -> mở tab mới", st == 200 and len(app.get_window_by_id(win.window_id).tabs) == n_tabs + 1, r)
    s_man = app.get_session_by_id(r["session_id"])

    async def is_ssh(s):
        return (await s.async_get_variable("jobName")) == "ssh" and "$" in (await screen_text(conn, s))
    check("2d ssh thành công (prompt remote)", await wait_for(lambda: is_ssh(s_man), 20),
          (await screen_text(conn, s_man)).strip().splitlines()[-1:] )
    check("2e biến user.xshellSessionId", await s_man.async_get_variable("user.xshellSessionId") == man["id"])
    tab_title = await s_man.tab.async_get_variable("titleOverride")
    check("2f tên tab = tên kênh", tab_title == "E2E thủ công", repr(tab_title))
    txt = await screen_text(conn, s_man)
    check("2g remote_dir: vào sẵn /config", "/config" in txt or "~" in txt, txt.strip().splitlines()[-1:])

    # ---- 3. panel file theo tab hiện tại
    st, c = await api.get("/api/context")
    check("3a panel file nhận kênh tay (theo user.xshellSessionId)",
          c["target"] and c["target"].get("channel") == man["id"] and c["channel"]["name"] == "E2E thủ công", c)
    st, ls = await api.get("/api/list", **{k: str(v) for k, v in c["target"].items() if v is not None and k != "label"}, path="/config")
    check("3b liệt kê file qua SFTP (kênh tay, -o UserKnownHostsFile)", st == 200, ls if st != 200 else f"{len(ls['entries'])} mục")

    st, r2 = await api.post(f"/api/sessions/{cfg_ch['id']}/open", {"mode": "tab"})
    s_cfg = app.get_session_by_id(r2["session_id"])
    await wait_for(lambda: is_ssh(s_cfg), 20)
    st, c2 = await api.get("/api/context")
    check("3c mở kênh config -> panel đổi host", c2["target"] and c2["target"]["alias"] == CFG_HOST, c2["target"])
    await s_man.async_activate(select_tab=True)
    await asyncio.sleep(0.5)
    st, c3 = await api.get("/api/context")
    check("3d chuyển lại tab kênh tay -> panel đổi theo", c3["target"] and c3["target"].get("channel") == man["id"])
    await s_cfg.async_activate(select_tab=True)
    await asyncio.sleep(0.5)

    # ---- 5. Shell Integration: cd trong terminal -> panel đi theo; nút cd ↵
    await s_cfg.async_send_text("cd /config/data\n")

    async def ctx_path(p):
        _, c = await api.get("/api/context")
        return c if c.get("path") == p else None
    c5 = await wait_for(lambda: ctx_path("/config/data"), 10)
    check("5a cd trong terminal -> panel nhận path (Shell Integration)", c5, c5 and c5["path"])
    st, _ = await api.post("/api/cd", {"session_id": s_cfg.session_id, "path": "/config/data/thư mục có dấu"})
    c5b = await wait_for(lambda: ctx_path("/config/data/thư mục có dấu"), 10)
    check("5b nút 'cd ↵' chuyển terminal (path có dấu cách + tiếng Việt)", st == 200 and c5b)
    await s_cfg.async_send_text("cd ~\n")

    # ---- 4. thao tác file (kênh config)
    t = {"alias": CFG_HOST, "channel": cfg_ch["id"]}
    names = ["e2e upload.txt", "tệp tiếng Việt.txt"]
    fd = aiohttp.FormData()
    for n in names:
        fd.add_field("name", n)
        fd.add_field("file", ("nội dung " + n).encode(), filename=n)
    st, up = await api.req("POST", "/api/upload", params={**t, "path": "/config/data", "overwrite": "1"}, data=fd)
    check("4a upload (multipart, tên tiếng Việt)", st == 200 and up.get("uploaded") == 2, up)
    out = subprocess.run(["docker", "exec", "xshell-e2e-1", "cat", "/config/data/tệp tiếng Việt.txt"],
                         capture_output=True, text=True).stdout
    check("4b nội dung trên server đúng", out == "nội dung tệp tiếng Việt.txt", out)
    st, _ = await api.post("/api/mkdir", {"target": t, "dir": "/config/data", "name": "e2e mới"})
    check("4c tạo thư mục", st == 200)
    st, _ = await api.post("/api/rename", {"target": t, "dir": "/config/data", "old": "e2e upload.txt", "new": "e2e đổi tên.txt"})
    check("4d đổi tên", st == 200)
    dest = tempfile.mkdtemp(prefix="xshell-e2e-dl-")
    st, dl = await api.post("/api/download", {"target": t, "dir": "/config", "names": ["data", "datalink"], "dest": dest, "reveal": False})
    ok = st == 200 and os.path.isfile(os.path.join(dest, "data", "e2e đổi tên.txt")) \
        and os.path.isfile(os.path.join(dest, "datalink", "hello.txt"))
    check("4e tải về cả thư mục (kể cả symlink tới thư mục)", ok, dl)
    st, _ = await api.post("/api/delete", {"target": t, "dir": "/config/data", "names": ["e2e mới", "e2e đổi tên.txt", "tệp tiếng Việt.txt"]})
    st2, ls = await api.get("/api/list", **t, path="/config/data")
    left = {e["name"] for e in ls["entries"]}
    check("4f xoá", st == 200 and not ({"e2e mới", "e2e đổi tên.txt"} & left), sorted(left))
    st, v = await api.get("/api/view", **t, path="/config/data/hello.txt")
    check("4g xem nhanh file text", st == 200 and v["text"] == "hello\n", v)

    # ---- badge + chuyển tới tab đang mở
    st, d = await api.get("/api/sessions")
    by = {c["id"]: c for c in d["sessions"]}
    check("badge kênh đang mở", by[man["id"]]["open"] == 1 and by[cfg_ch["id"]]["open"] == 1,
          (by[man["id"]]["open"], by[cfg_ch["id"]]["open"]))
    st, _ = await api.post("/api/sessions/focus", {"session_id": s_man.session_id})
    await asyncio.sleep(0.5)
    st, c = await api.get("/api/context")
    check("menu 'chuyển tới tab đang mở'", c["session_id"] == s_man.session_id)

    # ---- split dọc/ngang, tab hiện tại
    tab = s_man.tab
    before = len(tab.sessions)
    st, r = await api.post(f"/api/sessions/{cfg_ch['id']}/open", {"mode": "vsplit"})
    st2, r2 = await api.post(f"/api/sessions/{cfg_ch['id']}/open", {"mode": "hsplit"})
    tab = app.get_session_by_id(s_man.session_id).tab
    check("mở ở split dọc + ngang", st == 200 and st2 == 200 and len(tab.sessions) == before + 2)
    s_split = app.get_session_by_id(r2["session_id"])
    await wait_for(lambda: is_ssh(s_split), 20)
    # tab hiện tại: tạo tab shell local rồi mở kênh vào chính nó
    local_tab = await win.async_create_tab()
    s_local = local_tab.current_session
    await asyncio.sleep(1.5)
    st, r = await api.post(f"/api/sessions/{cfg_ch['id']}/open", {"mode": "current"})
    check("mở ở tab hiện tại (gửi lệnh vào shell đang mở)", st == 200 and r["session_id"] == s_local.session_id
          and await wait_for(lambda: is_ssh(s_local), 20))
    await s_local.async_send_text("exit\n")
    back_local = await wait_for(lambda: _job_is(s_local, ("zsh", "bash", "fish", "-zsh")), 10)
    check("ssh thoát ở tab hiện tại -> về shell local, tab còn", back_local)

    # ---- tab mới tự đóng khi ssh thoát bình thường
    await s_split.async_send_text("exit\n")

    async def gone(sid):
        return app.get_session_by_id(sid) is None
    check("tab/split đóng khi ssh thoát", await wait_for(lambda: gone(s_split.session_id), 10))
    # ssh lỗi (255) -> giữ tab lại để đọc lỗi
    st, bad = await api.post("/api/sessions", {"name": "E2E lỗi", "host": "127.0.0.1", "port": 1, "group": "E2E"})
    created.append(bad["id"])
    st, r = await api.post(f"/api/sessions/{bad['id']}/open", {"mode": "tab"})
    s_bad = app.get_session_by_id(r["session_id"])
    msg = await wait_for(lambda: _screen_has(conn, s_bad, "ssh lỗi"), 15)
    check("ssh lỗi -> tab giữ lại hiện thông báo", msg)
    await s_bad.async_send_text("\r")

    # ---- 6. sửa ~/.ssh/config -> danh sách cập nhật
    with open(SSH_CONFIG, "a", encoding="utf-8") as f:
        f.write("\nHost xshell-e2e-added xshell-e2e-added2\n  HostName 127.0.0.1\n")

    async def has_alias(a, missing=False):
        _, d = await api.get("/api/sessions")
        return any(c["host"] == a and c["missing"] == missing for c in d["sessions"])
    check("6a thêm Host vào ~/.ssh/config -> kênh xuất hiện", await wait_for(lambda: has_alias("xshell-e2e-added2"), 10))
    txt = open(SSH_CONFIG, encoding="utf-8").read().replace(" xshell-e2e-added2", "")
    with open(SSH_CONFIG, "w", encoding="utf-8") as f:
        f.write(txt)
    check("6b xoá Host -> kênh được đánh dấu biến mất (không xoá)",
          await wait_for(lambda: has_alias("xshell-e2e-added2", missing=True), 10))
    _, d = await api.get("/api/sessions")
    gone_ch = next(c for c in d["sessions"] if c["host"] == "xshell-e2e-added2")
    st, r = await api.post(f"/api/sessions/{gone_ch['id']}/open", {"mode": "tab"})
    check("mở kênh đã biến mất bị chặn", st == 409, r)
    for c in d["sessions"]:
        if c["host"].startswith("xshell-e2e-added"):
            created.append(c["id"])

    # ---- 7. không có tab ssh
    await win.async_create_tab()
    await asyncio.sleep(1)
    st, c = await api.get("/api/context")
    check("7 tab không ssh -> panel trống, không lỗi", st == 200 and c["target"] is None, c)
    st, d = await api.get("/api/sessions")
    check("7b API kênh vẫn chạy", st == 200)
    for s in (s_man, s_cfg):
        await s.async_send_text("exit\n")
    await asyncio.sleep(2)
    _, d = await api.get("/api/sessions")
    by = {c["id"]: c for c in d["sessions"]}
    check("badge về 0 khi đóng tab", by[man["id"]]["open"] == 0, by[man["id"]]["open"])


async def _job_is(s, names):
    return (await s.async_get_variable("jobName")) in names


async def _screen_has(conn, s, needle):
    try:
        return needle in await screen_text(conn, s)
    except Exception:
        return False


if __name__ == "__main__":
    iterm2.run_until_complete(main)
