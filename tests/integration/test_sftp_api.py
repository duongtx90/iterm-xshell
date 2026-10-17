import asyncio
import hashlib
import os

import aiohttp
import pytest

from conftest import PASSWORD, TOKEN, tgt
from xshell_panels.web import POOL_KEY

NAMES = ["file with space.txt", "Kho vận — báo cáo.txt", "ổ đĩa'quote\".log"]


def seed(root):
    (root / "dir_a").mkdir()
    (root / "dir_a" / "inner.txt").write_text("inner")
    for n in NAMES:
        (root / n).write_text(n)
    (root / ".hidden").write_text("h")
    os.symlink("dir_a", root / "link_to_dir")
    os.symlink("nowhere", root / "broken_link")
    (root / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)


def form(files, rel=None):
    fd = aiohttp.FormData()
    for name, data, r in files:
        if r:
            fd.add_field("rel", r)
        fd.add_field("name", name)        # như UI gửi
        fd.add_field("file", data, filename=name, content_type="application/octet-stream")
    return fd


async def test_auth_token(env):
    r = await env.client.get("/api/list", params=tgt("box"))
    assert r.status == 403
    r = await env.client.get("/files")
    assert r.status == 403
    r = await env.client.get("/files", params={"t": TOKEN})
    assert r.status == 200 and "Xftp" in await r.text()
    r = await env.client.get("/sessions", params={"t": TOKEN})
    assert r.status == 200 and "Kênh SSH" in await r.text()


async def test_list(env):
    seed(env.box.root)
    st, d = await env.get("/api/list", **tgt("box"), path="")
    assert st == 200, d
    assert d["path"] == "/"
    by = {e["name"]: e for e in d["entries"]}
    assert set(NAMES) <= set(by)
    assert by["dir_a"]["dir"] and not by["dir_a"]["link"]
    assert by["link_to_dir"]["link"] and by["link_to_dir"]["dir"]
    assert by["broken_link"].get("broken")
    assert by[NAMES[0]]["size"] == len(NAMES[0])
    st, d = await env.get("/api/list", **tgt("box"), path="/link_to_dir")
    assert st == 200 and [e["name"] for e in d["entries"]] == ["inner.txt"]
    st, d = await env.get("/api/list", **tgt("box"), path="~")
    assert st == 200 and d["path"] == "/"
    st, d = await env.get("/api/list", **tgt("box"), path="/nope")
    assert st == 404 and d["code"] == "nofile"


async def test_upload_download_unicode(env):
    files = [(n, ("nội dung " + n).encode(), None) for n in NAMES]
    r = await env.client.post("/api/upload", params={**tgt("box"), "path": "/", "overwrite": "1"},
                              data=form(files), headers=env.H)
    assert r.status == 200, await r.text()
    assert (await r.json())["uploaded"] == 3
    for n in NAMES:
        assert (env.box.root / n).read_text() == "nội dung " + n
    # tải về: tự đổi tên khi trùng
    for _ in range(2):
        st, d = await env.post("/api/download", {"target": tgt("box"), "dir": "/", "names": [NAMES[1]],
                                                 "reveal": False})
        assert st == 200, d
    got = sorted(p.name for p in env.downloads.iterdir())
    assert got == sorted([NAMES[1], "Kho vận — báo cáo (1).txt"])


async def test_upload_overwrite_guard_and_folder(env):
    (env.box.root / "exists.txt").write_text("old")
    r = await env.client.post("/api/upload", params={**tgt("box"), "path": "/"},
                              data=form([("exists.txt", b"new", None)]), headers=env.H)
    assert r.status == 409 and (await r.json())["code"] == "exists"
    assert (env.box.root / "exists.txt").read_text() == "old"
    # upload cả thư mục (rel = đường dẫn tương đối)
    r = await env.client.post("/api/upload", params={**tgt("box"), "path": "/", "overwrite": "1"},
                              data=form([("a.txt", b"A", "proj"), ("b.txt", b"B", "proj/sub dir/sâu")]),
                              headers=env.H)
    assert r.status == 200, await r.text()
    assert (env.box.root / "proj" / "a.txt").read_bytes() == b"A"
    assert (env.box.root / "proj" / "sub dir" / "sâu" / "b.txt").read_bytes() == b"B"


@pytest.mark.parametrize("rel,name", [("../escape", "x"), ("a/../../x", "x"), ("/abs", "x"),
                                      ("", ".."), ("", "../../etc/passwd")])
async def test_upload_path_traversal(env, rel, name):
    r = await env.client.post("/api/upload", params={**tgt("box"), "path": "/", "overwrite": "1"},
                              data=form([(name, b"evil", rel)]), headers=env.H)
    if name == "../../etc/passwd" and not rel:
        # basename -> 'passwd' trong thư mục hiện tại, không thoát ra ngoài
        assert r.status == 200
        assert (env.box.root / "passwd").read_bytes() == b"evil"
    elif rel == "/abs":
        # '/abs' bị strip thành 'abs' (tương đối) -> nằm trong thư mục đích
        assert r.status == 200 and (env.box.root / "abs" / "x").exists()
    else:
        assert r.status == 400
    assert not (env.tmp / "escape").exists() and not (env.tmp / "x").exists()


async def test_mkdir_rename_delete(env):
    seed(env.box.root)
    st, _ = await env.post("/api/mkdir", {"target": tgt("box"), "dir": "/", "name": "thư mục mới"})
    assert st == 200 and (env.box.root / "thư mục mới").is_dir()
    st, d = await env.post("/api/mkdir", {"target": tgt("box"), "dir": "/", "name": "../x"})
    assert st == 200 and (env.box.root / "x").is_dir()          # basename
    st, d = await env.post("/api/mkdir", {"target": tgt("box"), "dir": "/", "name": ".."})
    assert st == 400
    st, _ = await env.post("/api/rename", {"target": tgt("box"), "dir": "/", "old": NAMES[0], "new": "đổi tên.txt"})
    assert st == 200 and (env.box.root / "đổi tên.txt").exists()
    st, d = await env.post("/api/rename", {"target": tgt("box"), "dir": "/", "old": "đổi tên.txt", "new": NAMES[1]})
    assert st == 409 and d["code"] == "exists"
    # xoá symlink tới thư mục chỉ xoá link, không xoá nội dung thư mục thật
    st, _ = await env.post("/api/delete", {"target": tgt("box"), "dir": "/", "names": ["link_to_dir"]})
    assert st == 200 and not os.path.lexists(env.box.root / "link_to_dir")
    assert (env.box.root / "dir_a" / "inner.txt").exists()
    st, _ = await env.post("/api/delete", {"target": tgt("box"), "dir": "/", "names": ["dir_a", "đổi tên.txt"]})
    assert st == 200 and not (env.box.root / "dir_a").exists()


async def test_download_folder_and_symlink_dir(env):
    seed(env.box.root)
    st, d = await env.post("/api/download", {"target": tgt("box"), "dir": "/", "names": ["dir_a", "link_to_dir"],
                                             "reveal": False})
    assert st == 200, d
    assert (env.downloads / "dir_a" / "inner.txt").read_text() == "inner"
    assert (env.downloads / "link_to_dir" / "inner.txt").read_text() == "inner"


async def test_view(env):
    seed(env.box.root)
    st, d = await env.get("/api/view", **tgt("box"), path="/" + NAMES[1])
    assert st == 200 and d["text"] == NAMES[1] and not d["truncated"]
    st, d = await env.get("/api/view", **tgt("box"), path="/bin.dat")
    assert st == 415 and d["code"] == "binary"
    st, d = await env.get("/api/view", **tgt("box"), path="/dir_a")
    assert st == 400


async def test_cd(env):
    st, _ = await env.post("/api/cd", {"session_id": "s1", "path": "/var/www"})
    assert st == 200 and env.ctx.cds == [("s1", "/var/www")]


async def test_reconnect_after_connection_lost(env):
    seed(env.box.root)
    st, _ = await env.get("/api/list", **tgt("box"), path="/")
    assert st == 200
    n_before = len(env.box.conns)
    env.box.drop_all()
    await asyncio.sleep(0.2)
    st, d = await env.get("/api/list", **tgt("box"), path="/dir_a")
    assert st == 200, d
    assert [e["name"] for e in d["entries"]] == ["inner.txt"]
    assert len(env.box.conns) >= 1 and n_before >= 1


async def test_reconnect_stale_connection_mid_operation(env):
    """Kết nối chết mà client chưa biết (không nhận được FIN) -> lỗi -> nối lại và thử lại."""
    seed(env.box.root)
    st, _ = await env.get("/api/list", **tgt("box"), path="/")
    assert st == 200
    pool = env.app[POOL_KEY]
    entry = next(iter(pool._entries.values()))
    real = entry.sftp.readdir
    calls = {"n": 0}

    async def flaky(path):
        calls["n"] += 1
        from asyncssh import SFTPConnectionLost
        raise SFTPConnectionLost("gone")
    entry.sftp.readdir = flaky
    st, d = await env.get("/api/list", **tgt("box"), path="/dir_a")
    assert st == 200, d
    assert calls["n"] == 1 and entry.sftp.readdir is flaky and real


async def test_password_only_server(env):
    st, d = await env.get("/api/list", **tgt("pwbox"), path="/")
    assert st == 401 and d["code"] == "auth"
    st, _ = await env.post("/api/password", {"target": tgt("pwbox"), "password": "sai"})
    assert st == 200
    st, d = await env.get("/api/list", **tgt("pwbox"), path="/")
    assert st == 401 and d["code"] == "auth" and d["error"] == "Sai mật khẩu"
    st, _ = await env.post("/api/password", {"target": tgt("pwbox"), "password": PASSWORD})
    st, d = await env.get("/api/list", **tgt("pwbox"), path="/")
    assert st == 200, d


async def test_unknown_host_key(env):
    st, d = await env.get("/api/list", **tgt("unknownkey"), path="/")
    assert st == 400 and d["code"] == "hostkey"
    assert "known_hosts" in d["error"]


async def test_connection_refused(env):
    st, d = await env.get("/api/list", **tgt("refused"), path="/")
    assert st == 502 and d["code"] == "connect"


async def test_proxyjump(env):
    seed(env.box.root)
    st, d = await env.get("/api/list", **tgt("behind"), path="/")
    assert st == 200, d
    assert "dir_a" in {e["name"] for e in d["entries"]}
    assert len(env.jump.conns) == 1        # thực sự đi qua máy jump


async def test_upload_cancel_removes_partial(env):
    """Huỷ upload giữa chừng (client ngắt kết nối) -> file dở dang bị xoá trên server."""
    url = env.client.make_url("/api/upload")
    boundary = "XSHELLBOUNDARY"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()

    async def gen():
        yield head
        for _ in range(20):
            yield b"x" * (256 * 1024)
            await asyncio.sleep(0.02)
        raise ConnectionAbortedError("user cancelled")

    async with aiohttp.ClientSession() as s:
        with pytest.raises(Exception):
            await s.post(url, params={**tgt("box"), "path": "/", "overwrite": "1"}, data=gen(),
                         headers={**env.H, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    for _ in range(50):
        await asyncio.sleep(0.1)
        if not (env.box.root / "big.bin").exists():
            break
    assert not (env.box.root / "big.bin").exists()


@pytest.mark.slow
async def test_large_file_roundtrip(env):
    size = int(os.environ.get("XSHELL_BIG_MB", "300")) * 1024 * 1024
    src = env.tmp / "big-src.bin"
    h = hashlib.sha256()
    block = os.urandom(1024 * 1024)
    with open(src, "wb") as f:
        for i in range(size // len(block)):
            b = block[:-8] + i.to_bytes(8, "big")
            f.write(b)
            h.update(b)
    digest = h.hexdigest()
    with open(src, "rb") as f:
        fd = aiohttp.FormData()
        fd.add_field("name", "lớn file.bin")
        fd.add_field("file", f, filename="lớn file.bin", content_type="application/octet-stream")
        r = await env.client.post("/api/upload", params={**tgt("box"), "path": "/", "overwrite": "1"},
                                  data=fd, headers=env.H, timeout=aiohttp.ClientTimeout(total=600))
        assert r.status == 200, await r.text()
    remote = env.box.root / "lớn file.bin"
    assert remote.stat().st_size == size

    def sha(p):
        hh = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(4 << 20), b""):
                hh.update(chunk)
        return hh.hexdigest()
    assert sha(remote) == digest
    st, d = await env.post("/api/download", {"target": tgt("box"), "dir": "/", "names": ["lớn file.bin"],
                                             "reveal": False})
    assert st == 200, d
    assert sha(env.downloads / "lớn file.bin") == digest


async def test_download_background_job_progress(env):
    import asyncio
    data = os.urandom(3 * 1024 * 1024)
    (env.box.root / "big.bin").write_bytes(data)
    (env.box.root / "d").mkdir()
    (env.box.root / "d" / "x.bin").write_bytes(data[:1000])
    st, d = await env.post("/api/download", {"target": tgt("box"), "dir": "/", "names": ["big.bin", "d"],
                                             "reveal": False, "background": True})
    assert st == 200 and d["job"]["state"] == "running", d
    for _ in range(100):
        st, d = await env.get("/api/jobs")
        job = d["jobs"][0]
        if job["state"] != "running":
            break
        await asyncio.sleep(0.1)
    assert job["state"] == "done", job
    assert job["total"] == job["done"] == len(data) + 1000
    assert (env.downloads / "big.bin").read_bytes() == data
    st, _ = await env.post("/api/jobs/dismiss", {"id": job["id"]})
    st, d = await env.get("/api/jobs")
    assert d["jobs"] == []


async def test_upload_raw_filename_like_browser(env):
    """Không có field name: dùng filename thô (WebKit gửi UTF-8 không percent-encode)."""
    fd = aiohttp.FormData(quote_fields=False)
    fd.add_field("file", b"raw", filename="tên thô có dấu.txt", content_type="text/plain")
    r = await env.client.post("/api/upload", params={**tgt("box"), "path": "/", "overwrite": "1"},
                              data=fd, headers=env.H)
    assert r.status == 200, await r.text()
    assert (env.box.root / "tên thô có dấu.txt").read_bytes() == b"raw"


async def test_channel_target_uses_extra_o_options(env):
    """Kênh tay: host/port/key nằm trong kênh, known_hosts truyền qua extra_args -o."""
    seed(env.box.root)
    kh = env.tmp / "known_hosts"
    key = env.tmp / "id_test"
    ch = env.store.add({"name": "tay", "host": "127.0.0.1", "port": env.box.port, "user": "tester",
                        "identity_file": str(key),
                        "extra_args": f"-o UserKnownHostsFile={kh} -o IdentityAgent=none"})
    # Không có -o thì known_hosts mặc định không có host này -> lỗi hostkey
    st, d = await env.get("/api/list", alias="127.0.0.1", port=str(env.box.port), user="tester",
                          identity=str(key), path="/")
    assert st in (400, 401, 502) and d.get("code") in ("hostkey", "auth", "connect"), d
    st, d = await env.get("/api/list", alias="127.0.0.1", channel=ch["id"], path="/")
    assert st == 200, d
    assert "dir_a" in {e["name"] for e in d["entries"]}
