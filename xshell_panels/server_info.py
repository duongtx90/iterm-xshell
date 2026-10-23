"""Thông tin quản trị server cho tab "Server": mỗi mục là MỘT lệnh cố định (danh sách trắng,
client không gửi lệnh tuỳ ý), chạy qua SSH, chỉ đọc. Mục cần root dùng `sudo -n` nếu được."""
from __future__ import annotations

import re
import shlex

SECTIONS = ("procs", "ports", "firewall", "access", "actions")

# Đặt $S = "sudo -n" khi không phải root nhưng sudo không cần mật khẩu; in ##priv = 1 nếu có quyền root.
_PRIV = (
    'S=""; P=0; if [ "$(id -u)" = 0 ]; then P=1; '
    'elif sudo -n true 2>/dev/null; then S="sudo -n"; P=1; fi; echo "##priv $P"; '
)

_SCRIPTS = {
    # tên tiến trình + RSS; %CPU của ps là trung bình từ lúc tiến trình chạy
    "procs": "ps -eo pid=,user=,pcpu=,pmem=,rss=,comm= | sort -k3 -nr | head -n 80",
    "ports": "{ ss -H -ltnup 2>/dev/null || ss -H -ltnu 2>/dev/null ; } | head -n 80",
    "firewall": (
        'if command -v ufw >/dev/null 2>&1; then echo "##fw ufw"; $S ufw status verbose; '
        'elif command -v firewall-cmd >/dev/null 2>&1; then echo "##fw firewalld"; $S firewall-cmd --list-all; '
        'elif command -v nft >/dev/null 2>&1; then echo "##fw nftables"; $S nft list ruleset; '
        'elif command -v iptables >/dev/null 2>&1; then echo "##fw iptables"; $S iptables -S; '
        'else echo "##fw none"; fi'
    ),
    "access": (
        'echo "##who"; who; echo "##last"; last -n 15 -a 2>/dev/null | head -n 15; '
        'echo "##failed"; $S lastb -n 10 -a 2>/dev/null | head -n 10; '
        'echo "##ssh"; if command -v journalctl >/dev/null 2>&1; then '
        '$S journalctl -q --no-pager -n 400 -o short-iso -u ssh -u sshd 2>/dev/null; '
        'else cat /var/log/auth.log /var/log/secure 2>/dev/null | grep sshd | tail -n 400; fi '
        '| grep -E "Accepted|Failed|Invalid user" | tail -n 25'
    ),
    "actions": (
        'if command -v journalctl >/dev/null 2>&1; then '
        '$S journalctl -q --no-pager -n 300 -o short-iso _COMM=sudo 2>/dev/null; '
        'else $S sh -c "cat /var/log/auth.log /var/log/secure 2>/dev/null" | grep "sudo:" | tail -n 300; fi '
        '| grep "COMMAND=" | tail -n 40'
    ),
}

MAX_OUT = 64 * 1024


def build_command(section: str) -> str:
    if section not in _SCRIPTS:
        raise KeyError(section)
    # sh -c: chạy được dù login shell của user là fish/csh
    return "sh -c " + shlex.quote("exec 2>/dev/null; " + _PRIV + _SCRIPTS[section])


def _split(out: str) -> tuple[bool, dict[str, list[str]], dict[str, str]]:
    """Tách theo dòng đánh dấu '##tên [đối số]'."""
    priv, cur, sec, args = False, "", {"": []}, {}
    for line in out[:MAX_OUT].splitlines():
        if line.startswith("##priv"):
            priv = line.split()[-1] == "1"
        elif line.startswith("##"):
            tok = line[2:].split()
            cur = tok[0] if tok else ""
            if len(tok) > 1:
                args[cur] = tok[1]
            sec.setdefault(cur, [])
        elif line.strip():
            sec.setdefault(cur, []).append(line.rstrip())
    return priv, sec, args


def parse_procs(lines: list[str]) -> list[dict]:
    """Nhóm theo tên lệnh (nginx/php-fpm có nhiều worker): cộng CPU, RAM, RSS."""
    groups: dict[str, dict] = {}
    for ln in lines:
        f = ln.split(None, 5)
        if len(f) < 6:
            continue
        try:
            cpu, mem, rss = float(f[2]), float(f[3]), int(f[4]) * 1024
        except ValueError:
            continue
        name = f[5].strip()
        g = groups.setdefault(name, {"name": name, "count": 0, "cpu": 0.0, "mem": 0.0,
                                      "rss": 0, "users": set()})
        g["count"] += 1
        g["cpu"] += cpu
        g["mem"] += mem
        g["rss"] += rss
        g["users"].add(f[1])
    out = []
    for g in groups.values():
        g["cpu"], g["mem"] = round(g["cpu"], 1), round(g["mem"], 1)
        g["users"] = sorted(g["users"])
        out.append(g)
    return out


def parse_ports(lines: list[str]) -> list[dict]:
    """ss -H -ltnup: Netid State Recv-Q Send-Q Local Peer [Process]"""
    out, seen = [], set()
    for ln in lines:
        f = ln.split()
        if len(f) < 5 or f[0] not in ("tcp", "udp", "tcp6", "udp6") or not f[4].rpartition(":")[2].isdigit():
            continue
        local = f[4]
        addr, _, port = local.rpartition(":")
        proc = ""
        m = re.search(r'users:\(\("([^"]+)"', ln)
        if m:
            proc = m.group(1)
        key = (f[0][:3], port, addr in ("127.0.0.1", "::1", "[::1]"), proc)
        if key in seen:                       # 0.0.0.0 và [::] của cùng một cổng
            continue
        seen.add(key)
        out.append({"proto": f[0][:3], "port": port, "addr": addr.strip("[]") or "*",
                    "local_only": addr in ("127.0.0.1", "::1", "[::1]"), "proc": proc})
    out.sort(key=lambda p: (int(p["port"]) if p["port"].isdigit() else 0, p["proto"]))
    return out


_SUDO = re.compile(r"^(.*?)\s+\S+\s+sudo(?:\[\d+\])?:\s+(\S+)\s+:\s+(.*)$")


def parse_actions(lines: list[str]) -> list[dict]:
    out = []
    for ln in lines:
        m = _SUDO.match(ln)
        if not m:
            continue
        ts, user, rest = m.groups()
        c = re.search(r"COMMAND=(.*)$", rest)
        if not c:
            continue
        as_user = re.search(r"USER=(\S+)", rest)
        out.append({"time": ts.replace("T", " ")[:19], "user": user,
                    "as": as_user.group(1) if as_user else "", "cmd": c.group(1).strip()})
    return out[::-1]                          # mới nhất trước


def parse(section: str, out: str) -> dict:
    priv, sec, args = _split(out)
    lines = sec[""]
    res: dict = {"section": section, "priv": priv}
    if section == "procs":
        res["procs"] = parse_procs(lines)
    elif section == "ports":
        res["ports"] = parse_ports(lines)
    elif section == "firewall":
        kind = args.get("fw", "none")
        res["kind"] = kind
        res["text"] = "\n".join(sec.get("fw", []))
    elif section == "access":
        res["who"] = sec.get("who", [])
        res["last"] = [x for x in sec.get("last", []) if not x.startswith(("wtmp", "reboot"))][:15]
        res["failed"] = [x for x in sec.get("failed", []) if not x.startswith("btmp")]
        res["ssh"] = sec.get("ssh", [])[::-1]
    elif section == "actions":
        res["actions"] = parse_actions(lines)
    return res
