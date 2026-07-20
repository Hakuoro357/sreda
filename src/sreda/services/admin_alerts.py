"""Admin alerts — отправка критических нотификаций владельцу инстанса.

Используется при serious issues которые юзер не должен замечать (или
заметит как «бот не отвечает»), но владельцу инстанса (Boris) надо
узнать сразу же. Канал — `settings.admin_telegram_chat_id`, default =
личный чат Boris с ботом (352612382).

Прецедент 2026-05-04: embedding endpoint потерялся в .env во время
SQLite→PG миграции, fallback на FakeEmbeddingClient → recall тихо
вернул [] всем юзерам ~3-4 дня. Никаких алертов в логах не было,
обнаружили только когда юзер вручную пожаловался. Этот модуль —
страхующая «жёлтая кнопка» от подобного silent degradation.

Прецедент 2026-05-12 → 2026-05-14: mimo reasoning_content contract bug
(R-27) пропустил **каждый tool-use turn** на fallback path 6 days.
Боris: «любой ответ от llm с ошибкой должен вызывать алертинг». R-28
расширяет module: ``send_admin_alert`` (sync) с DB dedup + burst cap +
severity-based rate limits.

## Public API

- ``alert_admin_async(text)`` — legacy async helper для lifespan/poller
  callers. Без dedup. Используется только когда вызывающий gating себя.
- ``send_admin_alert(severity, title, body, dedupe_key, extra_context)``
  — R-28 sync entry-point с full guards (dedup table + burst cap +
  severity rate-limit). **Use this для recurring error classes** (LLM
  fallback, tool errors, provider 5xx).

#395 (2026-07-20): оба входа доставляют DUAL — Telegram основной (шлём первым) +
MAX дубль (вторым), best-effort на канал, «доставлено» = долетело ≥1 канал.
Разворачивает R-28 «MAX-primary → TG-fallback» (решение владельца).

Both best-effort; exceptions swallowed.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError

from sreda.config.settings import get_settings


logger = logging.getLogger(__name__)


# R-28: severity → rate-limit window (seconds between re-fires per dedupe_key).
_RATE_LIMIT_BY_SEVERITY: dict[str, int] = {
    "P0": 60,        # acute — re-fire после 1 min
    "P1": 300,       # default — 5 min
    "P2": 600,       # less acute — 10 min
    "INFO": 1800,    # informational — 30 min
}

# R-28: process-level burst protection. Max alerts emitted per minute.
_BURST_CAP_PER_MINUTE = 10
_burst_window_lock = threading.Lock()
_burst_window: list[float] = []  # monotonic timestamps of recent sends

# R-28 R2 fix (Codex MAJOR): process-level in-flight registry for keyed locks.
# Same dedupe_key fired по multiple threads concurrently могут все pass
# _should_attempt_per_dedupe before any thread calls _mark_sent — каждый
# отправляет duplicate Telegram message. Lease pattern: first thread for
# a key claims с 30s TTL, others skip. Cleanup on POST completion.
#
# Process-local only — multi-worker race still possible (~N processes ×
# 1 duplicate per genuine event). Acceptable noise level for alerts.
# Cross-process would need pg_advisory_lock — overkill для observability.
_IN_FLIGHT_TTL_S = 30.0
_in_flight_lock = threading.Lock()
_in_flight: dict[str, float] = {}  # dedupe_key → expires_at (monotonic)


Severity = Literal["P0", "P1", "P2", "INFO"]


def _try_reserve_in_flight(dedupe_key: str) -> bool:
    """R-28 R2: claim ``dedupe_key`` for delivery. Returns False если другой
    thread уже claimed (либо in-flight, либо finished within 30s of failure
    that didn't release the lease — natural expiry).

    Lease auto-expires после ``_IN_FLIGHT_TTL_S`` без explicit release —
    safe-guard against thread crash leaving stale entry.
    """
    now = time.monotonic()
    with _in_flight_lock:
        # Prune expired entries (cheap O(N) scan, N≪50 in practice)
        expired = [k for k, v in _in_flight.items() if v <= now]
        for k in expired:
            _in_flight.pop(k, None)
        if dedupe_key in _in_flight:
            return False
        _in_flight[dedupe_key] = now + _IN_FLIGHT_TTL_S
        return True


def _release_in_flight(dedupe_key: str) -> None:
    """Release in-flight lease early (after POST + mark complete).

    Idempotent: missing key = no-op.
    """
    with _in_flight_lock:
        _in_flight.pop(dedupe_key, None)


async def alert_admin_async(text: str) -> bool:
    """Send a service alert through admin channel — DUAL: Telegram primary + MAX duplicate.

    #395 (2026-07-20): служебные алерты дублируются в ОБА канала — Telegram основной
    (шлём ПЕРВЫМ), MAX дубль (ВТОРЫМ). Best-effort на канал: сбой/таймаут одного НЕ
    мешает другому. Returns True если доставлено ХОТЯ БЫ в один канал. Разворачивает
    R-28 (2026-05-15: MAX-primary → TG-fallback) — решение владельца #395.

    Этот legacy async helper используют callers в ``main.py``/``embeddings.py``/
    ``miniapp.py``/``max_subscription_health.py``/``outbox_delivery.py`` и #376-нотификация
    расхождения доменов (``react_loop._dis376_send_alert``).

    Реализация: делегирует общему sync-хелперу ``_deliver_dual_sync`` через ОДИН
    ``asyncio.to_thread`` (оба канала в одном потоке, TG→MAX) — отмена корутины-awaiter'а
    не расщепляет каналы (поток доводит оба). Text обрезается до 4000 chars (MAX/TG limit).
    """
    import asyncio

    settings = get_settings()
    from sreda.config.bot_registry import TelegramBotRegistry
    _registry = TelegramBotRegistry.from_settings(settings)
    _admin_cfg = _registry.resolve(_registry.admin_bot_key)
    tg_bot_token = _admin_cfg.token or None
    tg_chat_id = settings.admin_telegram_chat_id
    max_bot_token = settings.max_bot_token
    max_chat_id = settings.admin_max_chat_id

    max_ok = bool(max_bot_token and max_chat_id)
    tg_ok = bool(tg_bot_token and tg_chat_id)
    if not (max_ok or tg_ok):
        logger.warning(
            "alert_admin: no channel configured (MAX or TG); skipping"
        )
        return False

    if len(text) > 4000:
        text = text[:3990] + "\n…[truncated]"

    # #395 (R1 sol): дуал ОДНИМ to_thread через общий sync-helper — оба канала (TG→MAX)
    # в одном потоке; отмена корутины-awaiter'а не расщепляет каналы (поток доводит оба).
    delivered, delivered_via = await asyncio.to_thread(
        _deliver_dual_sync, text,
        tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id,
        max_bot_token=max_bot_token, max_chat_id=max_chat_id,
    )
    if delivered:
        logger.info("alert_admin: delivered via %s", delivered_via)
    else:
        logger.warning("alert_admin: delivery failed across all channels")
    return delivered


# =====================================================================
# R-28: sync API with dedup + burst cap + severity-based rate limits
# =====================================================================


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_dedupe_key(severity: str, title: str) -> str:
    """Fallback dedupe_key derived from severity + title hash."""
    h = hashlib.sha256(f"{severity}:{title}".encode("utf-8")).hexdigest()[:16]
    return f"auto:{severity}:{h}"


def _check_burst_cap() -> bool:
    """Process-wide burst guard. True если OK to send, False если capped.

    Sliding 60s window, max 10 sends. Lock-protected (called from multiple
    threads — uvicorn workers, job_runner pool).
    """
    now = time.monotonic()
    with _burst_window_lock:
        cutoff = now - 60.0
        global _burst_window
        _burst_window = [t for t in _burst_window if t > cutoff]
        if len(_burst_window) >= _BURST_CAP_PER_MINUTE:
            return False
        _burst_window.append(now)
        return True


# Marker timestamp для "никогда не sent". UNIX epoch — гарантированно < любой
# threshold (now - window). При INSERT новой row сюда же кладём — first
# attempt всегда выходит за rate window → should_attempt=True.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _should_attempt_per_dedupe(
    dedupe_key: str,
    severity: str,
    title: str,
) -> tuple[bool, int]:
    """Phase 1: increment occurrence + check rate window WITHOUT marking sent.

    Codex R1 MAJOR: previous version updated ``last_sent_at=:now`` атомарно при
    upsert — если Telegram POST затем падал, следующее occurrence suppressed
    до конца severity window. Split на attempt-check vs sent-mark fixes это:
    - Phase 1 (this): always bump count, decide should_attempt by comparing
      EXISTING ``last_sent_at`` против threshold (без update).
    - Phase 2 (``_mark_sent`` below): set ``last_sent_at=now()`` после
      successful POST.

    Returns ``(should_attempt, occurrence_count)``:
    - ``should_attempt=True`` если row created OR existing last_sent_at <
      threshold (rate window passed or never sent successfully)
    - ``occurrence_count`` always incremented (audit trail)

    Fail-open: SQLAlchemyError → (True, 0) — over-send preferred над missed.
    """
    from sreda.db.session import privileged_session
    window_s = _RATE_LIMIT_BY_SEVERITY.get(severity, 300)
    now = _utcnow()
    threshold = now - timedelta(seconds=window_s)
    try:
        # #138 Ф2: пишется из daemon-потока (tenant_ctx НЕ наследуется) в ГЛОБАЛЬНУЮ
        # admin_alerts_seen → privileged (иначе под Ф3 RLS запись упрётся в fail-closed).
        with privileged_session("monitor") as session:
            # UPSERT increments count but never touches last_sent_at.
            # Initial INSERT uses _EPOCH (1970-01-01) → first call always
            # has last_sent_at < threshold → should_attempt=True.
            result = session.execute(
                sa_text(
                    "INSERT INTO admin_alerts_seen "
                    "(dedupe_key, severity, title, first_seen_at, last_sent_at, occurrence_count) "
                    "VALUES (:k, :sev, :title, :now, :epoch, 1) "
                    "ON CONFLICT (dedupe_key) DO UPDATE SET "
                    "occurrence_count = admin_alerts_seen.occurrence_count + 1 "
                    "RETURNING occurrence_count, "
                    "(last_sent_at < :threshold) AS should_attempt"
                ),
                {"k": dedupe_key, "sev": severity, "title": title[:256],
                 "now": now, "epoch": _EPOCH, "threshold": threshold},
            ).first()
            session.commit()
            if result is None:
                return False, 0
            return bool(result[1]), int(result[0])
    except SQLAlchemyError:
        logger.exception("admin_alerts: dedup check failed (fail-open send)")
        return True, 0


def _mark_sent(dedupe_key: str) -> None:
    """Phase 2: mark last_sent_at = now() после successful Telegram POST.

    Codex R1 MAJOR fix: only update sent-state if delivery actually succeeded.
    If POST fails — row keeps old last_sent_at, next occurrence re-attempts.

    Fail-soft: SQLAlchemyError logged but не propagated.
    """
    from sreda.db.session import privileged_session
    try:
        # #138 Ф2: daemon-поток + глобальная admin_alerts_seen → privileged.
        with privileged_session("monitor") as session:
            session.execute(
                sa_text(
                    "UPDATE admin_alerts_seen SET last_sent_at = :now "
                    "WHERE dedupe_key = :k"
                ),
                {"now": _utcnow(), "k": dedupe_key},
            )
            session.commit()
    except SQLAlchemyError:
        logger.exception("admin_alerts: _mark_sent update failed (key=%s)", dedupe_key)


def _post_telegram_sync(bot_token: str, chat_id: str, text: str) -> bool:
    """Sync httpx POST к Telegram sendMessage. Best-effort.

    5s timeout — bound caller latency. Не использует TelegramClient (async).
    """
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5.0,
        )
        if resp.status_code == 200:
            return True
        logger.warning(
            "admin_alerts: Telegram returned %s: %s",
            resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:  # noqa: BLE001 — must never crash caller
        # SECURITY: log only the exception class — logger.exception() would emit
        # the full traceback, and an httpx error's repr embeds the token-bearing
        # Telegram URL. (This path uses raw httpx.post, not TelegramClient.)
        logger.warning(
            "admin_alerts: Telegram POST failed: %s", type(exc).__name__
        )
        return False


def _post_max_sync(bot_token: str, chat_id: str, text: str) -> bool:
    """R-28 amendment 2026-05-15: sync httpx POST к MAX /messages.

    MAX recipient routing (per ``integrations/max/client.py`` probe
    2026-05-05): ``chat_id`` идёт в **query string**, не в body. Body
    содержит только ``{"text": "..."}``. Auth header — ``Authorization:
    <token>`` БЕЗ Bearer-префикса (MAX API contract).

    5s timeout как у TG — bound caller latency. Best-effort, exceptions
    swallowed → caller (fallback path) попробует TG.
    """
    try:
        # #214: адрес и TLS-доверие — из общих хелперов MAX-клиента
        # (platform-api2 + корни Минцифры; trust_env=False — RU напрямую).
        from sreda.integrations.max.client import max_base_url, max_ssl_context

        resp = httpx.post(
            f"{max_base_url()}/messages",
            params={"chat_id": chat_id},
            json={"text": text},
            headers={"Authorization": bot_token},
            timeout=5.0,
            trust_env=False,
            verify=max_ssl_context(),
        )
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "admin_alerts: MAX returned %s: %s",
            resp.status_code, resp.text[:200],
        )
        return False
    except Exception:  # noqa: BLE001 — must never crash caller
        logger.exception("admin_alerts: MAX POST failed")
        return False


def _deliver_dual_sync(
    text: str,
    *,
    tg_bot_token: str | None,
    tg_chat_id: str | None,
    max_bot_token: str | None,
    max_chat_id: str | None,
) -> tuple[bool, str | None]:
    """#395: доставка служебного алерта в ОБА канала — Telegram основной (ПЕРВЫМ) +
    MAX дубль (ВТОРЫМ), best-effort. ЕДИНЫЙ источник дуал-логики для обоих входов
    (``alert_admin_async`` и ``_deliver_in_thread``) — чтобы порядок/семантика не
    расходились (R1 sol). Сконфигурен один канал → уходит только туда.

    Каждый канал в своём try/except: ``_post_*_sync`` и так глотают исключения → bool,
    это двойная страховка (если будущая реализация бросит — сбой одного канала НЕ
    мешает другому).

    Returns ``(delivered, delivered_via)``: delivered=True если долетело ≥1 канал;
    delivered_via — какие каналы сработали ("telegram" / "max" / "telegram+max" / None).

    Синхронный: вызывается напрямую из daemon-потока (``_deliver_in_thread``) и из
    ``alert_admin_async`` ОДНИМ ``asyncio.to_thread`` — тогда отмена корутины-awaiter'а
    не расщепляет каналы (поток доводит оба).
    """
    _tg = False
    _max = False
    if tg_bot_token and tg_chat_id:
        try:
            _tg = _post_telegram_sync(tg_bot_token, tg_chat_id, text)
        except Exception as exc:  # noqa: BLE001 — best-effort, второй канал не блокируем
            logger.warning("admin_alerts: Telegram exception: %s", type(exc).__name__)
    if max_bot_token and max_chat_id:
        try:
            _max = _post_max_sync(max_bot_token, max_chat_id, text)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("admin_alerts: MAX exception: %s", type(exc).__name__)
    delivered_via = "+".join(c for c, s in (("telegram", _tg), ("max", _max)) if s) or None
    return (_tg or _max), delivered_via


def _deliver_in_thread(
    *,
    dedupe_key: str,
    severity: str,
    title: str,
    body: str,
    extra_context: dict | None,
    tg_bot_token: str | None,
    tg_chat_id: str | None,
    max_bot_token: str | None,
    max_chat_id: str | None,
) -> None:
    """Background-thread payload: dedup check + format + POST + mark.

    Runs в daemon thread spawned by ``send_admin_alert`` (Xiaomi R1 medium:
    decouples 5s HTTP latency from caller's request path). Exceptions
    swallowed via _post_*_sync/dedup fail-open. Caller never blocks.

    #395 (2026-07-20): **DUAL by default** — Telegram основной (POST'им ПЕРВЫМ) +
    MAX дубль (ВТОРЫМ). Каналы независимы, best-effort (каждый _post_*_sync сам
    глотает исключения → bool). ok=True если доставил ХОТЯ БЫ один; сконфигурен
    только один канал → уходит только туда. dedup/burst/mark_sent — один раз на
    alert (по dedupe_key), НЕ на канал, поэтому потеря одного канала не крутит
    ре-файр. Разворачивает R-28 (MAX-primary → TG-fallback) и поглощает #294
    (явный both_channels больше не нужен — дуал стал дефолтом).
    """
    # Phase 1: in-flight lease (Codex R3 MAJOR fix). MUST take BEFORE dedup
    # check — иначе stale-then-release race:
    #   A passes dedup (last_sent_at=epoch) → A reserves → POST → mark_sent →
    #   release. B passed dedup ранее (saw epoch too), arrives after A's
    #   release → reserves successfully → POSTs duplicate based on stale
    #   decision. Reserve-first ensures B blocked entirely (no stale decision
    #   to act on later) — lease guards the full critical section.
    if not _try_reserve_in_flight(dedupe_key):
        logger.info(
            "admin_alerts: in-flight lease held by another thread "
            "(key=%s), suppressing duplicate",
            dedupe_key,
        )
        return

    # Phase 2: dedup check (DB). Inside lease — only one thread does this per
    # key at a time. Read picks up any update from previously-completed thread.
    should_attempt, occurrence_count = _should_attempt_per_dedupe(
        dedupe_key, severity, title,
    )
    if not should_attempt:
        logger.info(
            "admin_alerts: rate-limited (key=%s occurrence=%d), suppressing",
            dedupe_key, occurrence_count,
        )
        _release_in_flight(dedupe_key)
        return

    # Phase 3: burst cap (process-wide). Codex R1 M1: AFTER dedup so suppressed
    # repeats не съедают quota.
    if not _check_burst_cap():
        logger.warning(
            "admin_alerts: burst cap hit, dropping alert (severity=%s title=%s)",
            severity, title,
        )
        _release_in_flight(dedupe_key)
        return

    # Phase 4: format message
    emoji = {"P0": "🔥", "P1": "🚨", "P2": "⚠️", "INFO": "ℹ️"}.get(severity, "🚨")
    message_parts = [
        f"{emoji} [SREDA {severity}] {title}",
        "",
        body,
    ]
    if occurrence_count > 1:
        message_parts.append("")
        message_parts.append(
            f"(seen #{occurrence_count} since first occurrence)"
        )
    if extra_context:
        message_parts.append("")
        for k, v in extra_context.items():
            message_parts.append(f"  • {k}: {v}")
    text_payload = "\n".join(message_parts)
    if len(text_payload) > 4000:
        text_payload = text_payload[:3950] + "\n\n…(truncated)"

    # Phase 4: deliver — DUAL by default (#395) через общий _deliver_dual_sync (Telegram
    # основной, MAX дубль; best-effort, ok если ≥1). mark_sent один раз на alert (после
    # ≥1 успеха) → потеря одного канала не крутит ре-файр. delivered_via — diagnostic-лог.
    try:
        ok, delivered_via = _deliver_dual_sync(
            text_payload,
            tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id,
            max_bot_token=max_bot_token, max_chat_id=max_chat_id,
        )
        if ok:
            logger.info(
                "admin_alerts: delivered via %s (key=%s severity=%s)",
                delivered_via, dedupe_key, severity,
            )
            # Phase 5: mark sent (Codex R1 M2: только после successful POST).
            _mark_sent(dedupe_key)
        else:
            logger.warning(
                "admin_alerts: delivery failed across all channels for key=%s, "
                "last_sent_at unchanged — will retry on next occurrence",
                dedupe_key,
            )
    finally:
        # Release in-flight lease (success or failure paths). Lease auto-expires
        # via TTL even if release missed (e.g. thread crash).
        _release_in_flight(dedupe_key)


def send_admin_alert(
    severity: Severity,
    title: str,
    body: str,
    *,
    dedupe_key: str | None = None,
    extra_context: dict | None = None,
) -> None:
    """R-28: send admin alert with dedup + burst cap + severity rate-limit.

    #395 (2026-07-20): доставка DUAL — Telegram основной (первым) + MAX дубль
    (вторым), best-effort на канал; «доставлено» = долетело ≥1 канал.

    **Fire-and-forget**: spawns daemon thread for delivery (Xiaomi R1 medium:
    avoids blocking caller на 5s HTTP timeout when caller is already на
    degraded error-recovery path). Caller returns immediately.

    Phases (executed в daemon thread):
      1. **Dedup**: drop if same ``dedupe_key`` sent within severity window.
      2. **Burst cap**: drop if >10 sends/min process-wide (after dedup —
         Codex R1 M1).
      3. **Format + POST**: dual-send — Telegram (primary) + MAX (duplicate).
      4. **Mark sent**: update ``last_sent_at`` только если POST succeeded
         (Codex R1 M2).

    Settings-check happens на caller thread (cheap) — early exit если no
    bot_token/chat_id (no thread spawn cost для disabled deployments).

    Args:
        severity: P0 (acute, 60s re-fire) / P1 (5min) / P2 (10min) /
                  INFO (30min)
        title: short single-line description shown в Telegram header
        body: multi-line context (tenant, exception, file refs)
        dedupe_key: stable id like ``"llm_fallback:BadRequestError:housewife_assistant"``.
                    None → derived from sha256(severity + title).
        extra_context: optional dict → serialized as " • key: value" footer
    """
    # Early exit checks на caller thread (no I/O)
    settings = get_settings()
    from sreda.config.bot_registry import TelegramBotRegistry
    _registry = TelegramBotRegistry.from_settings(settings)
    _admin_cfg = _registry.resolve(_registry.admin_bot_key)
    tg_bot_token = _admin_cfg.token or None
    tg_chat_id = settings.admin_telegram_chat_id
    max_bot_token = settings.max_bot_token
    max_chat_id = settings.admin_max_chat_id

    # R-28 amendment: at least one channel must be fully configured.
    # MAX configured = max_bot_token + admin_max_chat_id (primary).
    # TG configured = admin_bot_key token + admin_telegram_chat_id (fallback or legacy).
    max_ok = bool(max_bot_token and max_chat_id)
    tg_ok = bool(tg_bot_token and tg_chat_id)
    if not (max_ok or tg_ok):
        logger.debug(
            "admin_alerts: no channel configured (MAX or TG), "
            "skipping (severity=%s title=%s)",
            severity, title,
        )
        return

    key = dedupe_key or _default_dedupe_key(severity, title)

    # Spawn daemon thread — caller returns immediately (no 5s blocking).
    # Daemon=True → won't prevent process exit on shutdown.
    try:
        threading.Thread(
            target=_deliver_in_thread,
            kwargs={
                "dedupe_key": key,
                "severity": severity,
                "title": title,
                "body": body,
                "extra_context": extra_context,
                "tg_bot_token": tg_bot_token if tg_ok else None,
                "tg_chat_id": tg_chat_id if tg_ok else None,
                "max_bot_token": max_bot_token if max_ok else None,
                "max_chat_id": max_chat_id if max_ok else None,
            },
            daemon=True,
            name=f"admin-alert-{severity}",
        ).start()
    except Exception:  # noqa: BLE001 — thread spawn never raises in practice
        logger.exception("admin_alerts: thread spawn failed (key=%s)", key)

