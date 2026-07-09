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
from urllib.parse import urlparse

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


def _proxy_for_url(url: str) -> str | None:
    """#208: прокси для внешнего probe — ЗЕРКАЛИТ бота. ``HTTPS_PROXY`` из ``_ENV``
    (``/etc/sreda/.env``) для хостов НЕ в ``NO_PROXY`` (Groq идёт через SOCKS-туннель, как
    ``speech/groq.py``); None (direct) для хостов В ``NO_PROXY`` (telegram/mimo/openrouter).
    Прямой маршрут с VDS до части CDN-IP мёртв (RU-сеть) → probe обязан мерить тот же путь,
    что и прод, иначе ложный CRITICAL (groq_stt 2026-06-23).

    ТОЧНО зеркалит приоритет бота (speech/groq.py::_resolve_outbound_proxy + provider_balances.py):
    SREDA_GROQ_HTTP_PROXY первым (Groq-специфичный HTTP→SOCKS-шим, если задан), затем общий SOCKS.
    На VDS (2026-06-23) задан только HTTPS_PROXY=socks5://… (socksio в venv ЕСТЬ → httpx+socks5 → Groq=401)."""
    proxy = None
    for _var in ("SREDA_GROQ_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                 "https_proxy", "http_proxy"):
        _v = _ENV.get(_var)
        if _v:
            proxy = _v
            break
    if not proxy:
        return None
    host = (urlparse(url).hostname or "").lower()
    raw = _ENV.get("NO_PROXY") or _ENV.get("no_proxy") or ""
    for entry in (e.strip().lower() for e in raw.split(",")):
        if entry and (host == entry or host.endswith("." + entry)):
            return None  # в NO_PROXY → прод ходит direct → probe тоже direct
    return proxy


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
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    proxy = _proxy_for_url(url)
    # #324: пробить через egress SOCKS (как _external_latency) — иначе прямой мёртвый
    # маршрут к api.telegram.org даёт ложный webhook_health timeout. trust_env=False:
    # cron не грузит os.environ, прокси берём из _ENV явно.
    _kw: dict[str, Any] = {"timeout": 5.0, "trust_env": False}
    if proxy:
        _kw["proxy"] = proxy
    try:
        with httpx.Client(**_kw) as c:
            r = c.get(url)
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


def _poller_unit_state(unit: str) -> str:
    """Return systemd active state string for *unit*, or 'unknown'."""
    rc = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True, text=True, timeout=5,
    )
    return rc.stdout.strip() or "unknown"


def _poller_unit_enabled_state(unit: str) -> str:
    """Return systemd enabled state string for *unit*, or 'unknown'."""
    rc = subprocess.run(
        ["systemctl", "is-enabled", unit],
        capture_output=True, text=True, timeout=5,
    )
    return (rc.stdout + rc.stderr).strip() or "unknown"


def _configured_bot_keys() -> list[str]:
    """Return the list of bot_keys that have tokens configured in /etc/sreda/.env.

    Always includes 'sreda' (primary bot). Adds 'sreda_home' only when
    SREDA_HOME_BOT_TOKEN is present and non-empty. This matches the logic in
    TelegramBotRegistry.from_settings so the monitor tracks exactly the bots
    the application knows about.
    """
    keys = ["sreda"]
    if _ENV.get("SREDA_HOME_BOT_TOKEN"):
        keys.append("sreda_home")
    return keys


def probe_telegram_poller_alive() -> ProbeResult:
    """Liveness — все сконфигурированные long-poller'ы живы и тикают.

    Для каждого bot_key (sreda + sreda_home если настроен):
      1. ``systemctl is-active sreda-telegram-poller@<key>`` == "active"
         (с fallback на legacy non-template unit для sreda, pre-cutover).
      2. ``poller_heartbeats.last_attempt_at`` для channel 'telegram:<key>'
         свежее 2 минут.

    Если токен бота настроен, поллер ОБЯЗАН быть активен — отсутствие
    или неактивность юнита является CRITICAL. «Not installed» не является
    OK для бота с настроенным токеном (inbound от него идёт через long-poll;
    сервис мёртв для пользователей).

    Если токен не настроен — bot_key в _configured_bot_keys() не попадёт,
    проба его не проверяет.

    Используем ``last_attempt_at`` (не ``last_ok_at``) — он ставится
    после КАЖДОГО getUpdates, в том числе при `200 []` и при сетевых
    ошибках. Liveness не зависит от того, отвечает ли Telegram API —
    для этого есть `telegram_api_health`.
    """
    bot_keys = _configured_bot_keys()
    issues: list[str] = []
    ok_parts: list[str] = []

    for bot_key in bot_keys:
        unit = f"sreda-telegram-poller@{bot_key}.service"
        legacy_unit = "sreda-telegram-poller.service"
        channel = f"telegram:{bot_key}"

        # --- 1. systemd state -------------------------------------------
        state = _poller_unit_state(unit)
        active = state == "active"

        if not active:
            # Fallback: legacy non-template unit for 'sreda' (pre-cutover)
            if bot_key == "sreda":
                legacy_state = _poller_unit_state(legacy_unit)
                if legacy_state == "active":
                    active = True
                    unit = legacy_unit  # report which unit is active
                else:
                    # Token is configured: both template and legacy unit are
                    # not active (whether not-found or inactive) — CRITICAL.
                    enabled = _poller_unit_enabled_state(unit)
                    legacy_enabled = _poller_unit_enabled_state(legacy_unit)
                    if "not-found" in enabled.lower() and "not-found" in legacy_enabled.lower():
                        issues.append(
                            f"{bot_key}: token configured but neither "
                            f"{unit} nor {legacy_unit} is installed — "
                            f"inbound is dead"
                        )
                    else:
                        issues.append(f"{bot_key}: systemd state={state} (legacy={legacy_state})")
                    continue
            else:
                # Token is configured: unit must be active.
                # Not-found is no longer OK — inbound is dead for this bot.
                enabled = _poller_unit_enabled_state(unit)
                if "not-found" in enabled.lower():
                    issues.append(
                        f"{bot_key}: token configured but {unit} is not installed — "
                        f"inbound is dead"
                    )
                else:
                    issues.append(f"{bot_key}: systemd {unit} state={state}")
                continue

        # --- 2. heartbeat in DB -----------------------------------------
        last_attempt = _pg_query(
            "SELECT EXTRACT(EPOCH FROM (NOW() - last_attempt_at))::int "
            f"FROM poller_heartbeats WHERE channel = '{channel}'"
        )
        if last_attempt is None or last_attempt == "":
            issues.append(
                f"{bot_key}: systemd active but no heartbeat row for channel='{channel}' "
                f"(poller never ticked?)"
            )
            continue
        try:
            secs = int(last_attempt)
        except ValueError:
            issues.append(f"{bot_key}: heartbeat parse fail: {last_attempt!r}")
            continue
        if secs > 120:
            issues.append(f"{bot_key}: last_attempt_at {secs}s ago (>120s threshold)")
            continue
        ok_parts.append(f"{bot_key}:heartbeat {secs}s ago")

    if issues:
        return ProbeResult(
            "telegram_poller_alive", "critical",
            "poller issues: " + "; ".join(issues),
        )
    return ProbeResult(
        "telegram_poller_alive", "ok",
        "all pollers active — " + ", ".join(ok_parts),
    )


def probe_telegram_api_health() -> ProbeResult:
    """Health — отвечает ли Telegram API на getUpdates для каждого бота.

    Для каждого настроенного bot_key проверяем разницу между
    ``last_attempt_at`` и ``last_ok_at`` в poller_heartbeats (channel
    = 'telegram:<key>'). Если last_ok_at старее 5 минут но
    last_attempt_at свежий → upstream проблема (TG API / network),
    не наша. Severity: warning.

    Если templated unit не установлен для данного bot_key — пропускаем
    (аналогично _alive пробе).
    """
    bot_keys = _configured_bot_keys()
    warnings: list[str] = []
    ok_parts: list[str] = []

    for bot_key in bot_keys:
        unit = f"sreda-telegram-poller@{bot_key}.service"
        legacy_unit = "sreda-telegram-poller.service"
        channel = f"telegram:{bot_key}"

        # Проверяем что хотя бы один из юнитов известен systemd
        enabled = _poller_unit_enabled_state(unit)
        legacy_enabled = _poller_unit_enabled_state(legacy_unit) if bot_key == "sreda" else "not-found"
        if "not-found" in enabled.lower() and "not-found" in legacy_enabled.lower():
            ok_parts.append(f"{bot_key}:(unit not installed yet — pre-cutover)")
            continue

        row = _pg_query(
            "SELECT "
            "  COALESCE(EXTRACT(EPOCH FROM (NOW() - last_ok_at))::int, -1), "
            f"  COALESCE(last_error, '') "
            f"FROM poller_heartbeats WHERE channel = '{channel}'"
        )
        if row is None or row == "":
            # Heartbeat row отсутствует — _alive проба поймает, тут ok.
            ok_parts.append(f"{bot_key}:(no heartbeat row yet)")
            continue

        parts = row.split("|", 1)
        if len(parts) != 2:
            warnings.append(f"{bot_key}: heartbeat parse fail: {row!r}")
            continue
        try:
            ok_secs = int(parts[0])
        except ValueError:
            warnings.append(f"{bot_key}: last_ok_at parse fail: {parts[0]!r}")
            continue
        last_err = parts[1].strip()

        if ok_secs == -1:
            warnings.append(
                f"{bot_key}: last_ok_at NULL (no successful getUpdates yet); "
                f"last_error={last_err[:100] or 'none'}"
            )
        elif ok_secs > 300:
            warnings.append(
                f"{bot_key}: last_ok_at {ok_secs}s ago (>300s); "
                f"last_error={last_err[:100] or 'none'}"
            )
        else:
            ok_parts.append(f"{bot_key}:last_ok {ok_secs}s ago")

    if warnings:
        return ProbeResult(
            "telegram_api_health", "warning",
            "TG API issues: " + "; ".join(warnings),
        )
    return ProbeResult(
        "telegram_api_health", "ok",
        ", ".join(ok_parts) if ok_parts else "(all ok)",
    )


def probe_unprocessed_inbound() -> ProbeResult:
    """Inbound persisted, но processing не дошёл до конца.

    Закрывает риск «inbound сохранён, _process_approved_turn упал /
    не стартовал». Считаем строки в ``inbound_messages`` со статусом
    ``ingested`` или ``processing_started``, созданные более 5 минут
    назад (но не старше 24 часов — historical noise игнорируем).

    Если статус-колонки ещё нет (миграция 0036 не накачена) —
    возвращаем ok с пометкой, проба не должна валить весь
    monitor_health на pre-cutover окружении.
    """
    # Сначала проверим что колонка существует.
    has_col = _pg_query(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'inbound_messages' "
        "AND column_name = 'processing_status'"
    )
    if has_col != "1":
        return ProbeResult(
            "unprocessed_inbound", "ok",
            "(column processing_status not present yet — migration 0036 pending)",
        )

    n = _pg_query(
        "SELECT COUNT(*) FROM inbound_messages "
        "WHERE processing_status IN ('ingested', 'processing_started') "
        "  AND created_at < NOW() - INTERVAL '5 minutes' "
        "  AND created_at > NOW() - INTERVAL '24 hours'"
    )
    if n is None:
        return ProbeResult(
            "unprocessed_inbound", "warning",
            "psql query failed",
        )
    try:
        count = int(n)
    except ValueError:
        return ProbeResult(
            "unprocessed_inbound", "warning",
            f"count parse fail: {n!r}",
        )
    if count > 0:
        return ProbeResult(
            "unprocessed_inbound", "critical",
            f"{count} inbound stuck in ingested/processing_started >5min",
        )
    return ProbeResult(
        "unprocessed_inbound", "ok",
        "0 stuck inbound",
    )


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
def _external_latency(
    url: str,
    name: str,
    baseline_ms: int = 500,
    warning_ms: int | None = None,
    timeout_s: float = 5.0,
    max_severity: Severity = "critical",
) -> ProbeResult:
    """Measure GET latency. Critical if 5xx или timeout. Warning если elapsed
    > warning_ms (если задан) или > 4x baseline иначе.

    #208: запрос идёт через SOCKS-прокси для хостов НЕ в NO_PROXY (как бот) — иначе прямой
    мёртвый маршрут даёт ложный CRITICAL. ``max_severity='warning'`` понижает critical→warning
    (для депрекейченных путей, напр. openrouter старого plan-execute рта)."""
    threshold_ms = warning_ms if warning_ms is not None else baseline_ms * 4

    def _cap(sev: Severity) -> Severity:
        return "warning" if (sev == "critical" and max_severity == "warning") else sev

    proxy = _proxy_for_url(url)
    # trust_env=False: не полагаемся на os.environ (cron его не грузит) — прокси берём из _ENV явно.
    client_kwargs: dict[str, Any] = {"timeout": timeout_s, "trust_env": False}
    if proxy:
        client_kwargs["proxy"] = proxy
    try:
        t0 = time.time()
        with httpx.Client(**client_kwargs) as c:
            r = c.get(url)
        elapsed_ms = int((time.time() - t0) * 1000)
        if r.status_code >= 500:
            return ProbeResult(name, _cap("critical"), f"{r.status_code} ({elapsed_ms}ms)")
        if elapsed_ms > threshold_ms:
            return ProbeResult(name, "warning", f"{elapsed_ms}ms (threshold {threshold_ms}ms)")
        return ProbeResult(name, "ok", f"{elapsed_ms}ms")
    except httpx.TimeoutException:
        return ProbeResult(name, _cap("critical"), f"timeout >{timeout_s}s")
    except Exception as e:
        return ProbeResult(name, _cap("critical"), f"error: {type(e).__name__}: {str(e)[:100]}")


def probe_telegram_api_latency() -> ProbeResult:
    token = _ENV.get("SREDA_TELEGRAM_BOT_TOKEN")
    if not token:
        return ProbeResult("telegram_api_latency", "ok", "(no token, skip)")
    return _external_latency(f"https://api.telegram.org/bot{token}/getMe",
                              "telegram_api_latency", baseline_ms=200)


def probe_mimo_llm_latency() -> ProbeResult:
    # Singapore datacenter — нестабильный путь через инет, периодические
    # сетевые блипы давали false-warnings. Поднял threshold до 20s —
    # ловим только серьёзные деградации (юзеры на fallback openrouter
    # переключатся раньше). Increase timeout до 22s чтобы ничего не
    # обрезалось на стороне probe.
    return _external_latency(
        "https://token-plan-sgp.xiaomimimo.com/v1/models",
        "mimo_llm_latency",
        warning_ms=20_000,
        timeout_s=22.0,
    )


def probe_openrouter_latency() -> ProbeResult:
    # #208: openrouter в NO_PROXY → прод ходит DIRECT, и это рот СТАРОГО plan-execute
    # (задепрекейчен; уйдёт с вырезанием старого планировщика). Прямой маршрут до openrouter
    # с VDS мёртв → probe не зелёный, но это НЕ live-critical → max_severity='warning'.
    return _external_latency("https://openrouter.ai/api/v1/models",
                              "openrouter_latency", baseline_ms=500,
                              max_severity="warning")


def probe_groq_stt_latency() -> ProbeResult:
    return _external_latency("https://api.groq.com/openai/v1/models",
                              "groq_stt_latency", baseline_ms=400)


# «Граница держит» = target НЕ достигнут. Кроме httpx.TransportError сюда ОБЯЗАТЕЛЬНО входит
# socksio.SOCKSError: при tcp-reset фильтра SOCKS-прокси отдаёт сбойный reply → socksio.ProtocolError
# («Malformed reply») ПРОТЕКАЕТ мимо httpx (НЕ TransportError) — доказано live на проде 2026-06-30.
# Без этого блокировка читалась бы как «probe error→warning» (ложный алерт каждый тик). Defensive import.
try:
    from socksio.exceptions import SOCKSError as _SocksError
    _BOUNDARY_BLOCKED_EXC: tuple[type[BaseException], ...] = (httpx.TransportError, _SocksError)
except Exception:  # noqa: BLE001  socksio — dep httpx[socks]; на проде есть, но не падаем если нет
    _BOUNDARY_BLOCKED_EXC = (httpx.TransportError,)


def probe_fetch_egress_filtered() -> ProbeResult:
    """#244 ИНВЕРСНАЯ проба SSRF-границы fetch_url. metadata-IP через фильтр-egress
    (``SREDA_FETCH_URL_PROXY``) ДОЛЖЕН быть недостижим. 3 шага:

    1. proxy обязан быть loopback-socks5 (иначе мониторим НЕ ту границу) → critical-misconfig.
       (Ошибка на root-туннель :1080 поймается и шагом 3: root не фильтрует → metadata reached → critical.)
    2. TCP-preflight живости туннеля. Мёртв → **warning** (НЕ ok): fetch тогда fail-closed, но граница
       НЕ проверена — нельзя выдавать «здорово» (иначе слепота к падению egress, ревью R1 MAJOR).
    3. GET 169.254.169.254 через ЖИВОЙ proxy: **ЛЮБОЙ HTTP-ответ (200/403/500/3xx) = critical** — TCP-коннект
       до link-local ПРОШЁЛ и сервис ответил = фильтр пробит (статус/тело неважны — важен факт достижимости;
       гейт на ==200 дал бы false-negative на 403/500 от достигнутой метадаты, ревью R1 — отклонено).
       Исключение через ЖИВОЙ proxy (RST/refused) = граница держит = ok.

    Семантика ИНВЕРСНА ``_external_latency`` (там 200=healthy). Пусто proxy → skip(ok)."""
    proxy = _ENV.get("SREDA_FETCH_URL_PROXY")
    if not proxy:
        return ProbeResult("fetch_egress_filtered", "ok", "(SREDA_FETCH_URL_PROXY unset, skip)")
    pp = urlparse(proxy)
    host = (pp.hostname or "").rstrip(".").lower()  # rstrip как прод-preflight: localhost. = localhost (R3)
    try:
        port = pp.port  # ValueError на нечисловом/out-of-range порту → ловим явно
    except ValueError:
        return ProbeResult("fetch_egress_filtered", "critical", "SREDA_FETCH_URL_PROXY: невалидный порт — мониторим не ту границу")
    # mirror прод-preflight_egress: чистый loopback-socks5, БЕЗ userinfo (R2/R3 ревью). Иначе мониторим не ту границу.
    # userinfo — по PRESENCE (is not None), не truthiness: пустой marker `socks5://@host` тоже отвергаем (R3).
    if (pp.scheme not in ("socks5", "socks5h") or host not in ("127.0.0.1", "::1", "localhost")
            or not port or pp.username is not None or pp.password is not None):
        return ProbeResult(
            "fetch_egress_filtered", "critical",
            f"SREDA_FETCH_URL_PROXY не чистый loopback-socks5 ({pp.scheme}://{host}:{port}) — мониторим не ту границу",
        )
    # 2. туннель жив? мёртв → fetch fail-closed, но граница НЕ проверена → warning (не ok).
    try:
        with socket.create_connection((host, port), timeout=3.0):
            pass
    except OSError:
        return ProbeResult(
            "fetch_egress_filtered", "warning",
            f"fetch-egress туннель :{port} недоступен — fetch fail-closed, граница не проверена",
        )
    # 3. инверсный GET через ЖИВОЙ proxy. trust_env=False: cron не грузит os.environ.
    # follow_redirects=False явно: 3xx ДОЛЖЕН вернуться ответом (=critical), а не уйти по редиректу.
    try:
        with httpx.Client(timeout=6.0, trust_env=False, proxy=proxy, follow_redirects=False) as c:
            r = c.get("http://169.254.169.254/latest/meta-data/")
        return ProbeResult(
            "fetch_egress_filtered", "critical",
            f"metadata REACHED via fetch-egress (http {r.status_code}) — фильтр НЕ режет private!",
        )
    except _BOUNDARY_BLOCKED_EXC as e:
        # connection-level (httpx refused/RST/timeout/protocol) ИЛИ socksio SOCKS-сбой через ЖИВОЙ proxy
        # = target не достигнут = граница держит = ok
        return ProbeResult("fetch_egress_filtered", "ok", f"private blocked via live proxy ({type(e).__name__})")
    except Exception as e:  # noqa: BLE001  setup/непредвиденное → граница НЕ проверена → warning, НЕ ложное ok
        return ProbeResult("fetch_egress_filtered", "warning", f"probe error, граница не проверена ({type(e).__name__})")


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
                            current = {"ts": ts, "iters": 0, "total_ms": 0, "ack_ms": None, "type": None, "outcome": "ok"}
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
                        # #140: исход хода ('breakdown' = провал; у планировщика
                        # iters всегда 0, поэтому считаем по outcome, а не iters).
                        if "outcome=" in line:
                            current["outcome"] = line.split("outcome=")[1].split()[0]
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
    # 2026-04-30: thresholds увеличены, добавлен минимум sample size.
    # Multi-iter LLM с tools (3+ iter) штатно занимает 15-25s — это не
    # аномалия, бот честно работает. Алертим только на p95 > 30s.
    # Минимум n=10 защищает от single-outlier на малой выборке (один
    # heavy 17s turn при n=3 ломал p95 в warning).
    traces = _recent_traces(window_min=30)
    if not traces:
        return ProbeResult("turn_latency_p95", "ok", "(no traces in 30m)")
    totals = [t["total_ms"] for t in traces if t["total_ms"] > 0]
    n = len(totals)
    if n < 10:
        # Не достаточно данных для p95 — outlier'ы доминируют.
        return ProbeResult(
            "turn_latency_p95", "ok",
            f"(n={n} < 10, p95 не считаем)",
        )
    p95 = _percentile(totals, 0.95)
    if p95 > 60_000:
        return ProbeResult("turn_latency_p95", "critical", f"p95={p95}ms (n={n})")
    if p95 > 30_000:
        return ProbeResult("turn_latency_p95", "warning", f"p95={p95}ms (n={n})")
    return ProbeResult("turn_latency_p95", "ok", f"p95={p95}ms (n={n})")


_FAILED_OUTCOMES = ("safe_reply", "breakdown")
# tool_error / fallback_used — НАМЕРЕННО не провал: ход восстановился внутри ReAct-петли,
# юзер получил реальный ответ. safe_reply — единственная видимая юзеру заглушка («потеряла контекст»).
# Абсолютный порог всплеска: N провалов за окно → алерт ДАЖЕ при низком трафике.
# Rate-гейт (n>=5 и >20%) на нашем трафике (~2 хода/час) почти не набирается → ночной
# сетевой сбой (4 safe_reply за часы) в 30-мин окно из 5 ходов мог не попасть (#227).
_FAILED_BURST = 3


def probe_failed_turns_rate() -> ProbeResult:
    # Источник истины по ReAct = БД react_turn_trace (#192). В trace.log у ReAct
    # ВСЕГДА outcome=ok (react_loop не зовёт mark_outcome) → парсер trace.log слеп
    # к провалам на всём прод-трафике (#227). Провал = ход отдал безопасную
    # заглушку safe_reply («Ой, потеряла контекст» — краш / таймаут / сетевой сбой
    # LLM) ИЛИ legacy breakdown (plan-execute). Знаменатель — только ЗАВЕРШЁННЫЕ
    # ходы (outcome IS NOT NULL): in_progress / paused-на-confirm в счёт не идут.
    out = _pg_query(
        "SELECT outcome, count(*) FROM react_turn_trace "
        "WHERE created_at > now() - interval '30 minutes' "
        "AND outcome IS NOT NULL GROUP BY outcome"
    )
    if out is None:
        return ProbeResult("failed_turns_rate", "warning", "psql query failed")
    counts: dict[str, int] = {}
    for line in out.splitlines():
        oc, sep, c = line.strip().partition("|")
        if sep:
            try:
                counts[oc] = int(c)
            except ValueError:
                pass
    n = sum(counts.values())
    if n == 0:
        return ProbeResult("failed_turns_rate", "ok", "(no finished react turns in 30m)")
    failed = sum(counts.get(o, 0) for o in _FAILED_OUTCOMES)
    pct = 100 * failed / n
    # Два триггера: абсолютный всплеск (низкий трафик — rate не наберёт n) ЛИБО доля (высокий трафик).
    if failed >= _FAILED_BURST or (n >= 5 and pct > 20):
        return ProbeResult("failed_turns_rate", "critical", f"{failed}/{n} react-turns failed ({pct:.0f}%)")
    return ProbeResult("failed_turns_rate", "ok", f"{failed}/{n} react-turns failed ({pct:.0f}%)")


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
    # Long-poll worker (см. plan mellow-discovering-conway.md). До cutover'а
    # пробы сами игнорят отсутствие systemd-юнита и колонки processing_status,
    # после cutover'а — заменят webhook_health.
    probe_telegram_poller_alive,
    probe_telegram_api_health,
    probe_unprocessed_inbound,
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
    probe_fetch_egress_filtered,  # #244: SSRF-граница fetch_url держится (инверсная: 200=critical)
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
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    proxy = _proxy_for_url(url)
    # #324: слать алерт через egress SOCKS — иначе при флапе прямого маршрута сам
    # алерт не доставится. trust_env=False: cron не грузит os.environ.
    _kw: dict[str, Any] = {"timeout": 10.0, "trust_env": False}
    if proxy:
        _kw["proxy"] = proxy
    try:
        with httpx.Client(**_kw) as c:
            r = c.post(
                url,
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
