import pytest

from xshell_panels.server_info import SECTIONS, build_command, parse


def test_every_section_has_command_and_unknown_rejected():
    for s in SECTIONS:
        assert build_command(s).startswith("sh -c ")
    with pytest.raises(KeyError):
        build_command("rm -rf /")


def test_procs_grouped_by_name():
    out = ("##priv 0\n 10 www 5.0 1.0 1000 nginx\n 11 www 3.0 2.0 2000 nginx\n"
           " 12 root 1.0 0.5 500 sshd\n")
    r = parse("procs", out)
    ng = next(p for p in r["procs"] if p["name"] == "nginx")
    assert ng["count"] == 2 and ng["cpu"] == 8.0 and ng["rss"] == 3000 * 1024 and r["priv"] is False


def test_ports_dedup_and_local_flag():
    out = ("##priv 1\n"
           'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
           'tcp LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=1,fd=4))\n'
           'tcp LISTEN 0 128 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=2,fd=5))\n')
    ports = parse("ports", out)["ports"]
    assert [(p["port"], p["proc"], p["local_only"]) for p in ports] == [
        ("22", "sshd", False), ("5432", "postgres", True)]


def test_firewall_kind_and_text():
    r = parse("firewall", "##priv 1\n##fw ufw\nStatus: active\n22/tcp ALLOW IN Anywhere\n")
    assert r["kind"] == "ufw" and "22/tcp" in r["text"]
    assert parse("firewall", "##priv 0\n##fw none\n")["kind"] == "none"


def test_actions_parse_journal_and_authlog():
    out = ("##priv 1\n"
           "2026-10-22T10:00:00+0700 h sudo[12]: bob : TTY=pts/0 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/apt update\n"
           "Oct 22 10:05:00 h sudo: amy : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/bin/systemctl restart nginx\n")
    a = parse("actions", out)["actions"]
    assert a[0]["user"] == "amy" and a[0]["cmd"].endswith("restart nginx")
    assert a[1]["time"].startswith("2026-10-22 10:00") and a[1]["as"] == "root"


def test_access_sections():
    r = parse("access", "##priv 0\n##who\nbob pts/0\n##last\nbob pts/0 1.2.3.4 Thu\nwtmp begins\n"
              "##failed\n##ssh\nA Accepted x\nB Failed y\n")
    assert r["who"] == ["bob pts/0"] and len(r["last"]) == 1 and r["ssh"][0].startswith("B")
