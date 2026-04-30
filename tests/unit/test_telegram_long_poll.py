"""Unit tests for ``sreda.workers.telegram_long_poll``.

Most tests bypass the real ``pg_try_advisory_lock`` call (SQLite
fixture has no such function) and exercise the poller's own logic:
offset/heartbeat persistence, the 409-handling branch, the durable-
ingest → offset-advance ordering, and the ``--check-config`` exit
path.

A separate test patches the lock helper to assert the
``SingletonLockError`` exit code path itself.
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.core import Tenant, User, Workspace
from sreda.db.models.poller_state import PollerHeartbeat, PollerOffset
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramDeliveryError
from sreda.workers import telegram_long_poll as tlp
from sreda.workers.telegram_long_poll import (
    SingletonLockError,
    TelegramConflictError,
    TelegramLongPoller,
)

EXISTING_CHAT_ID = "100000003"


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """Build an empty SQLite DB and return the poller-token settings.

    Re-uses the same caches the production code reads from, but clears
    them at the boundaries of the test so successive tests do not
    leak state through the lru_cache on ``get_settings`` / ``get_engine``
    / ``get_session_factory``.
    """
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef"
    ).decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _make_poller_without_lock(*, auto_delete_webhook: bool = False) -> TelegramLongPoller:
    """Build a poller and stub out the dedicated lock connection.

    SQLite has no ``pg_try_advisory_lock`` so we cannot exercise
    ``startup()`` end-to-end; instead, every test that does not care
    about the lock semantics calls this helper, which leaves
    ``self._lock_conn`` non-None (so ``shutdown`` is a no-op apart
    from clearing the attributes) and lets us call ``run_forever`` /
    ``_save_*`` against the SQLite DB just like in production.
    """
    poller = TelegramLongPoller("test-token", auto_delete_webhook=auto_delete_webhook)
    poller._lock_conn = MagicMock()
    poller._lock_engine = MagicMock()
    return poller


class _FetchScript:
    """Test double for _fetch_updates with an explicit script.

    Yields each batch in ``batches`` exactly once, then blocks
    indefinitely on ``asyncio.sleep`` so the run_forever loop has a
    real await point at which a ``cancel()`` can be delivered. Without
    this, the loop spins in tight CPU because AsyncMock's coroutine
    returns synchronously and never yields control to the cancel task.
    """

    def __init__(self, batches: list[list[dict]]):
        self.batches = list(batches)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.batches:
            return self.batches.pop(0)
        # Park forever — caller cancels the task to break out.
        await asyncio.sleep(60)
        return []


async def _run_once_then_cancel(poller: TelegramLongPoller, *, settle: float = 0.1) -> None:
    """Run ``poller.run_forever`` long enough to drain the scripted
    fetcher, then cancel and wait for the task to clean up. ``settle``
    is the upper bound on how long we wait for the script to play out;
    the cancel itself is best-effort."""
    run_task = asyncio.create_task(poller.run_forever())
    try:
        await asyncio.sleep(settle)
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, BaseException):
            pass


# ---- Offset + handle ordering -----------------------------------------


@pytest.mark.asyncio
async def test_offset_advances_only_after_handle_succeeds(fresh_db):
    """Successful handle → offset row updated to update_id."""
    poller = _make_poller_without_lock()

    handled: list[int] = []

    async def fake_handle(payload, *, bot_key="sreda"):
        handled.append(int(payload["update_id"]))
        return "inb_ok"

    update = {
        "update_id": 42,
        "message": {"message_id": 1, "chat": {"id": 1, "type": "private"}, "text": "hi"},
    }

    poller._fetch_updates = _FetchScript([[update]])  # type: ignore[assignment]
    with patch.object(tlp, "handle_telegram_update", fake_handle):
        await _run_once_then_cancel(poller)

    assert handled == [42]

    # Offset row reflects the consumed update.
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        row = session.query(PollerOffset).filter_by(channel="telegram").first()
        assert row is not None
        assert row.last_update_id == 42
    assert poller.offset == 43


@pytest.mark.asyncio
async def test_offset_not_advanced_on_handle_failure(fresh_db):
    """Handle raises mid-batch → offset stays put, next fetch re-delivers
    the same update; idempotency by update_id covers the duplicate."""
    poller = _make_poller_without_lock()

    async def failing_handle(payload, *, bot_key="sreda"):
        raise RuntimeError("simulated handle crash")

    update = {"update_id": 7, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"}, "text": "x"}}

    poller._fetch_updates = _FetchScript([[update]])  # type: ignore[assignment]
    with patch.object(tlp, "handle_telegram_update", failing_handle):
        await _run_once_then_cancel(poller)

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        row = session.query(PollerOffset).filter_by(channel="telegram").first()
        # Either no row at all (we never advanced) or row with the
        # previous offset — never the failed update_id.
        if row is not None:
            assert row.last_update_id != 7
    assert poller.offset == 0


# ---- Heartbeat ---------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_long_poll_updates_heartbeat_not_offset(fresh_db):
    """200 OK with [] is the normal idle case — heartbeat ticks
    (liveness) but offset stays put."""
    poller = _make_poller_without_lock()

    # Two empty batches, then the parking sleep — gives the loop more
    # than one iteration to record the heartbeat before we cancel.
    poller._fetch_updates = _FetchScript([[], []])  # type: ignore[assignment]
    await _run_once_then_cancel(poller)

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        offset_row = session.query(PollerOffset).filter_by(channel="telegram").first()
        assert offset_row is None  # never advanced
        hb = session.query(PollerHeartbeat).filter_by(channel="telegram").first()
        assert hb is not None
        assert hb.last_attempt_at is not None
        assert hb.last_ok_at is not None
        assert hb.last_error is None


@pytest.mark.asyncio
async def test_timeout_updates_heartbeat_with_error(fresh_db):
    """A real network timeout → last_attempt_at moves, last_ok_at stays
    stale, last_error captures the failure for diagnostics."""
    poller = _make_poller_without_lock()

    raised = {"n": 0}

    async def fetch_then_park():
        if raised["n"] == 0:
            raised["n"] = 1
            raise httpx.TimeoutException("boom")
        await asyncio.sleep(60)
        return []

    poller._fetch_updates = fetch_then_park  # type: ignore[assignment]
    await _run_once_then_cancel(poller)

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        hb = session.query(PollerHeartbeat).filter_by(channel="telegram").first()
        assert hb is not None
        assert hb.last_attempt_at is not None
        assert hb.last_ok_at is None
        assert hb.last_error is not None
        assert "timeout" in hb.last_error.lower()


@pytest.mark.asyncio
async def test_last_error_truncated_to_max_chars(fresh_db):
    """Long exception bodies (HTML 502 page, full traceback) get capped
    so the heartbeats row never blows up unexpectedly."""
    poller = _make_poller_without_lock()
    huge_body = "X" * 5000

    raised = {"n": 0}

    async def fetch_then_park():
        if raised["n"] == 0:
            raised["n"] = 1
            raise RuntimeError(huge_body)
        await asyncio.sleep(60)
        return []

    poller._fetch_updates = fetch_then_park  # type: ignore[assignment]
    await _run_once_then_cancel(poller)

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        hb = session.query(PollerHeartbeat).filter_by(channel="telegram").first()
        assert hb is not None
        assert hb.last_error is not None
        assert len(hb.last_error) <= tlp.LAST_ERROR_MAX_CHARS


# ---- 409 Conflict ------------------------------------------------------


@pytest.mark.asyncio
async def test_409_without_auto_delete_raises_typed_exception(fresh_db):
    """409 → TelegramConflictError propagates out so main() can map to
    exit code 3 and systemd's RestartPreventExitStatus=2 3 stops the
    auto-restart loop."""
    poller = _make_poller_without_lock()

    fake_post = AsyncMock(side_effect=TelegramDeliveryError(
        "Telegram getUpdates non-retryable 409",
        method="getUpdates", status_code=409,
    ))

    with patch.object(poller.client, "_post_request", fake_post):
        with pytest.raises(TelegramConflictError):
            await poller.run_forever()


@pytest.mark.asyncio
async def test_409_with_auto_delete_calls_deleteWebhook_and_continues(fresh_db):
    """When the operator opts into auto-delete (cutover only), 409 is
    healed by calling deleteWebhook and the loop keeps polling."""
    poller = _make_poller_without_lock(auto_delete_webhook=True)

    call_log: list[str] = []

    async def fake_post(method, *, timeout, json=None, data=None, files=None):
        call_log.append(method)
        if method == "getUpdates":
            count = call_log.count("getUpdates")
            if count == 1:
                raise TelegramDeliveryError(
                    "Telegram getUpdates non-retryable 409",
                    method="getUpdates", status_code=409,
                )
            if count == 2:
                # Second call: empty long-poll completes immediately.
                return {"ok": True, "result": []}
            # 3rd+ calls: park so the loop has a real await for cancel.
            await asyncio.sleep(60)
            return {"ok": True, "result": []}
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected method {method}")

    with patch.object(poller.client, "_post_request", fake_post):
        await _run_once_then_cancel(poller, settle=0.2)

    assert "deleteWebhook" in call_log
    assert call_log.count("getUpdates") >= 2


# ---- Singleton lock ----------------------------------------------------


@pytest.mark.asyncio
async def test_singleton_lock_failure_raises_typed_exception(fresh_db):
    """When pg_try_advisory_lock returns False, startup raises
    SingletonLockError so main() returns exit code 2 and systemd does
    not auto-restart into a tight loop."""
    poller = TelegramLongPoller("test-token")

    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = False  # lock not granted
    fake_engine.connect.return_value = fake_conn

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        with pytest.raises(SingletonLockError):
            await poller.startup()


@pytest.mark.asyncio
async def test_check_config_releases_lock(fresh_db):
    """`--check-config` acquires the lock, then releases it so a real
    start can grab it. Without this, pre-cutover sanity-check would
    leave the bot unable to launch its own poller."""
    acquired_count = {"n": 0}
    released_count = {"n": 0}

    fake_engine = MagicMock()
    fake_conn = MagicMock()
    # First execute() = pg_try_advisory_lock → True, second = unlock → True.
    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt)
        if "pg_try_advisory_lock" in sql:
            acquired_count["n"] += 1
            result.scalar.return_value = True
        elif "pg_advisory_unlock" in sql:
            released_count["n"] += 1
            result.scalar.return_value = True
        else:
            result.scalar.return_value = None
        return result
    fake_conn.execute.side_effect = execute_side_effect
    fake_engine.connect.return_value = fake_conn

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        rc = await tlp._amain(["--check-config"])

    assert rc == 0
    assert acquired_count["n"] == 1
    assert released_count["n"] == 1


# ---- main() exit codes -------------------------------------------------


@pytest.mark.asyncio
async def test_main_returns_2_on_singleton_lock_failure(fresh_db):
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = False
    fake_engine.connect.return_value = fake_conn

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        rc = await tlp._amain([])

    assert rc == 2


@pytest.mark.asyncio
async def test_main_returns_3_on_409_conflict(fresh_db):
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = True
    fake_engine.connect.return_value = fake_conn

    async def fake_post(method, *, timeout, json=None, data=None, files=None):
        raise TelegramDeliveryError(
            "Telegram getUpdates non-retryable 409",
            method="getUpdates", status_code=409,
        )

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        with patch("sreda.workers.telegram_long_poll.TelegramClient") as MockTC:
            MockTC.return_value._post_request = fake_post
            rc = await tlp._amain([])

    assert rc == 3


# ---- handle_telegram_update fast path ---------------------------------


@pytest.mark.asyncio
async def test_handle_telegram_update_fast_no_blocking_io(fresh_db, monkeypatch):
    """``handle_telegram_update`` must finish < 200ms in the approved-
    user path: it only does DB upsert + persist + create_task, the
    heavy LLM/voice/outbox work is detached. Anything taking > 200ms
    means we accidentally awaited a network/LLM call inline."""
    from sreda.services.telegram_inbound import handle_telegram_update

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        session.add(Tenant(
            id="tenant_1", name="T1", approved_at=datetime.now(timezone.utc),
        ))
        session.add(Workspace(
            id=f"workspace_tg_{EXISTING_CHAT_ID}", tenant_id="tenant_1", name="W",
        ))
        session.add(User(
            id="user_1", tenant_id="tenant_1",
            telegram_account_id=EXISTING_CHAT_ID,
        ))
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_1", tenant_id="tenant_1", user_id="user_1",
            display_name="Test", address_form="ty",
        ))
        session.commit()

    payload = {
        "update_id": 5001,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hi",
        },
    }

    # Spy on the heavy turn so we can assert it was scheduled but not
    # actually awaited inside handle_telegram_update.
    inline_calls: list[float] = []

    from sreda.services import telegram_inbound as ti

    async def fake_turn(**kwargs):
        inline_calls.append(time.monotonic())
        await asyncio.sleep(0.5)  # would blow the 200ms budget if awaited

    monkeypatch.setattr(ti, "_process_approved_turn", fake_turn)

    start = time.monotonic()
    inb_id = await handle_telegram_update(payload)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert inb_id is not None
    assert elapsed_ms < 200, f"handle_telegram_update took {elapsed_ms:.0f}ms"
    # The turn coroutine was scheduled (or about to be scheduled) but
    # certainly hasn't completed within ~10ms of returning.
    # We can't easily assert on the task object since it was created
    # in-place; the elapsed-time assertion above is the load-bearing one.


@pytest.mark.asyncio
async def test_handle_telegram_update_idempotent_on_duplicate(fresh_db):
    """Same update_id twice → second call is a true no-op:
    persist_telegram_inbound_event returns is_duplicate; we do not
    create a new InboundMessage row, do not change the existing row's
    processing_status, and do not schedule a second turn."""
    from sreda.services.telegram_inbound import handle_telegram_update
    from sreda.db.models.core import InboundMessage

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        session.add(Tenant(
            id="tenant_1", name="T1", approved_at=datetime.now(timezone.utc),
        ))
        session.add(Workspace(
            id=f"workspace_tg_{EXISTING_CHAT_ID}", tenant_id="tenant_1", name="W",
        ))
        session.add(User(
            id="user_1", tenant_id="tenant_1",
            telegram_account_id=EXISTING_CHAT_ID,
        ))
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_1", tenant_id="tenant_1", user_id="user_1",
            display_name="Test", address_form="ty",
        ))
        session.commit()

    payload = {
        "update_id": 9999,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hi",
        },
    }

    # Stub the turn so the test does not depend on the real LLM.
    from sreda.services import telegram_inbound as ti

    async def noop_turn(**kwargs):
        # Pretend the turn ran; pin the inbound row to "processed" so we
        # can verify the duplicate call leaves it untouched.
        with SessionLocal() as s:
            row = s.get(InboundMessage, kwargs["inbound_message_id"])
            if row is not None:
                row.processing_status = "processed"
                s.commit()

    monkeypatch_target = ti
    monkeypatch_target._process_approved_turn = noop_turn

    inb_id_1 = await handle_telegram_update(payload)
    # Drain the create_task'd turn.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=0.5)

    inb_id_2 = await handle_telegram_update(payload)
    assert inb_id_1 == inb_id_2  # same persisted row id, duplicate path

    # Only one InboundMessage row total.
    with SessionLocal() as session:
        rows = session.query(InboundMessage).all()
        assert len(rows) == 1
        # Still 'processed' (set by first turn) — duplicate did not
        # rewind to 'ingested' / 'processing_started'.
        assert rows[0].processing_status == "processed"
