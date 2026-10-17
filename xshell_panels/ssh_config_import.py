"""Đọc ~/.ssh/config để lấy danh sách alias Host cụ thể (bỏ wildcard), hỗ trợ Include."""
from __future__ import annotations

import glob
import os
import shlex
from dataclasses import dataclass, field

MAX_INCLUDE_DEPTH = 16


@dataclass
class HostEntry:
    alias: str
    file: str
    line: int
    # Chỉ để hiển thị gợi ý trên UI; không lưu vào kênh (để ssh tự đọc config lúc kết nối).
    hostname: str | None = None
    user: str | None = None
    port: str | None = None


@dataclass
class ConfigScan:
    hosts: list[HostEntry] = field(default_factory=list)
    files: list[str] = field(default_factory=list)      # mọi file đã đọc
    globs: list[str] = field(default_factory=list)      # pattern Include (để theo dõi file mới)
    errors: list[str] = field(default_factory=list)

    @property
    def aliases(self) -> list[str]:
        return [h.alias for h in self.hosts]


def _split_line(line: str):
    """'Key value', 'Key=value', 'Key = "a b"' -> ('key', ['value', ...]). None nếu trống/comment."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    # Tách keyword: đến khoảng trắng hoặc '='.
    i = 0
    while i < len(s) and not s[i].isspace() and s[i] != "=":
        i += 1
    key = s[:i].lower()
    rest = s[i:].lstrip()
    if rest.startswith("="):
        rest = rest[1:].lstrip()
    try:
        args = shlex.split(rest, comments=True)
    except ValueError:
        args = rest.split()
    return key, args


def is_concrete(pattern: str) -> bool:
    return not any(c in pattern for c in "*?!") and pattern != ""


def _resolve_include(arg: str, base_dir: str) -> str:
    p = os.path.expanduser(arg)
    if not os.path.isabs(p):
        # man ssh_config: đường dẫn tương đối của user config tính từ ~/.ssh
        p = os.path.join(base_dir, p)
    return p


def scan(path: str | None = None, ssh_dir: str | None = None) -> ConfigScan:
    path = os.path.expanduser(path or "~/.ssh/config")
    ssh_dir = os.path.expanduser(ssh_dir or os.path.dirname(path) or "~/.ssh")
    out = ConfigScan()
    seen_alias: set[str] = set()
    seen_files: set[str] = set()

    def read(file: str, depth: int):
        if depth > MAX_INCLUDE_DEPTH:
            out.errors.append(f"Include quá sâu: {file}")
            return
        real = os.path.realpath(file)
        if real in seen_files:
            return
        seen_files.add(real)
        try:
            with open(file, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            out.errors.append(f"{file}: {e.strerror}")
            return
        out.files.append(file)
        current: list[HostEntry] = []
        for n, raw in enumerate(lines, 1):
            parsed = _split_line(raw)
            if not parsed:
                continue
            key, args = parsed
            if key == "host":
                current = []
                for a in args:
                    if not is_concrete(a):
                        continue
                    if a in seen_alias:
                        continue        # ssh dùng block đầu tiên khớp; giữ lần xuất hiện đầu
                    seen_alias.add(a)
                    h = HostEntry(a, file, n)
                    out.hosts.append(h)
                    current.append(h)
            elif key == "match":
                current = []
            elif key == "include":
                for a in args:
                    pat = _resolve_include(a, ssh_dir)
                    out.globs.append(pat)
                    for inc in sorted(glob.glob(pat)):
                        if os.path.isfile(inc):
                            read(inc, depth + 1)
            elif current and args:
                for h in current:
                    if key == "hostname" and h.hostname is None:
                        h.hostname = args[0]
                    elif key == "user" and h.user is None:
                        h.user = args[0]
                    elif key == "port" and h.port is None:
                        h.port = args[0]

    if os.path.exists(path):
        read(path, 0)
    return out


def signature(path: str | None = None, ssh_dir: str | None = None, last: ConfigScan | None = None):
    """Dấu hiệu thay đổi: mtime/size của mọi file liên quan + danh sách file khớp các glob Include."""
    path = os.path.expanduser(path or "~/.ssh/config")
    files = [path] + (last.files if last else [])
    sig = []
    for f in dict.fromkeys(files):
        try:
            st = os.stat(f)
            sig.append((f, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((f, None, None))
    for pat in (last.globs if last else []):
        sig.append(("glob", pat, tuple(sorted(glob.glob(pat)))))
    return tuple(sig)
