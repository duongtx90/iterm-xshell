import os
import time

from xshell_panels.ssh_config_import import is_concrete, scan, signature


def w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_multiple_hosts_wildcards_comments(tmp_path):
    cfg = w(tmp_path / "config", """
# comment
Host *
  ServerAliveInterval 30
Host web1 web2 web3   # ba kênh
  HostName 10.0.0.%h
  User deploy
Host db-? !bad *.internal  db-main
  Port 2222
host LowerCase
HOST UpperCase
Host="quoted name"
Host=eq1
Host = eq2 eq3
""")
    s = scan(str(cfg))
    assert s.aliases == ["web1", "web2", "web3", "db-main", "LowerCase", "UpperCase",
                         "quoted name", "eq1", "eq2", "eq3"]
    web1 = s.hosts[0]
    assert web1.user == "deploy" and web1.file == str(cfg)
    assert s.hosts[3].port == "2222"


def test_is_concrete():
    assert is_concrete("web")
    for p in ("*", "web*", "db-?", "!x", ""):
        assert not is_concrete(p)


def test_include_relative_glob_nested(tmp_path):
    ssh = tmp_path / ".ssh"
    w(ssh / "config", "Include config.d/*\nInclude ~/absent\nHost top\n")
    w(ssh / "config.d" / "10-a", "Host a1 a2\nInclude nested/inner\n")
    w(ssh / "config.d" / "20-b", "Host b1\n")
    w(ssh / "nested" / "inner", "Host deep\n  hostname deep.example\n")
    s = scan(str(ssh / "config"), ssh_dir=str(ssh))
    assert s.aliases == ["a1", "a2", "deep", "b1", "top"]
    assert s.hosts[2].hostname == "deep.example"
    assert str(ssh / "nested" / "inner") in s.files


def test_include_absolute_and_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    w(ssh / "config", f"Include {tmp_path}/abs.conf\nInclude ~/.ssh/tilde.conf\n")
    w(tmp_path / "abs.conf", "Host abs\n")
    w(ssh / "tilde.conf", "Host tilde\n")
    assert scan(str(ssh / "config"), ssh_dir=str(ssh)).aliases == ["abs", "tilde"]


def test_include_cycle(tmp_path):
    ssh = tmp_path / ".ssh"
    w(ssh / "config", "Include other\nHost a\n")
    w(ssh / "other", "Include config\nHost b\n")
    assert scan(str(ssh / "config"), ssh_dir=str(ssh)).aliases == ["b", "a"]


def test_duplicate_alias_first_wins(tmp_path):
    cfg = w(tmp_path / "config", "Host a\n User one\nHost a b\n User two\n")
    s = scan(str(cfg))
    assert s.aliases == ["a", "b"]
    assert s.hosts[0].user == "one"


def test_match_block_resets_context(tmp_path):
    cfg = w(tmp_path / "config", "Host a\nMatch host a\n User nope\n")
    assert scan(str(cfg)).hosts[0].user is None


def test_missing_file(tmp_path):
    s = scan(str(tmp_path / "nope"))
    assert s.aliases == [] and s.files == []


def test_signature_changes(tmp_path):
    ssh = tmp_path / ".ssh"
    cfg = w(ssh / "config", "Include conf.d/*\nHost a\n")
    w(ssh / "conf.d" / "x", "Host x\n")
    s = scan(str(cfg), ssh_dir=str(ssh))
    sig = signature(str(cfg), last=s)
    assert signature(str(cfg), last=s) == sig
    # File mới khớp glob Include
    w(ssh / "conf.d" / "y", "Host y\n")
    assert signature(str(cfg), last=s) != sig
    s = scan(str(cfg), ssh_dir=str(ssh))
    sig = signature(str(cfg), last=s)
    # Sửa file include
    time.sleep(0.01)
    w(ssh / "conf.d" / "x", "Host x x2\n")
    os.utime(ssh / "conf.d" / "x", ns=(time.time_ns(), time.time_ns() + 10**9))
    assert signature(str(cfg), last=s) != sig
