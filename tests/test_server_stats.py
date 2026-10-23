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


def test_command_wrapped_in_sh():
    assert build_command().startswith("sh -c ")


def test_df_lists_real_disks_only():
    out = OUT.replace("/dev/sda1 1000000 950000 50000 95% /",
                      "tmpfs 10 1 9 10% /run\n/dev/sda1 1000000 950000 50000 95% /\n"
                      "/dev/sdb1 100 10 90 10% /data\n/dev/sdb1 100 10 90 10% /bind")
    r, _ = parse(out)
    assert [d["mount"] for d in r["disks"]] == ["/", "/data"] and r["disk"]["mount"] == "/"
