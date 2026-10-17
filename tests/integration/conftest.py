"""SFTP server thật (asyncssh, chạy trong process test) + aiohttp test client cho web app."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import asyncssh
import pytest
from aiohttp.test_utils import TestClient, TestServer

from xshell_panels.sessions_store import SessionStore
from xshell_panels.web import make_app

TOKEN = "test-token"
PASSWORD = "mật-khẩu đúng"


@dataclass
class Sshd:
    port: int
    root: Path
    host_key: asyncssh.SSHKey
    acceptor: object
    conns: list = field(default_factory=list)

    def drop_all(self):
        """Giả lập mất kết nối: server cắt mọi kết nối đang mở."""
        for c in list(self.conns):
            c.abort()
        self.conns.clear()


def _server_class(conns, password_only, allow_forward):
    class Server(asyncssh.SSHServer):
        def connection_made(self, conn):
            conns.append(conn)

        def connection_lost(self, exc):
            pass

        def begin_auth(self, username):
            return True

        def public_key_auth_supported(self):
            return not password_only

        def password_auth_supported(self):
            return password_only

        def validate_password(self, username, password):
            return password == PASSWORD

        def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
            return allow_forward
    return Server


async def start_sshd(tmp: Path, name: str, client_pub: str | None, password_only=False,
                     allow_forward=False) -> Sshd:
    root = tmp / f"remote-{name}"
    root.mkdir()
    hk = asyncssh.generate_private_key("ssh-ed25519")
    conns: list = []
    kw = {}
    if client_pub:
        kw["authorized_client_keys"] = asyncssh.import_authorized_keys(client_pub)

    def sftp_factory(chan):
        return asyncssh.SFTPServer(chan, chroot=str(root))

    acceptor = await asyncssh.listen(
        "127.0.0.1", 0, server_host_keys=[hk], sftp_factory=sftp_factory,
        server_factory=_server_class(conns, password_only, allow_forward), **kw)
    port = acceptor.sockets[0].getsockname()[1]
    return Sshd(port, root, hk, acceptor, conns)


@pytest.fixture
async def env(tmp_path):
    """Ba server: `box` (key), `pwbox` (chỉ mật khẩu), `jump` (cho phép forward) + ssh config."""
    ck = asyncssh.generate_private_key("ssh-ed25519")
    key = tmp_path / "id_test"
    key.write_bytes(ck.export_private_key())
    key.chmod(0o600)
    pub = ck.export_public_key().decode()
    box = await start_sshd(tmp_path, "box", pub)
    pwbox = await start_sshd(tmp_path, "pw", None, password_only=True)
    jump = await start_sshd(tmp_path, "jump", pub, allow_forward=True)
    kh = tmp_path / "known_hosts"
    kh.write_text("".join(
        f"[127.0.0.1]:{s.port} {s.host_key.export_public_key().decode()}"
        for s in (box, pwbox, jump)))
    empty_kh = tmp_path / "known_hosts_empty"
    empty_kh.write_text("")
    common = (f"  User tester\n  IdentityFile {key}\n  IdentitiesOnly yes\n"
              f"  IdentityAgent none\n  GlobalKnownHostsFile /dev/null\n")
    cfg = tmp_path / "ssh_config"
    cfg.write_text(
        f"Host box\n  HostName 127.0.0.1\n  Port {box.port}\n  UserKnownHostsFile {kh}\n{common}"
        f"Host pwbox\n  HostName 127.0.0.1\n  Port {pwbox.port}\n  UserKnownHostsFile {kh}\n{common}"
        f"Host jumpbox\n  HostName 127.0.0.1\n  Port {jump.port}\n  UserKnownHostsFile {kh}\n{common}"
        f"Host behind\n  HostName 127.0.0.1\n  Port {box.port}\n  ProxyJump jumpbox\n"
        f"  UserKnownHostsFile {kh}\n{common}"
        f"Host unknownkey\n  HostName 127.0.0.1\n  Port {box.port}\n  UserKnownHostsFile {empty_kh}\n{common}"
        f"Host refused\n  HostName 127.0.0.1\n  Port 1\n  UserKnownHostsFile {kh}\n{common}"
    )
    store = SessionStore(str(tmp_path / "state" / "sessions.json"))
    store.update_settings(ssh_config=str(cfg))
    dl = tmp_path / "Downloads"
    ctx = FakeCtx()
    app = make_app(ctx=ctx, store=store, token=TOKEN, download_dir=str(dl))
    client = TestClient(TestServer(app))
    await client.start_server()
    e = Env(client, box, pwbox, jump, dl, store, ctx, tmp_path, cfg, app)
    yield e
    await client.close()
    for s in (box, pwbox, jump):
        s.acceptor.close()


class FakeCtx:
    def __init__(self):
        self.cds = []
        self.opened = []
        self.open_map = {}

    async def current(self):
        return {"target": None, "path": None, "session_id": None, "channel": None}

    async def cd(self, session_id, path):
        self.cds.append((session_id, path))

    async def open_channels(self):
        return self.open_map

    async def open_channel(self, ch, mode):
        self.opened.append((ch["id"], mode))
        return "sess-1"

    async def focus(self, sid):
        self.focused = sid


@dataclass
class Env:
    client: TestClient
    box: Sshd
    pwbox: Sshd
    jump: Sshd
    downloads: Path
    store: SessionStore
    ctx: FakeCtx
    tmp: Path
    ssh_config: Path
    app: object

    H = {"X-Token": TOKEN}

    async def get(self, url, **params):
        r = await self.client.get(url, params=params, headers=self.H)
        return r.status, await r.json()

    async def post(self, url, json=None, method="POST"):
        r = await self.client.request(method, url, json=json, headers=self.H)
        return r.status, await r.json()


def tgt(alias, **kw):
    d = {"alias": alias}
    d.update(kw)
    return d
