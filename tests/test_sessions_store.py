import json
import os
import stat

import pytest

from xshell_panels import sessions_store as ss
from xshell_panels.sessions_store import SessionStore, ValidationError


@pytest.fixture
def store(tmp_path):
    return SessionStore(str(tmp_path / "cfg" / "sessions.json"))


def test_add_update_delete(store):
    ch = store.add({"name": "Web", "host": "web1", "port": "2222", "user": "u",
                    "tags": "prod, web", "color": "red", "group": "Production"})
    assert ch["port"] == 2222 and ch["tags"] == ["prod", "web"] and ch["source"] == "manual"
    assert store.get(ch["id"])["name"] == "Web"
    up = store.update(ch["id"], {"name": "Web 1", "port": ""})
    assert up["name"] == "Web 1" and up["port"] is None and up["host"] == "web1"
    assert store.delete(ch["id"]) is True
    assert store.delete(ch["id"]) is False
    assert store.list() == []


def test_persisted_and_reloaded(store):
    a = store.add({"name": "Kho vận", "host": "kho"})
    again = SessionStore(store.path)
    assert again.get(a["id"])["name"] == "Kho vận"
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["sessions"][0]["host"] == "kho"


def test_file_permissions(store):
    store.add({"host": "h"})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(store.path)).st_mode) == 0o700


def test_atomic_write_failure_keeps_old(store, monkeypatch):
    store.add({"host": "keep"})
    before = open(store.path).read()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(ss.json, "dump", boom)
    with pytest.raises(OSError):
        store.add({"host": "new"})
    assert open(store.path).read() == before
    # không để lại file tạm
    assert [f for f in os.listdir(os.path.dirname(store.path)) if f.endswith(".tmp")] == []


def test_atomic_uses_replace(store, monkeypatch):
    calls = []
    real = os.replace
    monkeypatch.setattr(ss.os, "replace", lambda a, b: (calls.append((a, b)), real(a, b)))
    store.add({"host": "h"})
    assert calls and calls[-1][1] == store.path and calls[-1][0] != store.path


def test_corrupt_file_backed_up(tmp_path):
    p = tmp_path / "sessions.json"
    p.write_text("{not json")
    s = SessionStore(str(p))
    assert s.list() == []
    assert any(f.startswith("sessions.json.broken-") for f in os.listdir(tmp_path))


@pytest.mark.parametrize("bad", [
    {"host": ""}, {"host": "a b"}, {"host": "-oProxyCommand=x"}, {"host": "h", "port": "0x"},
    {"host": "h", "port": 70000}, {"host": "h", "user": "-x"}, {"host": "h", "extra_args": "'unterminated"},
])
def test_validation(store, bad):
    with pytest.raises(ValidationError):
        store.add(bad)


def test_name_defaults_to_host_and_bad_color(store):
    ch = store.add({"host": "h", "color": "hotpink"})
    assert ch["name"] == "h" and ch["color"] == ""


def test_duplicate(store):
    a = store.add({"name": "A", "host": "h"})
    b = store.duplicate(a["id"])
    assert b["id"] != a["id"] and b["name"] == "A (bản sao)" and b["host"] == "h"


def test_sync_keeps_manual_and_marks_missing(store):
    m = store.add({"name": "manual web1", "host": "web1"})
    r = store.sync_ssh_config(["web1", "db"])
    assert r == {"added": ["web1", "db"], "missing": [], "restored": []}
    chs = store.list()
    assert len(chs) == 3
    cfg = {c["host"]: c for c in chs if c["source"] == "ssh_config"}
    # Không copy HostName/Port vào kênh import
    assert cfg["db"]["port"] is None and cfg["db"]["user"] == ""
    # Người dùng đặt nhóm/màu cho kênh import -> giữ nguyên khi đồng bộ
    store.update(cfg["db"]["id"], {"group": "Production", "color": "red", "host": "evil"})
    assert store.get(cfg["db"]["id"])["host"] == "db"      # alias không sửa được
    r = store.sync_ssh_config(["web1"])
    assert r == {"added": [], "missing": ["db"], "restored": []}
    db = store.get(cfg["db"]["id"])
    assert db["missing"] is True and db["group"] == "Production"
    assert store.get(m["id"])["missing"] is False
    # Đồng bộ lại không tạo trùng và khôi phục alias quay lại
    assert store.sync_ssh_config(["web1"]) == {"added": [], "missing": [], "restored": []}
    r = store.sync_ssh_config(["web1", "db"])
    assert r["restored"] == ["db"] and store.get(cfg["db"]["id"])["missing"] is False
    assert len(store.list()) == 3


def test_sync_empty_config_never_deletes(store):
    store.add({"host": "m"})
    store.sync_ssh_config(["a", "b"])
    store.sync_ssh_config([])
    chs = store.list()
    assert len(chs) == 3 and sum(c["missing"] for c in chs) == 2


def test_settings(store):
    assert store.settings["download_dir"] == "~/Downloads"
    store.update_settings(collapsed_groups=["x"], unknown=1)
    assert SessionStore(store.path).settings["collapsed_groups"] == ["x"]
    assert "unknown" not in store.settings
