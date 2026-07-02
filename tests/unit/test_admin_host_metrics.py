"""Unit tests for admin host/tunnel metrics — parsing + fail-soft.

The contract that matters: a probe glitch must degrade to None/"unknown",
never raise (a metrics hiccup must not 500 the admin dashboard).
"""

from __future__ import annotations

from sreda.admin import host_metrics as hm

_MEMINFO = """MemTotal:        4008064 kB
MemFree:          347000 kB
MemAvailable:    2502144 kB
Buffers:          120000 kB
"""


def test_parse_loadavg_ok():
    assert hm._parse_loadavg("0.06 0.03 0.00 1/234 5678") == 0.06


def test_parse_loadavg_bad_inputs():
    assert hm._parse_loadavg(None) is None
    assert hm._parse_loadavg("") is None
    assert hm._parse_loadavg("garbage") is None


def test_parse_meminfo_ok():
    used_mb, total_mb = hm._parse_meminfo(_MEMINFO)
    # total = 4008064 kB / 1024 = 3914 MB; used = (4008064-2502144)/1024 = 1470 MB
    assert total_mb == 3914
    assert used_mb == 1470


def test_parse_meminfo_missing_field():
    assert hm._parse_meminfo("MemTotal: 100 kB\n") == (None, None)
    assert hm._parse_meminfo(None) == (None, None)


def test_get_host_metrics_failsoft_when_proc_absent(monkeypatch):
    # /proc unreadable (dev machine / permission) → None fields, NO raise.
    monkeypatch.setattr(hm, "_read_text", lambda path: None)
    monkeypatch.setattr(hm.shutil, "disk_usage",
                        lambda p: (_ for _ in ()).throw(OSError("no such path")))
    m = hm.get_host_metrics()
    assert m.load1 is None
    assert m.mem_used_mb is None and m.mem_total_mb is None
    assert m.disk_used_pct is None


def test_get_host_metrics_reads_values(monkeypatch):
    monkeypatch.setattr(hm, "_read_text", lambda path: {
        "/proc/loadavg": "0.50 0.10 0.05 1/1 1",
        "/proc/meminfo": _MEMINFO,
    }.get(path))
    monkeypatch.setattr(hm.shutil, "disk_usage",
                        lambda p: type("DU", (), {"total": 100, "used": 13, "free": 87})())
    m = hm.get_host_metrics()
    assert m.load1 == 0.5
    assert m.mem_total_mb == 3914
    assert m.disk_used_pct == 13


def test_tunnel_status_active_with_restarts(monkeypatch):
    # R1: один батч-вызов `show -p ActiveState,NRestarts --value` —
    # две строки в порядке запроса.
    seen = []
    monkeypatch.setattr(hm, "_systemctl",
                        lambda args: seen.append(args) or "active\n1098")
    st = hm.get_tunnel_status("sreda-socks-tunnel.service")
    assert st.active == "active"
    assert st.nrestarts == 1098
    assert len(seen) == 1  # ровно ОДИН subprocess на юнит


def test_tunnel_status_failsoft_when_systemctl_absent(monkeypatch):
    # systemctl unavailable → "unknown"/None, NO raise.
    monkeypatch.setattr(hm, "_systemctl", lambda args: None)
    st = hm.get_tunnel_status("sreda-socks-tunnel.service")
    assert st.active == "unknown"
    assert st.nrestarts is None


def test_tunnel_status_inactive(monkeypatch):
    monkeypatch.setattr(hm, "_systemctl", lambda args: "inactive\n")
    st = hm.get_tunnel_status("x.service")
    assert st.active == "inactive"
    assert st.nrestarts is None
