#!/usr/bin/env python3
"""Kiểm thử UI hai panel trong WKWebView offscreen + chụp ảnh (không cần quyền Screen Recording).

Dựng SFTP server thật (asyncssh) + web app thật với TerminalContext giả, nạp /sessions và /files
vào WKWebView rộng 280px (sáng + tối), mô phỏng thao tác bằng JS, kiểm tra DOM và lưu PNG.

Chạy: python e2e/ui_snapshots.py [thư_mục_ảnh]   (mặc định e2e/screenshots)
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncssh  # noqa: E402
from aiohttp import web  # noqa: E402

from xshell_panels.sessions_store import SessionStore  # noqa: E402
from xshell_panels.web import make_app  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "e2e" / "screenshots")
TOKEN = "ui-token"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""), flush=True)


# ------------------------------------------------------------- server side
class FakeCtx:
    def __init__(self, store):
        self.store = store
        self.cds = []

    async def current(self):
        ch = next(c for c in self.store.list() if c["name"] == "Web prod 1")
        return {"target": {"alias": "box", "user": None, "port": None, "jump": None,
                           "identity": None, "label": "box", "channel": None},
                "path": "/srv/app", "session_id": "S1",
                "channel": {"id": ch["id"], "name": ch["name"], "color": "red", "remote_dir": None}}

    async def cd(self, sid, path):
        self.cds.append(path)

    async def open_channels(self):
        chs = self.store.list()
        return {chs[0]["id"]: ["S1"], chs[2]["id"]: ["S2", "S3"]}

    async def open_channel(self, ch, mode):
        return "S9"

    async def focus(self, sid):
        pass


def start_backend(tmp: Path):
    ready = threading.Event()
    info = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            root = tmp / "remote"
            (root / "srv" / "app" / "logs").mkdir(parents=True)
            (root / "srv" / "app" / "config").mkdir()
            for n, body in [("README.md", "# Ứng dụng demo\n\nXin chào từ server.\n" * 3),
                            ("docker-compose.yml", "services:\n  web:\n    image: nginx\n"),
                            ("báo cáo tháng 10.xlsx", "x" * 48213), (".env", "SECRET=1\n"),
                            ("deploy.sh", "#!/bin/sh\necho deploy\n")]:
                (root / "srv" / "app" / n).write_text(body)
            (root / "srv" / "app" / "logs" / "app.log").write_text("log\n" * 1000)
            os.symlink("logs", root / "srv" / "app" / "current-logs")
            hk = asyncssh.generate_private_key("ssh-ed25519")
            ck = asyncssh.generate_private_key("ssh-ed25519")
            key = tmp / "id"
            key.write_bytes(ck.export_private_key())
            key.chmod(0o600)

            class S(asyncssh.SSHServer):
                def begin_auth(self, u):
                    return True

            acc = await asyncssh.listen(
                "127.0.0.1", 0, server_host_keys=[hk], server_factory=S,
                authorized_client_keys=asyncssh.import_authorized_keys(ck.export_public_key().decode()),
                sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)))
            port = acc.sockets[0].getsockname()[1]
            kh = tmp / "kh"
            kh.write_text(f"[127.0.0.1]:{port} {hk.export_public_key().decode()}")
            cfg = tmp / "ssh_config"
            cfg.write_text(f"Host box\n  HostName 127.0.0.1\n  Port {port}\n  User demo\n"
                           f"  IdentityFile {key}\n  IdentityAgent none\n  UserKnownHostsFile {kh}\n")
            store = SessionStore(str(tmp / "sessions.json"))
            store.update_settings(ssh_config=str(cfg))
            for d in [dict(name="Web prod 1", group="Production", host="10.0.1.11", user="deploy", color="red", tags="web, prod"),
                      dict(name="Web prod 2", group="Production", host="10.0.1.12", user="deploy", color="red"),
                      dict(name="DB chính", group="Production", host="db-main", port=2222, color="orange", tags="mysql"),
                      dict(name="Kho vận — API", group="Kho vận", host="kho-api", user="ops", color="green", remote_dir="/srv/kho"),
                      dict(name="Kho vận — worker", group="Kho vận", host="kho-worker", user="ops", color="green"),
                      dict(name="Staging", group="Staging", host="stg", color="blue", proxy_jump="bastion")]:
                store.add(d)
            store.sync_ssh_config(["bastion", "pdev", "box", "old-host"])
            store.sync_ssh_config(["bastion", "pdev", "box"])
            app = make_app(ctx=FakeCtx(store), store=store, token=TOKEN, download_dir=str(tmp / "dl"))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            info["base"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            ready.set()
            await asyncio.Event().wait()

        loop.run_until_complete(main())

    threading.Thread(target=run, daemon=True).start()
    ready.wait(15)
    return info["base"]


# ---------------------------------------------------------------- WebKit side
def main():
    import AppKit
    import WebKit
    from Foundation import NSURL, NSURLRequest, NSDate, NSRunLoop, NSMakeRect

    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="xshell-ui-"))
    base = start_backend(tmp)
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyProhibited)

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))

    def make(path, dark, w=280, h=620):
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(-5000, -5000, w, h), AppKit.NSWindowStyleMaskBorderless,
            AppKit.NSBackingStoreBuffered, False)
        wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, w, h), WebKit.WKWebViewConfiguration.alloc().init())
        wv.setAppearance_(AppKit.NSAppearance.appearanceNamed_(
            AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua))
        win.setContentView_(wv)
        win.orderBack_(None)
        wv.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(f"{base}{path}?t={TOKEN}")))
        pump(2.5)
        return win, wv

    def js(wv, code, wait=0.4):
        box = {}

        def done(res, err):
            box["r"], box["e"] = res, err
        # Mỗi đoạn chạy trong scope riêng (tránh khai báo trùng const giữa các lần gọi).
        wv.evaluateJavaScript_completionHandler_("(function(){return eval(%s)})()" % json.dumps(code), done)
        end = time.time() + 5
        while "r" not in box and time.time() < end:
            pump(0.05)
        pump(wait)
        if box.get("e"):
            raise RuntimeError(str(box["e"]))
        r = box.get("r")
        return json.loads(r) if isinstance(r, str) and r[:1] in "[{" else r

    def shot(wv, name):
        box = {}

        def done(img, err):
            box["img"], box["e"] = img, err
        wv.takeSnapshotWithConfiguration_completionHandler_(None, done)
        end = time.time() + 5
        while "img" not in box and time.time() < end:
            pump(0.05)
        img = box.get("img")
        if img is None:
            check(f"chụp {name}", False, str(box.get("e")))
            return
        tiff = img.TIFFRepresentation()
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
        png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
        p = OUT / f"{name}.png"
        png.writeToFile_atomically_(str(p), True)
        print("   ảnh:", p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)

    for dark in (False, True):
        sfx = "dark" if dark else "light"
        # ---------------- panel kênh SSH
        win, wv = make("/sessions", dark)
        st = js(wv, "JSON.stringify({rows: document.querySelectorAll('.row').length, grps: [...document.querySelectorAll('.grp .g')].map(x=>x.textContent), badges: document.querySelectorAll('.badge').length, missing: document.querySelectorAll('.row.missing').length})")
        check(f"[{sfx}] sessions: render nhóm/kênh/badge",
              st["rows"] == 10 and st["grps"] == ["Kho vận", "Production", "Staging", "~/.ssh/config"] and st["badges"] == 2 and st["missing"] == 1, st)
        shot(wv, f"sessions-list-{sfx}")
        if not dark:
            r = js(wv, "document.getElementById('q').value='kho van'; document.getElementById('q').dispatchEvent(new Event('input')); JSON.stringify([...document.querySelectorAll('.row .nm')].map(x=>x.textContent))")
            check("sessions: tìm không dấu 'kho van'", r == ["Kho vận — API", "Kho vận — worker"], r)
            shot(wv, "sessions-search")
            js(wv, "document.getElementById('q').value=''; document.getElementById('q').dispatchEvent(new Event('input'));")
            grp = "[...document.querySelectorAll('.grp')].find(g=>g.textContent.includes('Production'))"
            r = js(wv, grp + ".click(); document.querySelectorAll('.row').length")
            check("sessions: thu gọn nhóm", r == 7, r)
            shot(wv, "sessions-group-collapsed")
            js(wv, grp + ".click()")
        r = js(wv, "const row=[...document.querySelectorAll('.row')].find(r=>r.textContent.includes('DB chính')); const b=row.getBoundingClientRect(); row.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:b.left+40,clientY:b.top+10})); JSON.stringify([...document.querySelectorAll('#menu div')].map(d=>d.textContent))")
        check(f"[{sfx}] sessions: menu chuột phải tự vẽ",
              r[:4] == ["Mở trong tab mới", "Mở ở split dọc", "Mở ở split ngang", "Mở ở tab hiện tại"] and "Copy lệnh ssh" in r and "Chuyển tới tab đang mở" in r, r)
        shot(wv, f"sessions-context-menu-{sfx}")
        js(wv, "document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))")
        r = js(wv, "const row=[...document.querySelectorAll('.row')].find(r=>r.textContent.includes('Kho vận — API')); row.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:30,clientY:100})); [...document.querySelectorAll('#menu div')].find(d=>d.textContent==='Sửa…').click(); JSON.stringify({shown: getComputedStyle(document.getElementById('form')).display, name: document.getElementById('f_name').value, cmd: document.getElementById('f_cmd').value})", 1.0)
        check(f"[{sfx}] sessions: form sửa kênh", r["shown"] == "flex" and r["name"] == "Kho vận — API" and "-t" in r["cmd"], r)
        shot(wv, f"sessions-edit-form-{sfx}")
        if not dark:
            r = js(wv, "document.getElementById('f_port').value='70000'; document.getElementById('fSave').click(); 'ok'", 0.8)
            err = js(wv, "document.getElementById('fErr').textContent")
            check("sessions: báo lỗi port không hợp lệ", "Port" in (err or ""), err)
            js(wv, "document.getElementById('fCancel').click()")
            r = js(wv, "document.getElementById('bAdd').click(); document.getElementById('f_host').value='new-host'; document.getElementById('f_name').value='Kênh mới'; document.getElementById('f_group').value='Mới'; document.getElementById('fSave').click(); 'ok'", 1.5)
            r = js(wv, "JSON.stringify([...document.querySelectorAll('.row .nm')].map(x=>x.textContent))", 2.5)
            check("sessions: thêm kênh qua form", "Kênh mới" in r, r)
            r = js(wv, "const row=[...document.querySelectorAll('.row')].find(r=>r.textContent.includes('Kênh mới')); row.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:30,clientY:100})); [...document.querySelectorAll('#menu div')].find(d=>d.textContent==='Xoá').click(); document.getElementById('mMsg').textContent", 0.5)
            check("sessions: xoá có xác nhận (modal tự vẽ)", "Xoá kênh" in r, r)
            shot(wv, "sessions-delete-confirm")
            js(wv, "document.getElementById('mOk').click()", 2.5)
            r = js(wv, "JSON.stringify([...document.querySelectorAll('.row .nm')].map(x=>x.textContent))")
            check("sessions: đã xoá sau khi xác nhận", "Kênh mới" not in r, r)
        win.close()

        # ---------------- panel file
        win, wv = make("/files", dark)
        pump(2.5)
        r = js(wv, "JSON.stringify({host: document.getElementById('hostLabel').textContent, chan: document.getElementById('chan').textContent, path: document.getElementById('path').value, rows: [...document.querySelectorAll('.row .nm')].map(x=>x.textContent)})")
        check(f"[{sfx}] files: tự nhận host + theo path terminal",
              r["host"] == "box" and r["chan"] == "Web prod 1" and r["path"] == "/srv/app" and "báo cáo tháng 10.xlsx" in r["rows"] and r["rows"][0] == "config", r)
        shot(wv, f"files-list-{sfx}")
        if not dark:
            r = js(wv, "const rows=[...document.querySelectorAll('.row')]; rows.find(x=>x.textContent.includes('README')).click(); document.querySelectorAll('.row')[[...document.querySelectorAll('.row')].findIndex(x=>x.textContent.includes('deploy'))].dispatchEvent(new MouseEvent('click',{bubbles:true,metaKey:true})); JSON.stringify({sel: document.querySelectorAll('.row.sel').length, ren: document.getElementById('aRen').disabled, del: document.getElementById('aDel').disabled})")
            check("files: chọn nhiều bằng ⌘", r == {"sel": 2, "ren": True, "del": False}, r)
            shot(wv, "files-multiselect")
            r = js(wv, "document.getElementById('aDel').click(); document.getElementById('mMsg').textContent")
            check("files: xoá có xác nhận", r.startswith("Xoá vĩnh viễn 2 mục"), r)
            shot(wv, "files-delete-confirm")
            js(wv, "document.getElementById('mCancel').click()")
            r = js(wv, "[...document.querySelectorAll('.row')].find(x=>x.textContent.includes('README')).click(); document.getElementById('aView').click(); 'ok'", 1.5)
            r = js(wv, "JSON.stringify({shown: getComputedStyle(document.getElementById('viewer')).display, text: document.getElementById('vText').textContent.slice(0,40)})")
            check("files: xem nhanh file text", r["shown"] == "flex" and "Ứng dụng demo" in r["text"], r)
            shot(wv, "files-quick-view")
            js(wv, "document.getElementById('vClose').click()")
            r = js(wv, "[...document.querySelectorAll('.row')].find(x=>x.textContent.includes('current-logs')).dispatchEvent(new MouseEvent('dblclick',{bubbles:true})); 'ok'", 1.5)
            r = js(wv, "JSON.stringify({path: document.getElementById('path').value, rows: [...document.querySelectorAll('.row .nm')].map(x=>x.textContent)})")
            check("files: vào symlink tới thư mục", r["path"] == "/srv/app/logs" and r["rows"] == ["app.log"], r)
            r = js(wv, "document.getElementById('bUp').click(); 'ok'", 1.5)
            r = js(wv, "document.getElementById('bMkdir').click(); document.getElementById('mInput').value='thư mục mới'; document.getElementById('mOk').click(); 'ok'", 1.5)
            r = js(wv, "JSON.stringify([...document.querySelectorAll('.row .nm')].map(x=>x.textContent))")
            check("files: tạo thư mục qua modal", "thư mục mới" in r, r)
            # upload qua DataTransfer giả (giống kéo thả từ Finder, gồm cả file tiếng Việt)
            r = js(wv, "const dt=new DataTransfer(); dt.items.add(new File(['xin chào'], 'tải lên.txt')); dt.items.add(new File(['b'], 'b c.txt')); document.getElementById('list').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt})); 'ok'", 2.5)
            r = js(wv, "JSON.stringify({rows: [...document.querySelectorAll('.row .nm')].map(x=>x.textContent), status: document.getElementById('statusText').textContent})")
            check("files: thả file để upload (DataTransfer)", "tải lên.txt" in r["rows"] and "b c.txt" in r["rows"], r)
            shot(wv, "files-after-upload")
        win.close()

    n_fail = sum(not ok for _, ok in RESULTS)
    print(f"\n{len(RESULTS) - n_fail}/{len(RESULTS)} PASS — ảnh ở {OUT}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
