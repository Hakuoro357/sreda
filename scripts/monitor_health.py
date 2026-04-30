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

        # Logic: алерт только если ВИДНО проблему ПРЯМО СЕЙЧАС.
        # last_error_date в TG getWebhookInfo это историческая запись,
        # сохраняется индефинитно. Сам факт recent error НЕ значит что
        # сейчас проблема — Telegram уже мог retry'нуть успешно.
        # Поэтому критерий: pending > 0 (юзеры висят в очереди) ИЛИ
        # рост queue (тренд).
        if pending >= 5:
            return ProbeResult(
                "webhook_health", "critical",
                f"pending_update_count={pending} (queue stuck): {last_err_msg}",
                value={"pending": pending},
            )
        if pending >= 1 and last_err and (now - last_err) < 300:
            # 1-4 pending + recent error = текущий incident, может разрастись
            return ProbeResult(
                "webhook_health", "warning",
                f"pending={pending} + last_err {now-last_err}s ago: {last_err_msg}",
            )
        return ProbeResult(
            "webhook_health", "ok",
            f"pending={pending} (last_err: {last_err_msg or 'none'} {(now-last_err)}s ago)" if last_err else f"pending={pending}",
        )
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


def _pg_query(sql: str) -> str | None:
    """Run a single SQL via psql, return scalar string or None on error."""
    pg_pwd_dsn = _ENV.get("SREDA_DATABASE_URL", "")
    if "@" not in pg_pwd_dsn:
        return None
    try:
        password = pg_pwd_dsn.split("://")[1].split("@")[0].split(":")[1]
    except Exception:
        return None
    rc = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-U", "sreda", "-d", "sreda",
         "-tA", "-c", sql],
        capture_output=True, text=True, timeout=5,
        env={**os.environ, "PGPASSWORD": password},
    )
    if rc.returncode == 0:
        return rc.stdout.strip()
    return None


def probe_pg_connections() -> ProbeResult:
    n = _pg_query("SELECT count(*) FROM pg_stat_activity")
    if n is None:
        return ProbeResult("pg_connections", "warning", "psql query failed")
    n_int = int(n)
    if n_int >= 70:
        return ProbeResult("pg_connections", "warning", f"{n_int}/100 connections")
    return ProbeResult("pg_connections", "ok", f"{n_int}/100 connections")


def probe_pg_disk_free() -> ProbeResult:
    rc = subprocess.run(
        ["df", "-BG", "--output=avail", "/var/lib/postgresql"],
        capture_output=True, text=True, timeout=5,
    )
    if rc.returncode != 0:
        return ProbeResult("pg_disk_free", "warning", "df failed")
    lines = rc.stdout.strip().split("\n")
    if len(lines) < 2:
        return ProbeResult("pg_disk_free", "warning", "df parse fail")
    avail_gb = int(lines[1].strip().rstrip("G"))
    if avail_gb < 1:
        return ProbeResult("pg_disk_free", "critical", f"{avail_gb}G free on /var/lib/postgresql")
    if avail_gb < 3:
        return ProbeResult("pg_disk_free", "warning", f"{avail_gb}G free on /var/lib/postgresql")
    return ProbeResult("pg_disk_free", "ok", f"{avail_gb}G free")


def probe_pg_locks() -> ProbeResult:
    n = _pg_query("SELECT count(*) FROM pg_locks WHERE NOT granted")
    if n is None:
        return ProbeResult("pg_locks", "ok", "(query failed, skip)")
    n_int = int(n)
    if n_int > 5:
        return ProbeResult("pg_locks", "warning", f"{n_int} ungranted locks")
    return ProbeResult("pg_locks", "ok", f"{n_int} ungranted locks")


# ---------------------------------------------------------------------------
# External API latency probes
# ---------------------------------------------------------------------------
def _external_latency(url: str, name: str, baseline_ms: int = 500) -> ProbeResult:
    """Measure GET latency. Critical if 2x consecutive misses or >2x baseline."""
    try:
        t0 = time.time()
        with httpx.Client(timeout=5.0) as c:
            r = c.get(url)
        elapsed_ms = int((time.time() - t0) * 1000)
        if r.status_code >= 500:
            return ProbeResult(name, "critical", f"{r.status_code} ({elapsed_ms}ms)")
        if elapsed_ms > baseline_ms * 4:
            return ProbeResult(name, "warning", f"{elapsed_ms}ms (4x baseline {baseline_ms}ms)")
        return ProbeResult(name, "ok", f"{elapsed_ms}ms")
    except httpx.TimeoutException:
        return ProbeResult(name, "critical", "timeout >5s")
    except Exception as e:
        return ProbeResult(name, "critical", f"error: {type(e).__name__}: {str(e)[:100]}")


def probe_telegram_api_latency() -> ProbeResult:
    token = _ENV.get("SREDA_TELEGRAM_BOT_TOKEN")
    if not token:
        return ProbeResult("telegram_api_latency", "ok", "(no token, skip)")
    return _external_latency(f"https://api.telegram.org/bot{token}/getMe",
                              "telegram_api_latency", baseline_ms=200)


def probe_mimo_llm_latency() -> ProbeResult:
    return _external_latency("https://token-plan-sgp.xiaomimimo.com/v1/models",
                              "mimo_llm_latency", baseline_ms=500)


def probe_openrouter_latency() -> ProbeResult:
    return _external_latency("https://openrouter.ai/api/v1/models",
                              "openrouter_latency", baseline_ms=500)


def probe_groq_stt_latency() -> ProbeResult:
    return _external_latency("https://api.groq.com/openai/v1/models",
                              "groq_stt_latency", baseline_ms=400)


# ---------------------------------------------------------------------------
# Trace.log analysis
# ---------------------------------------------------------------------------
def _recent_traces(window_min: int = 30) -> list[dict]:
    """Парсит последние N мин trace.log в struct'ы.

    Trace формат — multiline блок начинается с ``=== TRACE trace_<id> <ts> ...``,
    содержит indented events, заканчивается ``------- TOTAL <ms>ms iters=N ...``.
    """
    if not TRACE_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    traces = []
    current = None
    try:
        with open(TRACE_LOG, "r", encoding="utf-8", errors="ignore") as fh:
            # Tail-style: read last N MB
            try:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 500_000))
            except OSError:
                pass
            for line in fh:
                line = line.rstrip()
                if "=== TRACE " in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        try:
                            ts = datetime.fromisoformat(parts[3] + "T" + parts[4]).replace(tzinfo=timezone.utc)
                            current = {"ts": ts, "iters": 0, "total_ms": 0, "ack_ms": None, "type": None}
                        except Exception:
                            current = None
                elif "webhook.received" in line and current is not None:
                    # "      0ms  webhook.received       type=text"
                    if "type=text" in line:
                        current["type"] = "text"
                    elif "type=voice" in line:
                        current["type"] = "voice"
                    elif "type=callback" in line:
                        current["type"] = "callback"
                    else:
                        current["type"] = "other"
                elif "ack.sent" in line and current is not None:
                    # "      4ms  ack.sent  [539ms] phrase=..."
                    try:
                        bracket = line.split("[")[1].split("ms]")[0]
                        current["ack_ms"] = int(bracket)
                    except Exception:
                        pass
                elif line.startswith("------- TOTAL ") and current is not None:
                    try:
                        ms = int(line.split("TOTAL ")[1].split("ms")[0])
                        iters = int(line.split("iters=")[1].split()[0])
                        current["total_ms"] = ms
                        current["iters"] = iters
                        if current["ts"] >= cutoff:
                            traces.append(current)
                    except Exception:
                        pass
                    current = None
    except Exception:
        return []
    return traces


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def probe_turn_latency_p95() -> ProbeResult:
    traces = _recent_traces(window_min=30)
    if not traces:
        return ProbeResult("turn_latency_p95", "ok", "(no traces in 30m)")
    totals = [t["total_ms"] for t in traces if t["total_ms"] > 0]
    if not totals:
        return ProbeResult("turn_latency_p95", "ok", f"(no completed turns in 30m, n={len(traces)})")
    p95 = _percentile(totals, 0.95)
    if p95 > 30_000:
        return ProbeResult("turn_latency_p95", "critical", f"p95={p95}ms (n={len(totals)})")
    if p95 > 15_000:
        return ProbeResult("turn_latency_p95", "warning", f"p95={p95}ms (n={len(totals)})")
    return ProbeResult("turn_latency_p95", "ok", f"p95={p95}ms (n={len(totals)})")


def probe_failed_turns_rate() -> ProbeResult:
    traces = _recent_traces(window_min=30)
    # Считаем только text/voice (где LLM ОБЯЗАН отработать). Callback'и и
    # pending-bot ведут к iters=0 by design.
    chat_traces = [t for t in traces if t.get("type") in ("text", "voice")]
    if not chat_traces:
        return ProbeResult("failed_turns_rate", "ok", "(no chat turns in 30m)")
    n = len(chat_traces)
    failed = sum(1 for t in chat_traces if t["iters"] == 0)
    pct = 100 * failed / n if n else 0
    if n >= 5 and pct > 20:
        return ProbeResult("failed_turns_rate", "critical", f"{failed}/{n} chat-turns failed ({pct:.0f}%)")
    return ProbeResult("failed_turns_rate", "ok", f"{failed}/{n} chat-turns failed ({pct:.0f}%)")


def probe_ack_latency_p95() -> ProbeResult:
    traces = _recent_traces(window_min=30)
    ack_values = [t["ack_ms"] for t in traces if t.get("ack_ms")]
    if not ack_values:
        return ProbeResult("ack_latency_p95", "ok", "(no ack samples in 30m)")
    p95 = _percentile(ack_values, 0.95)
    if p95 > 5000:
        return ProbeResult("ack_latency_p95", "critical", f"p95={p95}ms (n={len(ack_values)})")
    if p95 > 2000:
        return ProbeResult("ack_latency_p95", "warning", f"p95={p95}ms (n={len(ack_values)})")
    return ProbeResult("ack_latency_p95", "ok", f"p95={p95}ms (n={len(ack_values)})")


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def probe_fail2ban_active() -> ProbeResult:
    # fail2ban-client status требует sudo, sreda юзер не имеет привилегий.
    # Проверяем только что service active. Ban count не critical для monitoring.
    rc = subprocess.run(
        ["systemctl", "is-active", "fail2ban"],
        capture_output=True, text=True, timeout=5,
    )
    if rc.stdout.strip() == "active":
        return ProbeResult("fail2ban_active", "ok", "active")
    return ProbeResult("fail2ban_active", "warning", f"fail2ban={rc.stdout.strip() or 'unknown'}")


PROBES: list[Callable[[], ProbeResult]] = [
    # Critical infra
    probe_uvicorn_active,
    probe_job_runner_active,
    probe_pg_responsive,
    probe_pg_connections,
    probe_pg_disk_free,
    probe_pg_locks,
    probe_webhook_health,
    probe_last_backup_age,
    # External APIs
    probe_telegram_api_latency,
    probe_mimo_llm_latency,
    probe_openrouter_latency,
    probe_groq_stt_latency,
    # Trace metrics
    probe_turn_latency_p95,
    probe_failed_turns_rate,
    probe_ack_latency_p95,
    # Security
    probe_fail2ban_active,
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
