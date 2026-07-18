"""Telegram long-polling worker.

Runs as a separate systemd unit (``sreda-telegram-poller@<bot_key>.service``)
and calls Telegram's ``getUpdates`` in a loop.  For each update it invokes
``services.telegram_inbound.handle_telegram_update``, then advances the
in-DB offset only after that ingest has committed (durable ingest →
offset advance order, idempotency by ``external_update_id`` covers the
crash window).  An update that crashes the handler deterministically
(``MAX_UPDATE_ATTEMPTS`` consecutive failures) is dead-lettered instead:
the offset advances past it and an admin alert fires — otherwise one
poison update would stall the bot's whole inbound (head-of-line) while
the heartbeat stayed green (audit 2026-07-18).

Why long-poll instead of webhook (2026-04-30 incident set):
  Connection initiated from our side → kernel TCP keepalive notices a
  dead connection in seconds and reopens. Inbound TCP from Telegram
  (Singapore → Timeweb Moscow) was being silently killed by some middle-
  box without RST, leaving Telegram's pool stuck for 30-60s; users saw
  «бот не отвечает». TCP-side palliatives helped but did not fix it
  fully — see plan ``mellow-discovering-conway.md``.

Process model (Phase 3 — per-bot):
  * Each poller instance is started with ``--bot-key <key>`` (or env
    ``SREDA_TELEGRAM_BOT_KEY``).  Token / username are resolved from
    ``TelegramBotRegistry``.
  * Single-instance guarantee per bot via PG advisory lock whose key is
    deterministically derived from ``bot_key`` (SHA-256, 64-bit signed).
    Two bots → two different lock ids → two pollers can coexist safely.
  * Offset + heartbeat in dedicated tables (``poller_offsets``,
    ``poller_heartbeats``), keyed by ``"telegram:<bot_key>"``.
  * Exit codes:  0 normal,  2 lock held by another instance,
    3 active webhook conflict (must ``deleteWebhook`` manually).
  * ``--check-config`` verifies token↔bot via Telegram ``getMe``, then
    exits cleanly without polling.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from sreda.config.logging import configure_logging
from sreda.config.settings import get_settings
from sreda.db.models.poller_state import PollerHeartbeat, PollerOffset
from sreda.db.session import get_session_factory
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.services.telegram_inbound import handle_telegram_update

logger = logging.getLogger(__name__)


# ---- Constants ---------------------------------------------------------

# Long-poll wait passed to Telegram's getUpdates. 25s is a safe value:
# below the 30s socket timeout we set on httpx and well above human
# typing latency.
POLL_TIMEOUT_SECS = 25
# httpx timeout = long-poll timeout + slack. Must be > POLL_TIMEOUT_SECS
# so that an empty long-poll (200 OK with []) is not classified as
# httpx.TimeoutException.
HTTP_TIMEOUT_SECS = POLL_TIMEOUT_SECS + 5
# Backoff after network/HTTP errors. Linear, no exponential — getUpdates
# is cheap and we want to recover quickly when TG comes back.
BACKOFF_SECS = 2
# Cap stored last_error so we don't blow up the heartbeats row when an
# exception body includes a multi-KB HTML 502 page or a full traceback.
LAST_ERROR_MAX_CHARS = 1000
# Audit 2026-07-18 (#1): poison-update guard. How many consecutive times
# the same update may crash the handler before we call it poison and
# dead-letter it (advance the offset past it + admin alert). Before this
# guard the offset moved ONLY after a successful handle, so one
# deterministically-crashing update blocked the bot's entire inbound
# (head-of-line) for up to ~24h (Telegram-side update TTL).
MAX_UPDATE_ATTEMPTS = 3


def _advisory_lock_id(bot_key: str) -> int:
    """Derive a stable 64-bit signed advisory lock id from *bot_key*.

    Uses SHA-256 so the result is deterministic across Python restarts
    (unlike ``hash()`` which is randomised per-process by PYTHONHASHSEED).
    Takes the first 8 bytes of the digest and interprets them as a big-
    endian signed 64-bit integer — the range ``pg_advisory_lock`` accepts.

    Two different bot_keys always produce different ids (no collision for
    any realistic set of bot keys; SHA-256 prefix collisions are
    negligible at 2 items).
    """
    digest = hashlib.sha256(
        f"sreda-telegram-poller:{bot_key}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _poller_channel(bot_key: str) -> str:
    """Return the ``channel`` key used in ``poller_offsets`` / ``poller_heartbeats``.

    Format: ``"telegram:<bot_key>"``.  The legacy single-bot key
    ``"telegram"`` was migrated to ``"telegram:sreda"`` in migration
    20260603_0049; any ``--bot-key sreda`` poller will resume seamlessly.
    """
    return f"telegram:{bot_key}"


# ---- Typed exit-path exceptions ----------------------------------------

class SingletonLockError(RuntimeError):
    """Raised when ``pg_try_advisory_lock`` returns false on startup —
    another instance of the poller is already running. ``main()`` maps
    this to exit code 2; systemd is configured with
    ``RestartPreventExitStatus=2 3`` so a duplicate launch will not be
    auto-restarted into a tight loop."""


class TelegramConflictError(RuntimeError):
    """Raised when ``getUpdates`` returns 409 Conflict — Telegram still
    has an active webhook for this bot, so it refuses to surface
    updates via long-poll. ``main()`` maps to exit code 3. Recovery
    requires either calling ``deleteWebhook`` manually or restarting
    the worker with ``SREDA_TELEGRAM_POLLER_AUTO_DELETE_WEBHOOK=true``
    (only safe at planned cutover, never as default)."""


# ---- Time helper -------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- Poller class ------------------------------------------------------

class TelegramLongPoller:
    def __init__(
        self,
        token: str,
        *,
        bot_key: str = "sreda",
        auto_delete_webhook: bool = False,
    ) -> None:
        self.token = token
        self.bot_key = bot_key
        self.auto_delete_webhook = auto_delete_webhook
        self.client = TelegramClient(token)
        self.SessionLocal = get_session_factory()
        # Dedicated connection for the advisory lock — separate from the
        # main session pool so that a query-side rollback can never wipe
        # the lock, and the connection never lands back in a pool where
        # something else might use it.
        self._lock_engine: Engine | None = None
        self._lock_conn: Connection | None = None
        self.offset: int = 0
        # Poison-update guard: consecutive handler failures per update_id.
        # Entries are popped on success / dead-letter, so only updates
        # currently failing occupy the map.
        self._update_failures: dict[int, int] = {}
        # Per-bot derived values.
        self._lock_key: int = _advisory_lock_id(bot_key)
        self._channel: str = _poller_channel(bot_key)

    async def startup(self) -> None:
        """Acquire the singleton lock + load offset.

        Order matters: lock first, then offset. A second instance that
        starts while we hold the lock fails fast on the lock and never
        races on offset load.
        """
        settings = get_settings()
        self._lock_engine = create_engine(
            settings.database_url, poolclass=NullPool,
        )
        self._lock_conn = self._lock_engine.connect()
        try:
            locked = self._lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": self._lock_key},
            ).scalar()
        except Exception:
            self._lock_conn.close()
            self._lock_engine.dispose()
            self._lock_conn = None
            self._lock_engine = None
            raise
        if locked:
            # R-23: commit the implicit autobegin transaction so the
            # connection returns to `idle` state. pg_try_advisory_lock is
            # SESSION-level — lock persists across commit and is released
            # only on session close or explicit pg_advisory_unlock. Without
            # this commit, SQLAlchemy 2.x autobegin leaves the connection
            # in `idle in transaction` state forever, blocking re-enable of
            # `idle_in_transaction_session_timeout` (defense-in-depth from
            # R-18, currently reverted on prod).
            self._lock_conn.commit()
        if not locked:
            self._lock_conn.close()
            self._lock_engine.dispose()
            self._lock_conn = None
            self._lock_engine = None
            raise SingletonLockError(
                f"Another telegram poller for bot_key={self.bot_key!r} already "
                "holds the advisory lock. "
                "Inspect `ps auxf | grep telegram_long_poll` and `pg_locks`. "
                "After fixing run `systemctl reset-failed sreda-telegram-poller@"
                f"{self.bot_key}` and `systemctl start "
                f"sreda-telegram-poller@{self.bot_key}`.",
            )
        self.offset = self._load_offset()
        logger.info(
            "telegram poller starting: bot_key=%s channel=%s offset=%d "
            "auto_delete_webhook=%s",
            self.bot_key, self._channel, self.offset, self.auto_delete_webhook,
        )

    async def shutdown(self) -> None:
        """Release the advisory lock and dispose the dedicated engine.

        Idempotent: safe to call on every exit path including the case
        where ``startup`` itself raised before the lock was acquired."""
        if self._lock_conn is None:
            return
        try:
            self._lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": self._lock_key},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to release advisory lock")
        else:
            # R-23 symmetry: end the implicit autobegin transaction after
            # unlock so the connection does not sit in `idle in transaction`
            # between unlock and close. Advisory locks are session-level,
            # so commit does not affect lock state (the unlock above
            # already released it). Separate from the unlock try/except per
            # Qwen R-23 review M1: distinguish unlock-failed vs commit-failed
            # in the logs.
            try:
                self._lock_conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to commit after advisory unlock (lock already released)",
                )
        finally:
            try:
                self._lock_conn.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._lock_engine is not None:
                    self._lock_engine.dispose()
            except Exception:  # noqa: BLE001
                pass
            self._lock_conn = None
            self._lock_engine = None

    def _load_offset(self) -> int:
        """Read the saved offset from PostgreSQL. Returns 0 when no row
        exists (first start), which causes Telegram to deliver any
        updates it currently holds."""
        with self.SessionLocal() as session:
            row = (
                session.query(PollerOffset)
                .filter_by(channel=self._channel)
                .first()
            )
            return row.last_update_id + 1 if row else 0

    def _save_offset(self, update_id: int) -> None:
        """Persist last successfully-ingested update_id. Called only
        after ``handle_telegram_update`` has returned (durable ingest
        is committed) — so a crash between the two commits is safe:
        the next ``getUpdates`` re-delivers the same update and
        ``persist_inbound_event`` short-circuits on duplicate."""
        with self.SessionLocal() as session:
            session.merge(
                PollerOffset(
                    channel=self._channel,
                    last_update_id=update_id,
                    updated_at=_utcnow(),
                )
            )
            session.commit()
        self.offset = update_id + 1

    def _save_heartbeat(self, *, ok: bool, error: str | None = None) -> None:
        """Update the heartbeat row.

        ``last_attempt_at`` always advances — the monitor probe uses it
        as a liveness signal (the process is up and making requests),
        independent of whether Telegram itself is healthy.

        ``last_ok_at`` advances only on successful API replies and
        powers a separate health probe — when ``last_attempt_at`` is
        fresh but ``last_ok_at`` is stale, Telegram is down rather than
        the poller, and the alert is downgraded from critical to
        warning.
        """
        now_ts = _utcnow()
        with self.SessionLocal() as session:
            row = session.get(PollerHeartbeat, self._channel)
            if row is None:
                row = PollerHeartbeat(
                    channel=self._channel,
                    last_attempt_at=now_ts,
                )
                session.add(row)
            row.last_attempt_at = now_ts
            if ok:
                row.last_ok_at = now_ts
                row.last_error = None
            else:
                # #95: last_error виден в админке/БД — редактируем токен
                # (heartbeat не проходит через лог-фильтр)
                from sreda.config.log_redaction import redact_secrets
                # редактируем ДО обрезки: срез мог разрезать токен так,
                # что регэксп его не узнаёт (Codex R1)
                row.last_error = redact_secrets(
                    error or "")[:LAST_ERROR_MAX_CHARS]
            session.commit()

    async def _fetch_updates(self) -> list[dict]:
        """One ``getUpdates`` call. Translates the 409-Conflict body
        into ``TelegramConflictError`` and lets every other failure
        bubble up to ``run_forever``'s catch-all."""
        try:
            result = await self.client._post_request(
                "getUpdates",
                timeout=HTTP_TIMEOUT_SECS,
                json={
                    "offset": self.offset,
                    "timeout": POLL_TIMEOUT_SECS,
                    "allowed_updates": [
                        "message",
                        "edited_message",
                        "callback_query",
                    ],
                },
            )
        except TelegramDeliveryError as exc:
            if exc.status_code == 409:
                raise TelegramConflictError(str(exc)) from exc
            raise
        # On non-error 200 OK Telegram returns {"ok": true, "result": [...]}.
        # If "ok" is False, _post_request would already have raised
        # TelegramDeliveryError — but defend against silent shape
        # changes anyway.
        if not result.get("ok"):
            description = str(result.get("description") or "")
            if "Conflict" in description:
                raise TelegramConflictError(description)
            raise RuntimeError(f"getUpdates non-ok: {result!r}")
        return result.get("result") or []

    async def run_forever(self) -> None:
        while True:
            try:
                updates = await self._fetch_updates()
                self._save_heartbeat(ok=True)
                for upd in updates:
                    update_id = upd.get("update_id")
                    if not isinstance(update_id, int):
                        # Defensive: skip malformed updates without
                        # advancing the offset (otherwise we'd silently
                        # drop a real one if Telegram surprises us).
                        logger.warning(
                            "skipping update without integer update_id: %r",
                            upd,
                        )
                        continue
                    try:
                        await handle_telegram_update(upd, bot_key=self.bot_key)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — poison-update guard
                        if await self._handle_update_failure(update_id, upd, exc):
                            # Will retry: offset not advanced, so the next
                            # fetch re-delivers this update first. The rest
                            # of the batch stays queued behind it; sleep so
                            # a deterministic crash can't hot-loop (a fetch
                            # with pending updates returns immediately).
                            await asyncio.sleep(BACKOFF_SECS)
                            break
                        # Poison update dead-lettered — carry on with the
                        # rest of the batch.
                        continue
                    self._update_failures.pop(update_id, None)
                    self._save_offset(update_id)
            except asyncio.CancelledError:
                raise
            except TelegramConflictError as exc:
                if self.auto_delete_webhook:
                    logger.warning(
                        "409 Conflict — deleting webhook (auto_delete_webhook=true): %s",
                        exc,
                    )
                    try:
                        await self.client._post_request(
                            "deleteWebhook",
                            timeout=10.0,
                            json={"drop_pending_updates": False},
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "auto-deleteWebhook failed, re-raising original 409",
                        )
                        raise
                    continue
                # Re-raise so main() returns a clean exit code 3.
                logger.error(
                    "409 Conflict: an active webhook is still set on bot_key=%s. "
                    "Run `curl -X POST https://api.telegram.org/bot$TOKEN/deleteWebhook` "
                    "manually OR set SREDA_TELEGRAM_POLLER_AUTO_DELETE_WEBHOOK=true. "
                    "After fixing run `systemctl reset-failed sreda-telegram-poller@"
                    "%s` and `systemctl start sreda-telegram-poller@%s`.",
                    self.bot_key, self.bot_key, self.bot_key,
                )
                raise
            except httpx.TimeoutException as exc:
                logger.warning("network timeout on getUpdates: %s", exc)
                self._save_heartbeat(ok=False, error=f"timeout: {exc}")
                await asyncio.sleep(BACKOFF_SECS)
            except Exception as exc:  # noqa: BLE001
                logger.exception("poller iteration error")
                self._save_heartbeat(
                    ok=False, error=f"{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(BACKOFF_SECS)

    async def _handle_update_failure(
        self, update_id: int, upd: dict, exc: Exception
    ) -> bool:
        """Poison-update guard (audit 2026-07-18 #1).

        Returns True when the failure was counted and the update should be
        retried later (offset NOT advanced — caller breaks the batch and
        backs off). Returns False when the update hit
        ``MAX_UPDATE_ATTEMPTS`` and was dead-lettered: the offset is
        advanced past it and an admin alert fires, so the rest of the
        batch may proceed.

        Why dead-letter at all: before this guard the offset moved only
        after a successful handle, so a deterministically-crashing update
        re-ran every iteration and stalled the bot's whole inbound
        (head-of-line) while ``_save_heartbeat(ok=True)`` — recorded right
        after the fetch — kept every health probe green. Dead-letter loses
        exactly one update but unblocks everyone behind it, and the
        critical log + alert make the loss visible (the audit's core
        complaint was the SILENT failure mode).
        """
        attempts = self._update_failures.get(update_id, 0) + 1
        self._update_failures[update_id] = attempts
        if attempts < MAX_UPDATE_ATTEMPTS:
            logger.exception(
                "telegram update %s failed (attempt %d/%d) — will retry",
                update_id, attempts, MAX_UPDATE_ATTEMPTS,
            )
            return True
        self._update_failures.pop(update_id, None)
        logger.critical(
            "telegram update %s crashed handler %d times — DEAD-LETTER: "
            "advancing offset past it (bot_key=%s, error=%s: %s)",
            update_id, attempts, self.bot_key, type(exc).__name__, exc,
        )
        self._save_offset(update_id)
        await self._alert_poison_update(update_id, upd, exc)
        return False

    async def _alert_poison_update(
        self, update_id: int, upd: dict, exc: Exception
    ) -> None:
        """Best-effort admin alert about a dead-lettered poison update.

        Never raises — alerting must not crash the poller. The update
        payload itself is NOT included (it carries personal chat data);
        only the update's type keys and the redacted error.
        """
        try:
            from sreda.config.log_redaction import redact_secrets
            from sreda.services.admin_alerts import send_admin_alert

            send_admin_alert(
                "P0",
                f"telegram poller: poison update {update_id} dead-lettered",
                redact_secrets(
                    f"bot_key={self.bot_key}\n"
                    f"update_keys={sorted(upd.keys())}\n"
                    f"error={type(exc).__name__}: {exc}"
                )[:LAST_ERROR_MAX_CHARS],
                dedupe_key=f"telegram-poison-update:{self.bot_key}:{update_id}",
            )
        except Exception:  # noqa: BLE001 — alert must never kill the poller
            logger.exception("failed to send poison-update admin alert")


# ---- Entry point -------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sreda.workers.telegram_long_poll",
        description="Sreda Telegram long-poller (see plan mellow-discovering-conway.md)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help=(
            "Verify bot config via Telegram getMe (token↔username match + token "
            "uniqueness), acquire advisory lock, load offset, then exit cleanly "
            "without polling. Use before enabling the systemd unit at cutover."
        ),
    )
    parser.add_argument(
        "--bot-key",
        default=None,
        help=(
            "Bot key to poll (e.g. 'sreda', 'sreda_home'). "
            "Overrides SREDA_TELEGRAM_BOT_KEY env var. "
            "Must match a key in TelegramBotRegistry. "
            "Fail-closed if unknown."
        ),
    )
    return parser.parse_args(argv)


def _install_signal_handlers(main_task: asyncio.Task) -> bool:
    """Wire SIGTERM / SIGINT to cancel the running main task.

    Why: при ``systemctl stop sreda-telegram-poller@<key>`` systemd шлёт
    SIGTERM. Если мы не установим asyncio-aware handler, Python в
    стандартном поведении НЕ конвертирует SIGTERM в CancelledError
    (только SIGINT/Ctrl+C). Поллер просто продолжит крутиться, systemd
    подождёт TimeoutStopSec (по умолчанию 90с) и пошлёт SIGKILL.
    SIGKILL — жёсткий kill, никакие ``finally`` не срабатывают, в
    частности `pg_advisory_unlock` не вызывается.

    PostgreSQL **обычно** освобождает advisory lock сразу как connection
    закрывается (TCP RST после убитого процесса), но при keepalive +
    server-side idle linger возможно окно секунд-десятки когда lock
    висит «осиротевший». Следующий старт получит SingletonLockError
    (exit 2) и из-за ``RestartPreventExitStatus=2 3`` systemd не
    перезапустит сервис автоматически — нужно ручное reset-failed.

    С installed signal handlers: SIGTERM → main_task.cancel() →
    CancelledError на ближайшем await → finally блок _amain →
    poller.shutdown() → явный pg_advisory_unlock → clean exit.

    Returns True если handlers установлены, False если платформа их
    не поддерживает (Windows, либо запуск из non-main thread).
    """
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, main_task.cancel)
        return True
    except (NotImplementedError, RuntimeError, AttributeError):
        # NotImplementedError: Windows
        # RuntimeError: not main thread
        # AttributeError: signal module missing some signals
        return False


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    # --- Resolve bot_key -------------------------------------------------
    # Priority: --bot-key CLI > SREDA_TELEGRAM_BOT_KEY env > legacy default.
    bot_key: str | None = args.bot_key
    if bot_key is None:
        bot_key = os.environ.get("SREDA_TELEGRAM_BOT_KEY", "").strip() or None
    if bot_key is None:
        # Backward-compat: if neither flag nor env given, behave like legacy
        # single-bot start with "sreda".  The legacy unit
        # sreda-telegram-poller.service (no --bot-key) falls here.
        bot_key = "sreda"

    # --- Resolve registry ------------------------------------------------
    from sreda.config.bot_registry import TelegramBotRegistry, telegram_client_for

    try:
        registry = TelegramBotRegistry.from_settings(settings)
        cfg = registry.resolve(bot_key)
    except KeyError as exc:
        logger.error(
            "Unknown bot_key %r — refusing to start. "
            "Check SREDA_TELEGRAM_BOT_KEY / --bot-key and registry config. "
            "Detail: %s",
            bot_key, exc,
        )
        return 1

    token = cfg.token
    if not token:
        logger.error(
            "bot_key=%r has an empty token; refusing to start. "
            "Set the corresponding token env var.",
            bot_key,
        )
        return 1

    logger.info("telegram poller resolving bot_key=%s username=%s", bot_key, cfg.username)

    # --- --check-config: verify token↔bot before acquiring lock ----------
    if args.check_config:
        return await _run_check_config(bot_key, registry, telegram_client_for)

    # Startup check: embeddings (2026-05-04 lesson). Best-effort, не блокирует
    # старт — отправит alert в admin chat при проблеме.
    try:
        from sreda.services.embeddings import (
            assert_embeddings_configured_or_alert,
        )
        await assert_embeddings_configured_or_alert()
    except Exception:  # noqa: BLE001
        logger.warning("embeddings startup-check raised", exc_info=True)

    auto_delete = (
        os.environ.get("SREDA_TELEGRAM_POLLER_AUTO_DELETE_WEBHOOK", "")
        .strip().lower() == "true"
    )
    poller = TelegramLongPoller(
        token,
        bot_key=bot_key,
        auto_delete_webhook=auto_delete,
    )

    # Issue #68 hotfix: the telegram-poller process runs handlers.chat()
    # in its OWN asyncio loop — NOT through FastAPI. The lifespan
    # startup_writer() в main.py covers only uvicorn; without an
    # equivalent here, every request envelope hits
    # `_WRITER_READY.is_set() == False` → DROPPED + P1 admin alert.
    # See post-deploy incident 2026-05-23.
    try:
        from sreda.services import llm_trace
        await llm_trace.startup_writer()
        logger.info("llm-trace writer initialised в telegram-poller loop")
    except Exception:  # noqa: BLE001 — additive feature must not break poller boot
        logger.warning(
            "llm-trace startup_writer raised в telegram-poller — "
            "trace persist will degrade fail-open",
            exc_info=True,
        )

    # Graceful shutdown on SIGTERM / SIGINT — see helper docstring.
    # Falls back silently on platforms that don't support
    # loop.add_signal_handler (Windows tests).
    main_task = asyncio.current_task()
    if main_task is not None:
        installed = _install_signal_handlers(main_task)
        if installed:
            logger.info("signal handlers installed: SIGTERM/SIGINT → cancel main")

    try:
        try:
            await poller.startup()
        except SingletonLockError:
            logger.error("singleton lock already held — aborting (bot_key=%s)", bot_key)
            return 2

        try:
            await poller.run_forever()
        except TelegramConflictError:
            return 3
        except asyncio.CancelledError:
            # Graceful shutdown via SIGTERM/SIGINT — main task was
            # cancelled by signal handler. Swallow CancelledError so
            # finally runs cleanly and we exit 0.
            logger.info("received shutdown signal — exiting cleanly")
            return 0
        return 0
    finally:
        await poller.shutdown()
        # Drain llm-trace queue before exit — same semantics as FastAPI
        # lifespan shutdown в main.py. 10s timeout matches uvicorn's.
        try:
            from sreda.services import llm_trace
            await llm_trace.shutdown_drain(timeout_seconds=10.0)
        except Exception:  # noqa: BLE001
            logger.warning(
                "llm-trace shutdown_drain raised в telegram-poller",
                exc_info=True,
            )


async def _run_check_config(
    bot_key: str,
    registry: object,
    telegram_client_for_fn: object,
) -> int:
    """Run the --check-config verification path.

    1. Call ``verify_bot_configs`` for the configured bot_key (token
       uniqueness + getMe username match).
    2. Fail-closed if bot has a token but NO username (swap-check can't
       run silently).
    3. Acquire the advisory lock + load offset, then release and exit 0.

    Network unreachable: reported as a warning, continues (best-effort
    connectivity at config-check time).  Username mismatch: hard fail (rc=1).
    """
    from sreda.config.bot_registry import verify_bot_configs

    cfg = registry.resolve(bot_key)

    # Fail-closed if token set but username not — swap-check can't verify.
    if cfg.token and not cfg.username:
        logger.error(
            "--check-config: bot_key=%r has a token but no username configured. "
            "Set the corresponding USERNAME env var so getMe can verify the "
            "token↔bot mapping.  Refusing to start.",
            bot_key,
        )
        return 1

    # Build a get_me callable that builds a fresh client per token so
    # verify_bot_configs compares the right bot's getMe to its username
    # (not the currently-selected bot_key's client for every bot).
    async def _real_get_me(token: str) -> dict:
        per_token_client = TelegramClient(token)
        try:
            body = await per_token_client.get_me()
            return body.get("result") or {}
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # True network error (TCP/DNS/SOCKS unreachable) — re-raise so
            # the outer handler can treat it as transient and skip.
            logger.warning(
                "--check-config: getMe network error for token=*** (%s). "
                "Skipping username verification.",
                exc,
            )
            raise
        # Any other exception (TelegramDeliveryError for ok=false / 4xx / 5xx,
        # bad-token 401, etc.) propagates as-is → outer handler hard-fails.

    # Run verify_bot_configs — checks token uniqueness + username match
    # for ALL bots in the registry, as the plan specifies.
    network_unreachable = False
    try:
        await verify_bot_configs(registry, get_me=_real_get_me)
        logger.info("--check-config: verify_bot_configs passed (all bots ok)")
    except ValueError as exc:
        # Hard fail: token duplicate or username mismatch.
        logger.error("--check-config: FAILED — %s", exc)
        return 1
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        # getMe network error: report but don't block the rest of the check.
        logger.warning(
            "--check-config: getMe network error — skipping username verification "
            "(%s). Advisory lock + offset check will still run.",
            exc,
        )
        network_unreachable = True
    except Exception as exc:  # noqa: BLE001
        # Telegram API error / bad token / ok=false — hard config fail.
        logger.error(
            "--check-config: FAILED — getMe returned a config error "
            "(bad token, Telegram API rejection, or ok=false): %s",
            exc,
        )
        return 1

    # --- Acquire lock + load offset, then release ------------------------
    # Re-use a temporary poller just for the lock/offset dance.
    poller = TelegramLongPoller(
        cfg.token,
        bot_key=bot_key,
    )
    try:
        await poller.startup()
    except SingletonLockError:
        logger.error(
            "--check-config: singleton lock held by another instance (bot_key=%s). "
            "The poller is already running — check-config cannot acquire the lock.",
            bot_key,
        )
        return 2

    logger.info(
        "--check-config: OK — bot_key=%s channel=%s offset=%d%s",
        bot_key,
        poller._channel,
        poller.offset,
        " (getMe skipped — network unreachable)" if network_unreachable else "",
    )
    await poller.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        feature_requests_log_path=settings.feature_requests_log_path,
        trace_log_path=settings.trace_log_path,
    )
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
