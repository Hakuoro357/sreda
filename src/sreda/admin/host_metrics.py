"""Host + egress-tunnel health for the admin overview.

Read-only and **fail-soft by design**: every probe degrades to
``None`` / ``"unknown"`` instead of raising, because a metrics glitch
must never turn the admin dashboard into a 500. No new dependencies —
CPU/RAM/disk come from ``/proc`` + ``shutil`` (stdlib); systemd unit
state comes from ``systemctl`` (read-only, works for the non-root
service user).

On non-Linux dev machines ``/proc`` is absent, so every reader returns
``None`` and the template shows "—". Tests inject fakes via the
``_read_text`` / ``_systemctl`` seams.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

# Egress tunnel systemd units (see SERVERS.md §2 / #244).
TUNNEL_UNIT_MAIN = "sreda-socks-tunnel.service"       # :1080 general egress
TUNNEL_UNIT_FETCH = "sreda-fetch-egress-tunnel.service"  # :1081 filtered (#244)


@dataclass(slots=True)
class HostMetrics:
    load1: float | None = None          # 1-min load average
    ncpu: int | None = None             # logical CPUs (load1/ncpu ≈ utilisation)
    mem_used_mb: int | None = None
    mem_total_mb: int | None = None
    disk_used_pct: int | None = None
    disk_free_gb: float | None = None


@dataclass(slots=True)
class TunnelStatus:
    unit: str
    active: str = "unknown"             # "active" | "inactive" | "failed" | "unknown"
    nrestarts: int | None = None        # cumulative auto-restarts (churn signal)
    uptime_hours: float | None = None   # hours since last (re)start — the useful signal


# --- seams (monkeypatched in tests) ----------------------------------------

def _read_text(path: str) -> str | None:
    """Read a small proc/sys file. Any error → None (fail-soft)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _systemctl(args: list[str]) -> str | None:
    """Run a read-only ``systemctl`` query. Any error/timeout → None.

    ``show`` returns rc=0 even for inactive units, so we do NOT gate on
    returncode — stdout is the answer either way. Timeout is tight
    (1.5s): the dashboard must stay instant even with a degraded
    systemd; a timeout degrades the tunnel card to "unknown".
    """
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            capture_output=True, text=True, timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").strip()


# --- host metrics -----------------------------------------------------------

def _parse_loadavg(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return round(float(text.split()[0]), 2)
    except (ValueError, IndexError):
        return None


def _parse_meminfo(text: str | None) -> tuple[int | None, int | None]:
    """Return (used_mb, total_mb). used = MemTotal - MemAvailable."""
    if not text:
        return None, None
    total_kb: int | None = None
    avail_kb: int | None = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = _first_int(line)
        elif line.startswith("MemAvailable:"):
            avail_kb = _first_int(line)
        if total_kb is not None and avail_kb is not None:
            break
    if total_kb is None or avail_kb is None:
        return None, None
    used_mb = max(0, (total_kb - avail_kb)) // 1024
    return used_mb, total_kb // 1024


def _first_int(line: str) -> int | None:
    for tok in line.split():
        if tok.isdigit():
            return int(tok)
    return None


def get_host_metrics(disk_path: str = "/") -> HostMetrics:
    m = HostMetrics()
    m.load1 = _parse_loadavg(_read_text("/proc/loadavg"))
    m.ncpu = os.cpu_count()
    m.mem_used_mb, m.mem_total_mb = _parse_meminfo(_read_text("/proc/meminfo"))
    try:
        du = shutil.disk_usage(disk_path)
        m.disk_used_pct = round(du.used / du.total * 100) if du.total else None
        m.disk_free_gb = round(du.free / (1024 ** 3), 1)
    except OSError:
        pass
    return m


# --- tunnel status ----------------------------------------------------------

def get_tunnel_status(unit: str) -> TunnelStatus:
    """One batched ``systemctl show`` call per unit (R1: 2 calls → 1).

    ``--value`` prints one property per line in the requested order:
    ActiveState, NRestarts, ActiveEnterTimestamp. Uptime — фидбек
    владельца 2026-07-03: накопительный NRestarts (1098 после шторма
    24-25.06) пугает; «жив N ч» — полезный сигнал.
    """
    st = TunnelStatus(unit=unit)
    out = _systemctl([
        "show", "-p", "ActiveState,NRestarts,ActiveEnterTimestamp",
        "--value", unit,
    ])
    if out is None:
        return st  # systemctl недоступен → "unknown"/None
    lines = out.splitlines()
    if lines and lines[0].strip():
        st.active = lines[0].strip()
    if len(lines) > 1 and lines[1].strip().isdigit():
        st.nrestarts = int(lines[1].strip())
    if len(lines) > 2:
        st.uptime_hours = _uptime_hours(lines[2].strip())
    return st


def _uptime_hours(active_enter: str) -> float | None:
    """systemd 'Wed 2026-07-01 12:01:27 UTC' → часы с момента старта."""
    if not active_enter:
        return None
    from datetime import UTC, datetime

    parts = active_enter.split()
    # форма: [День] YYYY-MM-DD HH:MM:SS [TZ] — берём дату+время
    for i, tok in enumerate(parts):
        if len(tok) == 10 and tok[4] == "-" and i + 1 < len(parts):
            try:
                dt = datetime.fromisoformat(f"{tok} {parts[i + 1]}")
            except ValueError:
                return None
            tz = parts[i + 2] if i + 2 < len(parts) else "UTC"
            if tz != "UTC":
                return None  # незнакомый пояс — честнее «—», чем враньё
            dt = dt.replace(tzinfo=UTC)
            hours = (datetime.now(UTC) - dt).total_seconds() / 3600
            return round(hours, 1) if hours >= 0 else None
    return None


def get_egress_tunnels() -> list[TunnelStatus]:
    """Both egress tunnels, main first."""
    return [
        get_tunnel_status(TUNNEL_UNIT_MAIN),
        get_tunnel_status(TUNNEL_UNIT_FETCH),
    ]
