"""Telegram long-polling worker.

Runs as a separate systemd unit (``sreda-telegram-poller.service``) and
calls Telegram's ``getUpdates`` in a loop. For each update it invokes
``services.telegram_inbound.handle_telegram_update``, then advances the
in-DB offset only after that ingest has committed (durable ingest →
offset advance order, idempotency by ``external_update_id`` covers the
crash window).

Why long-poll instead of webhook (2026-04-30 incident set):
  Connection initiated from our side → kernel TCP keepalive notices a
  dead connection in seconds and reopens. Inbound TCP from Telegram
  (Singapore → Timeweb Moscow) was being silently killed by some middle-
  box without RST, leaving Telegram's pool stuck for 30-60s; users saw
  «бот не отвечает». TCP-side palliatives helped but did not fix it
  fully — see plan ``mellow-discovering-conway.md``.

Process model:
  * Single-instance via PG advisory lock on a dedicated long-lived
    connection (NullPool, never returned to the engine pool).
  * Offset + heartbeat in dedicated tables (``poller_offsets``,
    ``poller_heartbeats``). Heartbeat fields distinguish liveness from
    upstream API health.
  * Exit codes:  0 normal,  2 lock held by another instance,
    3 active webhook conflict (must ``deleteWebhook`` manually).
  * ``--check-config`` runs startup (acquires lock, loads offset)
    then exits cleanly without polling.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
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
# 64-bit constant for ``pg_try_advisory_lock``. The high byte 0x5E
# stands for "Sreda"; the rest is arbitrary. Long-poller uses one fixed
# key per channel — when MAX is added we'll pick a sibling key.
LOCK_KEY_TELEGRAM = 0x5E_DA_7E_1E_60_AB_F0_1F
# Cap stored last_error so we don't blow up the heartbeats row when an
# exception body includes a multi-KB HTML 502 page or a full traceback.
LAST_ERROR_MAX_CHARS = 1000
# Channel column value in poller_offsets / poller_heartbeats.
CHANNEL = "telegram"


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
    def __init__(self, token: str, *, auto_delete_webhook: bool = False) -> None:
        self.token = token
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
                {"k": LOCK_KEY_TELEGRAM},
            ).scalar()
        except Exception:
            self._lock_conn.close()
            self._lock_engine.dispose()
            self._lock_conn = None
            self._lock_engine = None
            raise
        if not locked:
            self._lock_conn.close()
            self._lock_engine.dispose()
            self._lock_conn = None
            self._lock_engine = None
            raise SingletonLockError(
                "Another telegram poller already holds the advisory lock. "
                "Inspect `ps auxf | grep telegram_long_poll` and `pg_locks`. "
                "After fixing run `systemctl reset-failed sreda-telegram-poller` "
                "and `systemctl start sreda-telegram-poller`.",
            )
        self.offset = self._load_offset()
        logger.info(
            "telegram poller starting: offset=%d auto_delete_webhook=%s",
            self.offset, self.auto_delete_webhook,
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
                {"k": LOCK_KEY_TELEGRAM},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to release advisory lock")
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
                .filter_by(channel=CHANNEL)
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
                    channel=CHANNEL,
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
            row = session.get(PollerHeartbeat, CHANNEL)
            if row is None:
                row = PollerHeartbeat(
                    channel=CHANNEL,
                    last_attempt_at=now_ts,
                )
                session.add(row)
            row.last_attempt_at = now_ts
            if ok:
                row.last_ok_at = now_ts
                row.last_error = None
            else:
                row.last_error = (error or "")[:LAST_ERROR_MAX_CHARS]
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
                    await handle_telegram_update(upd)
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
                    "409 Conflict: an active webhook is still set on this bot. "
                    "Run `curl -X POST https://api.telegram.org/bot$TOKEN/deleteWebhook` "
                    "manually OR set SREDA_TELEGRAM_POLLER_AUTO_DELETE_WEBHOOK=true. "
                    "After fixing run `systemctl reset-failed sreda-telegram-poller` "
                    "and `systemctl start sreda-telegram-poller`.",
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
            "Run startup (acquire advisory lock + load offset) then exit "
            "cleanly without polling. Use to verify config/DB/lock before "
            "enabling the systemd unit at cutover."
        ),
    )
    return parser.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.error("SREDA_TELEGRAM_BOT_TOKEN is not set; refusing to start")
        return 1
    auto_delete = (
        os.environ.get("SREDA_TELEGRAM_POLLER_AUTO_DELETE_WEBHOOK", "")
        .strip().lower() == "true"
    )
    poller = TelegramLongPoller(
        settings.telegram_bot_token, auto_delete_webhook=auto_delete,
    )
    try:
        try:
            await poller.startup()
        except SingletonLockError:
            logger.error("singleton lock already held — aborting")
            return 2

        if args.check_config:
            logger.info(
                "config OK; would have polled with offset=%d", poller.offset,
            )
            return 0

        try:
            await poller.run_forever()
        except TelegramConflictError:
            return 3
        return 0
    finally:
        await poller.shutdown()


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
