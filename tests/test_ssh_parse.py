import shlex

import pytest

from xshell_panels import ApiError
from xshell_panels.ssh_parse import (
    Target, build_ssh_argv, build_ssh_command, hop_options, parse_jump, parse_ssh_G,
    parse_ssh_command, remote_cd_command, ssh_o_options, target_for_channel, target_from,
)


@pytest.mark.parametrize("cmd,expected", [
    ("ssh web1", Target("web1")),
    ("ssh deploy@web1", Target("web1", "deploy")),
    ("ssh -p 2222 web1", Target("web1", port=2222)),
    ("ssh -p2222 web1", Target("web1", port=2222)),
    ("ssh -l root web1", Target("web1", "root")),
    ("ssh -lroot web1", Target("web1", "root")),
    ("ssh -o Port=2200 web1", Target("web1", port=2200)),
    ("ssh -o 'Port 2200' web1", Target("web1", port=2200)),
    ("ssh -oPort=2200 web1", Target("web1", port=2200)),
    ("ssh -o User=bob web1", Target("web1", "bob")),
    ("ssh -o user=bob web1", Target("web1", "bob")),
    ("ssh -o ProxyJump=bastion web1", Target("web1", jump="bastion")),
    ("ssh -J bastion web1", Target("web1", jump="bastion")),
    ("ssh -J a@b:22,c web1", Target("web1", jump="a@b:22,c")),
    ("ssh ssh://alice@db.example.com:2022", Target("db.example.com", "alice", 2022)),
    ("ssh ssh://db.example.com", Target("db.example.com")),
    ("ssh ssh://db.example.com/", Target("db.example.com")),
    ("ssh -i ~/.ssh/k -p 22 u@h", Target("h", "u", 22, identity="~/.ssh/k")),
    ("/usr/bin/ssh -tt -A -4 h", Target("h")),
    ("ssh -L 8080:localhost:80 -D 1080 -W h2:22 h", Target("h")),
    ("ssh -F /tmp/cfg -E log -c aes128-ctr -m hmac-sha2-256 -b 1.2.3.4 h", Target("h")),
    ("ssh -vvv -C h uptime", Target("h")),
    ("ssh h ls -la /tmp", Target("h")),
    ("ssh -t h 'cd /var && bash'", Target("h")),
    ("ssh -- h", Target("h")),
    ("ssh -l a b@h", Target("h", "b")),           # đích thắng -l
    ("ssh -p 1 h -p 2", Target("h", port=1)),     # tham số sau đích là remote command
    ("ssh user.name@ip-10-0-0-1", Target("ip-10-0-0-1", "user.name")),
    ("ssh a@b@host", Target("host", "a@b")),
])
def test_parse_ssh(cmd, expected):
    assert parse_ssh_command(cmd) == expected


@pytest.mark.parametrize("cmd", [
    "", None, "zsh", "-zsh", "vim ssh", "sshpass -p x ssh h", "ssh", "ssh -p 22", "ssh -v",
    "mosh h", "scp a h:b", "ssh 'unterminated", "sshd -D",
])
def test_parse_not_ssh(cmd):
    assert parse_ssh_command(cmd) is None


def test_parse_bad_port_ignored():
    assert parse_ssh_command("ssh -p abc h") == Target("h")


def test_target_label_and_key():
    t = Target("h", "u", 2222, "j")
    assert t.label == "u@h:2222"
    assert t.key == ("h", "u", 2222, "j", "", ())
    assert t.to_json()["label"] == "u@h:2222"


def test_target_from():
    assert target_from({"alias": "h", "port": "22", "user": "", "jump": "null"}) == Target("h", port=22)
    with pytest.raises(ApiError):
        target_from({"alias": " "})
    with pytest.raises(ApiError):
        target_from({"alias": "h", "port": "x"})


def test_parse_jump():
    assert parse_jump("a,u@b:2222,ssh://c:23") == [Target("a"), Target("b", "u", 2222), Target("c", port=23)]
    assert parse_jump("none") == []
    assert parse_jump("") == []


def test_parse_ssh_G_and_hop_options(tmp_path):
    key = tmp_path / "id"
    key.write_text("x")
    kh = tmp_path / "kh"
    kh.write_text("")
    cfg = parse_ssh_G(
        f"hostname 10.0.0.5\nport 2222\nuser deploy\nidentityfile {key}\n"
        f"identityfile ~/.ssh/does-not-exist\nuserknownhostsfile {kh} {tmp_path}/none\n"
        "proxyjump bastion\nidentityagent none\nhostkeyalias web\nstricthostkeychecking ask\n")
    o = hop_options(cfg, Target("web"))
    assert o["host"] == "10.0.0.5" and o["port"] == 2222 and o["username"] == "deploy"
    assert o["client_keys"] == [str(key)]
    assert o["known_hosts"] == [str(kh)]
    assert o["agent_path"] is None
    assert o["host_key_alias"] == "web"
    assert o["config"] == []
    assert "proxy_command" not in o


def test_hop_options_no_strict():
    o = hop_options(parse_ssh_G("hostname h\nstricthostkeychecking no\n"), Target("h"))
    assert o["known_hosts"] is None
    o = hop_options(parse_ssh_G("hostname h\n"), Target("h"), strict=False)
    assert o["known_hosts"] is None


def test_hop_options_proxycommand():
    o = hop_options(parse_ssh_G("hostname 1.2.3.4\nport 22\nuser u\nproxycommand nc -X 5 -x p:1080 %h %p\n"),
                    Target("h"))
    assert o["proxy_command"] == "nc -X 5 -x p:1080 1.2.3.4 22"


# ------------------------------------------------------- dựng lệnh từ kênh
def test_build_simple():
    assert build_ssh_command({"host": "web1"}) == "ssh web1"
    assert build_ssh_command({"host": "web1", "user": "deploy", "port": 22}) == "ssh deploy@web1"


def test_build_full():
    ch = {"host": "h", "user": "u", "port": 2222, "identity_file": "~/.ssh/my key",
          "proxy_jump": "bastion", "extra_args": "-L 8080:localhost:80 -A"}
    argv = build_ssh_argv(ch)
    assert argv == ["ssh", "-p", "2222", "-i", "~/.ssh/my key", "-J", "bastion",
                    "-L", "8080:localhost:80", "-A", "u@h"]
    assert shlex.split(build_ssh_command(ch)) == argv


def test_build_remote_dir_escaping():
    ch = {"host": "h", "remote_dir": "/var/www/my app; rm -rf /"}
    argv = build_ssh_argv(ch)
    assert argv[:3] == ["ssh", "-t", "h"]
    assert argv[3] == "cd '/var/www/my app; rm -rf /' && exec $SHELL -l"
    # Lệnh gửi vào shell local tách đúng lại thành argv
    assert shlex.split(build_ssh_command(ch)) == argv
    # Và phía remote: shell remote tách lệnh thành cd + đối số đã quote
    assert shlex.split(argv[3])[:2] == ["cd", "/var/www/my app; rm -rf /"]


def test_remote_cd_tilde():
    assert remote_cd_command("~") == "cd ~ && exec $SHELL -l"
    assert remote_cd_command("~/my app") == "cd ~/'my app' && exec $SHELL -l"
    assert remote_cd_command("/srv") == "cd /srv && exec $SHELL -l"


def test_build_unicode_and_quotes():
    ch = {"host": "h", "remote_dir": "/data/Kho vận/it's"}
    argv = build_ssh_argv(ch)
    assert shlex.split(build_ssh_command(ch)) == argv
    assert shlex.split(argv[-1])[1] == "/data/Kho vận/it's"


def test_target_for_channel():
    assert target_for_channel({"host": "a", "port": 22, "source": "ssh_config"}) == Target("a")
    assert target_for_channel({"host": "a", "port": 2222, "user": "u", "proxy_jump": "b",
                               "identity_file": "k", "source": "manual"}) == Target("a", "u", 2222, "b", "k")


def test_ssh_o_options():
    assert ssh_o_options("-L 1:a:2 -o UserKnownHostsFile=/x/kh -oStrictHostKeyChecking=yes -A") == \
        ("UserKnownHostsFile=/x/kh", "StrictHostKeyChecking=yes")
    assert ssh_o_options("'bad") == ()
    t = target_for_channel({"host": "h", "extra_args": "-o 'User=bob'"})
    assert t.options == ("User=bob",)
