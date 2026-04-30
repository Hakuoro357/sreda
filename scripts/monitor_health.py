"""Health monitor для Sreda — простой cron-based observability.

Запускается каждые 5 минут из cron, проверяет ~15 probe'ов, шлёт алерты
в Telegram-чат админа на STATE TRANSITION (OK→DOWN или DOWN→OK).

Состояние пробов хранится в /var/lib/sreda/monitor-state.json. Cooldown
15 минут между двумя alert'ами одного и того же probe (защита от flap-storm).

Конфиг: значения thresholds — константы в этом файле. Для изменения —
git push + reload cron (cron сам подхватит).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATE_PATH = Path("/var/lib/sreda/monitor-state.json")
TRACE_LOG = Path("/var/log/sreda/trace.log")
BACKUP_DIR = Path("/var/backups/sreda")
ENV_PATH = "/etc/sreda/.env"
COOLDOWN_MIN = 15
ADMIN_CHAT_ID = "352612382"  # Boris

Severity = Literal["ok", "warning", "critical"]


# ---------------------------------------------------------------------------
# Bot token loader (read /etc/sreda/.env without overriding os.environ)
# ---------------------------------------------------------------------------
def _load_env(path: str = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


_ENV = _load_env()


# ---------------------------------------------------------------------------
# ProbeResult
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    name: str
    status: Severity
    message: str
    value: Any = None


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def probe_uvicorn_active() -> ProbeResult:
    rc = subprocess.run(
        ["systemctl", "is-active", "sreda-uvicorn"],
        capture_output=True, text=True, timeout=5,
    )
    if rc.stdout.strip() == "active":
        return ProbeResult("uvicorn_active", "ok", "active")
    return ProbeResult(
        "uvicorn_active", "critical",
        f"sreda-uvicorn = {rc.stdout.strip() or 'unknown'}",
    )


def probe_job_runner_active() -> ProbeResult:
    rc = subprocess.run(
        ["systemctl", "is-active", "sreda-job-runner"],
        capture_output=True, text=True, timeout=5,
    )
    if rc.stdout.strip() == "active":
        return ProbeResult("job_runner_active", "ok", "active")
    return ProbeResult(
        "job_runner_active", "critical",
        f"sreda-job-runner = {rc.stdout.strip() or 'unknown'}",
    )


def probe_pg_responsive() -> ProbeResult:
    pg_pwd = _ENV.get("SREDA_DATABASE_URL", "")
    # SREDA_DATABASE_URL=postgresql+psycopg://sreda:PASS@localhost:5432/sreda
    if "@" not in pg_pwd:
        return ProbeResult("pg_responsive", "critical", "SREDA_DATABASE_URL not parseable")
    try:
        password = pg_pwd.split("://")[1].split("@")[0].split(":")[1]
    except Exception:
        return ProbeResult("pg_responsive", "critical", "DSN parse fail")

    rc = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-U", "sreda", "-d", "sreda",
         "-tA", "-c", "SELECT 1"],
        capture_output=True, text=True, timeout=5,
        env={**os.environ, "PGPASSWORD": password},
    )
    if rc.returncode == 0 and rc.stdout.strip() == "1":
        return ProbeResult("pg_responsive", "ok", "psql SELECT 1 ok")
    return ProbeResult(
        "pg_responsive", "critical",
        f"psql failed: {rc.stderr.strip()[:200]}",
    )


def probe_webhook_health() -> ProbeResult:
    token = _ENV.get("SREDA_TELEGRAM_BOT_TOKEN")
    if not token:
        return ProbeResult("webhook_health", "warning", "no bot token in env")
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
        body = r.json()
        if not body.get("ok"):
            return ProbeResult("webhook_health", "critical", f"getWebhookInfo ok=false: {body.get('description')}")
        info = body["result"]
        pending = info.get("pending_update_count", 0)
        last_err = info.get("last_error_date")
        last_err_msg = info.get("last_error_message")
        now = int(time.time())

        if pending >= 5:
            return ProbeResult(
                "webhook_health", "critical",
                f"pending_update_count={pending} (queue stuck)",
                value={"pending": pending, "last_err": last_err_msg},
            )
        if last_err and (now - last_err) < 300:
            return ProbeResult(
                "webhook_health", "critical",
                f"recent error ({(now-last_err)}s ago): {last_err_msg}",
            )
        return ProbeResult("webhook_health", "ok", f"pending={pending}")
    except Exception as e:
        return ProbeResult("webhook_health", "critical", f"getWebhookInfo failed: {e}")


def probe_last_backup_age() -> ProbeResult:
    if not BACKUP_DIR.exists():
        return ProbeResult("last_backup_age", "critical", "backup dir missing")
    backups = sorted(BACKUP_DIR.glob("sreda-*.dump.gz.enc"))
    if not backups:
        return ProbeResult("last_backup_age", "critical", "no encrypted backups found")
    latest = backups[-1]
    age_seconds = time.time() - latest.stat().st_mtime
    age_h = age_seconds / 3600
    size = latest.stat().st_size

    if age_h > 30:
        return ProbeResult(
            "last_backup_age", "critical",
            f"latest backup {age_h:.1f}h old (cron не отработал?)",
        )
    if size < 100 * 1024:  # <100KB
        return ProbeResult(
            "last_backup_age", "critical",
            f"backup size {size}b (corrupt?)",
        )
    return ProbeResult(
        "last_backup_age", "ok",
        f"{latest.name} age={age_h:.1f}h size={size//1024}KB",
    )


PROBES: list[Callable[[], ProbeResult]] = [
    probe_uvicorn_active,
    probe_job_runner_active,
    probe_pg_responsive,
    probe_webhook_health,
    probe_last_backup_age,
]


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
@dataclass
class ProbeState:
    status: Severity = "ok"
    since: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_alert_at: str | None = None
    last_message: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "since": self.since,
            "last_alert_at": self.last_alert_at,
            "last_message": self.last_message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProbeState":
        return cls(
            status=d.get("status", "ok"),
            since=d.get("since", datetime.now(timezone.utc).isoformat()),
            last_alert_at=d.get("last_alert_at"),
            last_message=d.get("last_message", ""),
        )


def load_state() -> dict[str, ProbeState]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text())
        return {k: ProbeState.from_dict(v) for k, v in raw.get("probes", {}).items()}
    except Exception:
        return {}


def save_state(state: dict[str, ProbeState]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(
        {"probes": {k: v.to_dict() for k, v in state.items()},
         "updated_at": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False, indent=2,
    ))


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
def send_telegram_alert(text: str) -> None:
    token = _ENV.get("SREDA_TELEGRAM_BOT_TOKEN")
    if not token:
        print("[alert] no bot token, skipping send", file=sys.stderr)
        return
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
        if r.status_code != 200 or not r.json().get("ok"):
            print(f"[alert] send failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"[alert] send exception: {e}", file=sys.stderr)


def format_alert(name: str, prev_status: Severity, new: ProbeResult,
                 host: str, prev_state: ProbeState | None) -> str:
    icons = {"critical": "🚨", "warning": "⚠️", "ok": "✅"}
    if new.status == "ok" and prev_status != "ok":
        # Recovery
        down_for = ""
        if prev_state and prev_state.since:
            try:
                since = datetime.fromisoformat(prev_state.since)
                delta = datetime.now(timezone.utc) - since
                down_for = f"\nDown for: {_fmt_duration(delta)}"
            except Exception:
                pass
        return (
            f"✅ <b>RECOVERED:</b> {name}\n"
            f"Probe: <code>{name}</code>{down_for}\n"
            f"Host: {host}"
        )
    icon = icons.get(new.status, "❓")
    return (
        f"{icon} <b>{new.status.upper()}:</b> {new.name}\n"
        f"Probe: <code>{name}</code>\n"
        f"Message: {new.message}\n"
        f"Host: {host}"
    )


def _fmt_duration(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> int:
    host = socket.gethostname()
    state = load_state()
    now = datetime.now(timezone.utc)

    for probe in PROBES:
        try:
            result = probe()
        except Exception as e:
            result = ProbeResult(probe.__name__.replace("probe_", ""), "critical", f"probe crashed: {e}")

        prev = state.get(result.name)
        prev_status = prev.status if prev else "ok"

        if result.status != prev_status:
            # Cooldown check (only for new alerts, not recoveries)
            should_alert = True
            if result.status != "ok" and prev and prev.last_alert_at:
                try:
                    last_alert = datetime.fromisoformat(prev.last_alert_at)
                    if (now - last_alert).total_seconds() < COOLDOWN_MIN * 60:
                        should_alert = False
                except Exception:
                    pass

            if should_alert:
                msg = format_alert(result.name, prev_status, result, host, prev)
                send_telegram_alert(msg)

                last_alert_iso = now.isoformat() if result.status != "ok" else (prev.last_alert_at if prev else None)
            else:
                last_alert_iso = prev.last_alert_at if prev else None

            state[result.name] = ProbeState(
                status=result.status,
                since=now.isoformat(),
                last_alert_at=last_alert_iso,
                last_message=result.message,
            )
        else:
            # Same status — just update message
            if prev:
                prev.last_message = result.message
                state[result.name] = prev
            else:
                state[result.name] = ProbeState(
                    status=result.status, since=now.isoformat(),
                    last_message=result.message,
                )

    save_state(state)

    # Print summary (для ручного debug)
    for name, ps in state.items():
        icon = {"ok": "✅", "warning": "⚠️", "critical": "🚨"}.get(ps.status, "?")
        print(f"  {icon} {name}: {ps.last_message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
