import os
import time

from xshell_panels.config_sync import SshConfigSync


async def test_crud_and_open(env):
    st, ch = await env.post("/api/sessions", {"name": "Web", "host": "web1", "port": "2222",
                                              "remote_dir": "/srv/my app", "group": "Production"})
    assert st == 200, ch
    st, d = await env.get("/api/sessions")
    assert st == 200
    s = d["sessions"][0]
    assert s["command"] == "ssh -p 2222 -t web1 'cd '\"'\"'/srv/my app'\"'\"' && exec $SHELL -l'"
    assert s["open"] == 0
    env.ctx.open_map = {ch["id"]: ["sess-a", "sess-b"]}
    st, d = await env.get("/api/sessions")
    assert d["sessions"][0]["open"] == 2
    st, d = await env.post(f"/api/sessions/{ch['id']}", {"name": "Web 1"}, method="PUT")
    assert st == 200 and d["name"] == "Web 1" and d["port"] == 2222
    st, d = await env.post(f"/api/sessions/{ch['id']}", {"host": "bad host"}, method="PUT")
    assert st == 400 and d["code"] == "invalid"
    st, dup = await env.post(f"/api/sessions/{ch['id']}/duplicate", {})
    assert st == 200 and dup["name"] == "Web 1 (bản sao)"
    st, d = await env.post(f"/api/sessions/{ch['id']}/open", {"mode": "vsplit"})
    assert st == 200 and env.ctx.opened == [(ch["id"], "vsplit")]
    st, d = await env.post(f"/api/sessions/{ch['id']}", {}, method="DELETE")
    assert st == 200
    st, d = await env.post("/api/sessions/nope/open", {"mode": "tab"})
    assert st == 404


async def test_ui_state(env):
    st, _ = await env.post("/api/ui", {"collapsed_groups": ["Production"]})
    st, d = await env.get("/api/sessions")
    assert d["settings"]["collapsed_groups"] == ["Production"]


async def test_ssh_config_sync_via_watcher(env, tmp_path):
    cfg = tmp_path / "user_ssh_config"
    cfg.write_text("Host alpha beta\nHost *\n  User x\n")
    sync = SshConfigSync(env.store, str(cfg))
    sync.sync_now()
    # gắn watcher vào app mới để test endpoint /api/sessions/sync
    from aiohttp.test_utils import TestClient, TestServer
    from xshell_panels.web import make_app
    app = make_app(ctx=env.ctx, store=env.store, token="t2", watcher=sync)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        H = {"X-Token": "t2"}
        hosts = lambda d: sorted((c["host"], c["missing"]) for c in d["sessions"])  # noqa: E731
        d = await (await client.get("/api/sessions", headers=H)).json()
        assert hosts(d) == [("alpha", False), ("beta", False)]
        # thêm kênh tay + sửa config
        await client.post("/api/sessions", json={"host": "manual-host"}, headers=H)
        cfg.write_text("Host alpha gamma\n")
        os.utime(cfg, ns=(time.time_ns(), time.time_ns() + 10**9))
        r = await (await client.post("/api/sessions/sync", json={}, headers=H)).json()
        assert r == {"added": ["gamma"], "missing": ["beta"], "restored": []}
        d = await (await client.get("/api/sessions", headers=H)).json()
        assert hosts(d) == [("alpha", False), ("beta", True), ("gamma", False), ("manual-host", False)]
        # vòng watcher tự phát hiện thay đổi theo mtime
        cfg.write_text("Host alpha beta gamma\n")
        os.utime(cfg, ns=(time.time_ns(), time.time_ns() + 2 * 10**9))
        assert (await sync.sync())["restored"] == ["beta"]
        assert (await sync.sync()) == {"added": [], "missing": [], "restored": []}
    finally:
        await client.close()


async def test_open_missing_channel_blocked_in_ui_layer(env):
    # Kênh mất khỏi config vẫn hiện (không bị xoá) và có cờ missing
    env.store.sync_ssh_config(["gone"])
    env.store.sync_ssh_config([])
    st, d = await env.get("/api/sessions")
    assert [c["missing"] for c in d["sessions"]] == [True]
