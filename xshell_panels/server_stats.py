"""Số liệu phần cứng của server (CPU/RAM/ổ đĩa/load) qua một lệnh SSH nhẹ, chỉ đọc /proc và df."""
from __future__ import annotations

import shlex

WARN_PCT = 90          # CPU / RAM / ổ đĩa từ mức này trở lên là quá tải
WARN_LOAD_PER_CORE = 1.5


def build_command(path: str | None) -> str:
    """Lệnh chạy trên server; bọc trong sh -c để chạy được dù login shell là fish/csh."""
    df = f"df -Pk -- {shlex.quote(path)} || df -Pk /" if path else "df -Pk /"
    script = (
        "exec 2>/dev/null; echo '##stat'; head -n1 /proc/stat; "
        "echo '##mem'; grep -E '^(MemTotal|MemAvailable|MemFree|Buffers|Cached):' /proc/meminfo; "
        "echo '##load'; cat /proc/loadavg; "
        "echo '##nproc'; nproc; "
        f"echo '##df'; {{ {df}; }} | tail -n 1"
    )
    return f"sh -c {shlex.quote(script)}"


def _sections(out: str) -> dict[str, list[str]]:
    sec: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("##"):
            cur = sec.setdefault(line[2:].strip(), [])
        elif cur is not None and line.strip():
            cur.append(line.strip())
    return sec


def _cpu_times(line: str):
    f = line.split()
    if not f or f[0] != "cpu":
        return None
    v = [int(x) for x in f[1:9] if x.isdigit()]
    if len(v) < 4:
        return None
    return sum(v), v[3] + (v[4] if len(v) > 4 else 0)   # total, idle+iowait


def parse(out: str, prev_cpu=None):
    """-> (stats dict, cpu_times mới để lưu làm mốc cho lần sau)."""
    sec = _sections(out)
    res: dict = {"cpu": None, "mem": None, "disk": None, "load": None, "cores": None}

    cur = None
    if sec.get("stat"):
        try:
            cur = _cpu_times(sec["stat"][0])
        except ValueError:
            cur = None
    if cur and prev_cpu and cur[0] > prev_cpu[0]:
        busy = 1 - (cur[1] - prev_cpu[1]) / (cur[0] - prev_cpu[0])
        res["cpu"] = round(max(0.0, min(1.0, busy)) * 100, 1)

    mem = {}
    for line in sec.get("mem", []):
        k, _, rest = line.partition(":")
        try:
            mem[k] = int(rest.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = mem.get("MemTotal")
    if total:
        avail = mem.get("MemAvailable")
        if avail is None:
            avail = mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)
        used = max(0, total - avail)
        res["mem"] = {"used": used, "total": total, "pct": round(used * 100 / total, 1)}

    if sec.get("load"):
        try:
            res["load"] = [float(x) for x in sec["load"][0].split()[:3]]
        except ValueError:
            pass
    if sec.get("nproc"):
        try:
            res["cores"] = int(sec["nproc"][0])
        except ValueError:
            pass

    if sec.get("df"):
        f = sec["df"][-1].split()
        try:
            size, used = int(f[1]) * 1024, int(f[2]) * 1024
            if size:
                res["disk"] = {"used": used, "total": size, "pct": round(used * 100 / size, 1),
                               "mount": f[-1]}
        except (ValueError, IndexError):
            pass

    res["alerts"] = alerts(res)
    return res, cur


def alerts(s: dict) -> list[str]:
    a = []
    if s.get("cpu") is not None and s["cpu"] >= WARN_PCT:
        a.append("cpu")
    if s.get("mem") and s["mem"]["pct"] >= WARN_PCT:
        a.append("mem")
    if s.get("disk") and s["disk"]["pct"] >= WARN_PCT:
        a.append("disk")
    if s.get("load") and s.get("cores") and s["load"][0] / s["cores"] >= WARN_LOAD_PER_CORE:
        a.append("load")
    return a
