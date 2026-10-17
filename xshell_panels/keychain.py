"""Lưu mật khẩu kênh SSH trong macOS Keychain qua lệnh `security`.

Mật khẩu không bao giờ nằm trên argv: lệnh ghi dùng `security -i` (đọc lệnh từ stdin).
"""
from __future__ import annotations

import asyncio
import subprocess

SERVICE = "iterm-xshell"
SECURITY = "/usr/bin/security"


def _q(s: str) -> str:
    # Cú pháp trích dẫn của `security -i`: chuỗi trong nháy kép, escape \ và ".
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _run(args, stdin: bytes | None = None, timeout=10):
    p = await asyncio.create_subprocess_exec(
        SECURITY, *args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    return p.returncode, out, err


async def set_password(account: str, password: str) -> bool:
    if "\n" in password or "\r" in password:
        raise ValueError("Mật khẩu chứa ký tự xuống dòng")
    cmd = f"add-generic-password -U -s {_q(SERVICE)} -a {_q(account)} -w {_q(password)}\n"
    rc, _, _ = await _run(["-i"], cmd.encode())
    return rc == 0 and await get_password(account) == password


async def get_password(account: str) -> str | None:
    # `-w` in mật khẩu không phải ASCII dưới dạng hex mà không đánh dấu, nên dùng `-g`:
    # stderr có `password: "abc"` hoặc `password: 0x77C3B6...  "w\303\266rd"`.
    rc, _, err = await _run(["find-generic-password", "-s", SERVICE, "-a", account, "-g"])
    if rc != 0:
        return None
    for line in err.decode(errors="replace").splitlines():
        if line.startswith("password: 0x"):
            return bytes.fromhex(line[len("password: 0x"):].split()[0]).decode("utf-8", "replace")
        if line.startswith('password: "') and line.endswith('"'):
            return line[len('password: "'):-1]
        if line.strip() == "password:":
            return ""
    return None


async def delete_password(account: str) -> bool:
    rc, _, _ = await _run(["delete-generic-password", "-s", SERVICE, "-a", account])
    return rc == 0


async def has_password(account: str) -> bool:
    rc, _, _ = await _run(["find-generic-password", "-s", SERVICE, "-a", account])
    return rc == 0
