"""Parse lệnh ssh, resolve cấu hình bằng `ssh -G`, và dựng lệnh ssh từ một kênh.

Module này không phụ thuộc iTerm2 và asyncssh để test được độc lập.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import shlex
import subprocess
from dataclasses import dataclass, field

from . import ApiError

# iTerm2 chạy script với PATH tối giản (/usr/bin:/bin:...), dùng đường dẫn tuyệt đối.
SSH_BIN = "/usr/bin/ssh" if os.path.exists("/usr/bin/ssh") else "ssh"

# Các option của ssh có tham số (man ssh, OpenSSH 9.x).
_SSH_OPTS_WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")


@dataclass(frozen=True)
class Target:
    """Đích kết nối SFTP. `alias` là thứ người dùng gõ sau `ssh` (alias config hoặc host)."""
    alias: str
    user: str | None = None
    port: int | None = None
    jump: str | None = None
    identity: str | None = None
    options: tuple = ()          # các "-o Key=Value" (từ extra_args của kênh)

    @property
    def key(self):
        return (self.alias, self.user or "", self.port or 0, self.jump or "", self.identity or "",
                self.options)

    @property
    def label(self):
        s = (f"{self.user}@" if self.user else "") + self.alias
        if self.port:
            s += f":{self.port}"
        return s

    def to_json(self):
        return {"alias": self.alias, "user": self.user, "port": self.port,
                "jump": self.jump, "identity": self.identity, "label": self.label}


def _split_dest(dest: str):
    """'user@host:port' / 'ssh://user@host:port' / 'host' -> (host, user, port)."""
    user = port = None
    if dest.startswith("ssh://"):
        dest = dest[6:].rstrip("/")
        if "@" in dest:
            user, dest = dest.rsplit("@", 1)
        if dest.startswith("[") and "]" in dest:          # ssh://[::1]:22
            host, _, rest = dest[1:].partition("]")
            if rest.startswith(":"):
                port = rest[1:]
            dest = host
        elif dest.count(":") == 1:
            dest, port = dest.split(":")
    elif "@" in dest:
        user, dest = dest.rsplit("@", 1)
    return dest, user or None, port


def parse_ssh_command(cmd: str) -> Target | None:
    """'ssh -p 2222 deploy@web1 -i k' -> Target('web1','deploy',2222,identity='k').

    Trả về None nếu không phải lệnh ssh. Phần remote command phía sau đích bị bỏ qua.
    """
    try:
        argv = shlex.split(cmd or "")
    except ValueError:
        return None
    if not argv or os.path.basename(argv[0]) != "ssh":
        return None
    user = port = dest = jump = identity = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            dest = argv[i + 1] if i + 1 < len(argv) else None
            break
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in _SSH_OPTS_WITH_ARG:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = argv[i] if i < len(argv) else ""
                    if c == "l":
                        user = val
                    elif c == "p":
                        port = val
                    elif c == "J":
                        jump = val
                    elif c == "i":
                        identity = val
                    elif c == "o":
                        k, _, v = val.replace("=", " ", 1).partition(" ")
                        k, v = k.strip().lower(), v.strip()
                        if k == "port":
                            port = v
                        elif k == "user":
                            user = v
                        elif k == "proxyjump":
                            jump = v
                        elif k == "identityfile":
                            identity = v
                    break
                j += 1
            i += 1
            continue
        dest = a
        break
    if not dest:
        return None
    host, u, p = _split_dest(dest)
    # User/port trong đích ghi đè -l/-p giống ssh thật (đích thắng).
    user, port = u or user, p or port
    if not host:
        return None
    try:
        port = int(port) if port else None
    except ValueError:
        port = None
    return Target(host, user or None, port, jump or None, identity or None)


def target_from(d) -> Target:
    """Dựng Target từ query/JSON của UI."""
    def opt(k):
        v = d.get(k)
        return v if v not in (None, "", "null", "None") else None
    alias = (d.get("alias") or "").strip()
    if not alias:
        raise ApiError(400, "Chưa chọn host")
    port = opt("port")
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        raise ApiError(400, "Port không hợp lệ")
    return Target(alias, opt("user"), port, opt("jump"), opt("identity"))


# ------------------------------------------------------------------ ssh -G
def parse_ssh_G(text: str) -> dict[str, list[str]]:
    cfg: dict[str, list[str]] = {}
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k:
            cfg.setdefault(k.lower(), []).append(v.strip())
    return cfg


async def ssh_G(t: Target, config_file: str | None = None) -> dict[str, list[str]]:
    cmd = [SSH_BIN]
    if config_file:
        cmd += ["-F", config_file]
    cmd.append("-G")
    if t.user:
        cmd += ["-l", t.user]
    if t.port:
        cmd += ["-p", str(t.port)]
    if t.jump:
        cmd += ["-J", t.jump]
    if t.identity:
        cmd += ["-i", t.identity]
    for o in t.options:
        cmd += ["-o", o]
    cmd += ["--", t.alias]
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
    except (OSError, asyncio.TimeoutError):
        return {}
    return parse_ssh_G(out.decode(errors="replace"))


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def parse_jump(spec: str) -> list[Target]:
    """'a,user@b:2222,ssh://c' -> [Target(a), Target(b,user,2222), Target(c)]."""
    hops = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item or item.lower() == "none":
            continue
        host, user, port = _split_dest(item)
        if port is None and host.startswith("[") and "]" in host:      # [::1]:22
            host, _, rest = host[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else None
        elif port is None and host.count(":") == 1:                   # host:port
            host, port = host.split(":")
        try:
            port = int(port) if port else None
        except ValueError:
            port = None
        hops.append(Target(host, user, port))
    return hops


@dataclass
class Hop:
    """Một chặng kết nối: tham số cho asyncssh.connect."""
    label: str
    opts: dict = field(default_factory=dict)


def hop_options(cfg: dict[str, list[str]], t: Target, strict: bool = True) -> dict:
    """Từ output `ssh -G` dựng tham số asyncssh.connect cho một chặng (chưa gồm tunnel)."""
    def first(k, default=None):
        return (cfg.get(k) or [default])[0]

    host = first("hostname", t.alias)
    opts = {
        "host": host,
        "port": int(first("port", t.port or 22)),
        "username": first("user", t.user or getpass.getuser()),
        # Đã resolve bằng ssh -G nên không để asyncssh tự đọc ~/.ssh/config nữa
        # (nó sẽ khớp theo HostName chứ không theo alias -> áp nhầm cấu hình).
        "config": [],
    }
    keys = [_expand(x) for x in cfg.get("identityfile", [])]
    keys = [k for k in keys if os.path.isfile(k)]
    if keys:
        opts["client_keys"] = keys
    agent = first("identityagent")
    if agent:
        low = agent.lower()
        if low == "none":
            opts["agent_path"] = None
        elif low != "ssh_auth_sock":
            opts["agent_path"] = _expand(agent.strip('"'))
    alias = first("hostkeyalias")
    if alias and alias.lower() != "none":
        opts["host_key_alias"] = alias
    shkc = (first("stricthostkeychecking") or "").lower()
    if not strict or shkc in ("false", "no", "off"):
        opts["known_hosts"] = None
    else:
        files = []
        for line in cfg.get("userknownhostsfile", []) + cfg.get("globalknownhostsfile", []):
            for f in shlex.split(line):
                f = _expand(f)
                if os.path.isfile(f):
                    files.append(f)
        opts["known_hosts"] = files or ()
    pc = first("proxycommand")
    if pc and pc.lower() != "none":
        opts["proxy_command"] = (pc.replace("%h", host).replace("%p", str(opts["port"]))
                                   .replace("%r", opts["username"]).replace("%n", t.alias)
                                   .replace("%%", "%"))
    return opts


async def resolve_chain(t: Target, config_file: str | None = None, strict: bool = True,
                        _depth: int = 0) -> list[Hop]:
    """Trả về danh sách chặng [jump1, jump2, ..., đích]. Mỗi jump cũng được resolve bằng ssh -G."""
    if _depth > 8:
        raise ApiError(400, "ProxyJump lồng nhau quá sâu", "config")
    cfg = await ssh_G(t, config_file)
    final = Hop(t.label, hop_options(cfg, t, strict))
    proxy = (cfg.get("proxyjump") or [None])[0]
    hops: list[Hop] = []
    if proxy and proxy.lower() != "none" and "proxy_command" not in final.opts:
        for i, j in enumerate(parse_jump(proxy)):
            sub = await resolve_chain(j, config_file, strict, _depth + 1)
            if i == 0:
                hops.extend(sub)
            else:
                # Chặng sau đi qua chặng trước: chỉ lấy chặng cuối của sub (ProxyJump của
                # jump thứ 2 trở đi bị bỏ qua, giống ssh -J a,b).
                hops.append(sub[-1])
    return hops + [final]


# ----------------------------------------------------------- dựng lệnh ssh
def remote_cd_command(remote_dir: str) -> str:
    """Lệnh chạy trên server để vào sẵn thư mục rồi mở login shell."""
    d = remote_dir.strip()
    if d == "~":
        q = "~"
    elif d.startswith("~/"):
        q = "~/" + shlex.quote(d[2:])
    else:
        q = shlex.quote(d)
    return f"cd {q} && exec $SHELL -l"


def build_ssh_argv(ch: dict, ssh_bin: str = "ssh") -> list[str]:
    """Dựng argv ssh từ một kênh (dict theo schema sessions_store)."""
    argv = [ssh_bin]
    port = ch.get("port")
    if port and int(port) != 22:
        argv += ["-p", str(int(port))]
    if ch.get("identity_file"):
        argv += ["-i", ch["identity_file"]]
    if ch.get("proxy_jump"):
        argv += ["-J", ch["proxy_jump"]]
    extra = ch.get("extra_args") or ""
    if extra:
        argv += shlex.split(extra)
    remote_dir = (ch.get("remote_dir") or "").strip()
    if remote_dir:
        argv.append("-t")
    host = ch["host"]
    argv.append(f"{ch['user']}@{host}" if ch.get("user") else host)
    if remote_dir:
        argv.append(remote_cd_command(remote_dir))
    return argv


def build_ssh_command(ch: dict, ssh_bin: str = "ssh") -> str:
    return shlex.join(build_ssh_argv(ch, ssh_bin))


def ssh_o_options(extra_args: str) -> tuple:
    """Lấy các '-o X' trong tham số ssh thêm, để `ssh -G` resolve giống lệnh ssh của kênh."""
    try:
        argv = shlex.split(extra_args or "")
    except ValueError:
        return ()
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "-o" and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 1
        elif a.startswith("-o") and len(a) > 2:
            out.append(a[2:])
        i += 1
    return tuple(out)


def target_for_channel(ch: dict) -> Target:
    port = ch.get("port")
    try:
        port = int(port) if port else None
    except (TypeError, ValueError):
        port = None
    if port == 22 and ch.get("source") == "ssh_config":
        port = None
    return Target(ch["host"], ch.get("user") or None, port,
                  ch.get("proxy_jump") or None, ch.get("identity_file") or None,
                  ssh_o_options(ch.get("extra_args") or ""))
