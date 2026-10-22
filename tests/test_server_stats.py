from xshell_panels.server_stats import build_command, parse

OUT = """##stat
cpu  100 0 100 800 0 0 0 0 0 0
##mem
MemTotal:       1000000 kB
MemFree:         100000 kB
MemAvailable:    100000 kB
##load
3.00 2.00 1.00 1/200 123
##nproc
2
##df
/dev/sda1 1000000 950000 50000 95% /
"""
NEXT = OUT.replace("cpu  100 0 100 800", "cpu  200 0 200 800")


def test_first_sample_has_no_cpu():
    r, cur = parse(OUT)
    assert r["cpu"] is None and cur == (1000, 800)
    assert r["mem"]["pct"] == 90.0 and r["disk"]["pct"] == 95.0 and r["cores"] == 2


def test_cpu_delta_and_alerts():
    _, prev = parse(OUT)
    r, _ = parse(NEXT, prev)
    assert r["cpu"] == 100.0
    assert set(r["alerts"]) == {"cpu", "mem", "disk", "load"}


def test_non_linux_output_degrades():
    r, cur = parse("##stat\n##mem\n##load\n##nproc\n##df\n/dev/disk1 100 10 90 10% /\n")
    assert cur is None and r["cpu"] is None and r["mem"] is None and r["disk"]["pct"] == 10.0
    assert r["alerts"] == []


def test_command_quotes_path():
    assert "'" in build_command("/a b/it's") and build_command(None).startswith("sh -c ")
