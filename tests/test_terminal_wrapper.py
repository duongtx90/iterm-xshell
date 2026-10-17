import shlex
import subprocess
import time

from xshell_panels import terminal
from xshell_panels.terminal import is_remote_hostname, wrapped_command


def run(cmd, stdin=""):
    t = time.monotonic()
    p = subprocess.run(shlex.split(cmd), input=stdin, capture_output=True, text=True, timeout=30)
    return p, time.monotonic() - t


def test_wrapper_exit_code_and_min_lifetime(monkeypatch):
    monkeypatch.setattr(terminal, "MIN_SESSION_SECONDS", 2)
    p, dt = run(wrapped_command("sh -c 'exit 3'"))
    assert p.returncode == 3 and dt >= 1.9
    assert "ssh lỗi" not in p.stdout


def test_wrapper_keeps_tab_on_255(monkeypatch):
    monkeypatch.setattr(terminal, "MIN_SESSION_SECONDS", 0)
    p, _ = run(wrapped_command("sh -c 'exit 255'"), stdin="\n")
    assert p.returncode == 255 and "ssh lỗi (mã 255)" in p.stdout


def test_wrapper_quotes_ssh_command(monkeypatch):
    monkeypatch.setattr(terminal, "MIN_SESSION_SECONDS", 0)
    p, _ = run(wrapped_command(shlex.join(["printf", "%s|", "a b", "it's", "$HOME"])))
    assert p.stdout == "a b|it's|$HOME|"


def test_is_remote_hostname():
    import socket
    assert not is_remote_hostname(socket.gethostname())
    assert not is_remote_hostname("localhost")
    assert not is_remote_hostname(None)
    assert is_remote_hostname("prod-web-01")
