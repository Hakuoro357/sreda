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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from sreda.config.bot_registry import BotConfig, TelegramBotRegistry
from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.poller_state import PollerHeartbeat, PollerOffset
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramDeliveryError
from sreda.workers import telegram_long_poll as tlp
from sreda.workers.telegram_long_poll import (
    SingletonLockError,
    TelegramConflictError,
    TelegramLongPoller,
    _run_check_config,
)
from tests.unit.conftest import seed_telegram_user

EXISTING_CHAT_ID = "100000003"


def _seed_approved_housewife_user(session) -> None:
    """Seed the same state an approved prod tenant has after Phase 2."""

    seeded = seed_telegram_user(
        session, chat_id=EXISTING_CHAT_ID, profile_id="tup_1",
    )
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"sreda_free_{uuid4().hex[:8]}",
        feature_key="housewife_assistant",
        title="Sreda Free",
        description="",
        price_rub=0,
        billing_period_days=30,
        credits_monthly_quota=1_000_000,
    )
    session.add(plan)
    session.flush()
    session.add(
        TenantSubscription(
            id=f"sub_{uuid4().hex[:16]}",
            tenant_id=seeded.tenant_id,
            plan_id=plan.id,
            feature_key="housewife_assistant",
            status="active",
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            active_until=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    session.commit()

    from sreda.services.onboarding import mark_welcome_sent

    mark_welcome_sent(session, seeded.tenant_id, seeded.user_id)


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
    # Phase 3: channel key is now "telegram:sreda" (per-bot namespace).
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        row = session.query(PollerOffset).filter_by(channel="telegram:sreda").first()
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
        row = session.query(PollerOffset).filter_by(channel="telegram:sreda").first()
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
        # Phase 3: channel key is now "telegram:sreda" (per-bot namespace).
        offset_row = session.query(PollerOffset).filter_by(channel="telegram:sreda").first()
        assert offset_row is None  # never advanced
        hb = session.query(PollerHeartbeat).filter_by(channel="telegram:sreda").first()
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
        hb = session.query(PollerHeartbeat).filter_by(channel="telegram:sreda").first()
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
        hb = session.query(PollerHeartbeat).filter_by(channel="telegram:sreda").first()
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
async def test_check_config_releases_lock(fresh_db, monkeypatch):
    """`--check-config` acquires the lock, then releases it so a real
    start can grab it. Without this, pre-cutover sanity-check would
    leave the bot unable to launch its own poller.

    Phase 3: --check-config now also calls getMe to verify the token↔bot
    mapping.  We stub the registry to supply a username and stub getMe to
    return the matching username so the verification passes before the lock
    acquire/release dance is exercised.
    """
    from unittest.mock import patch as _patch
    from sreda.config.bot_registry import BotConfig, TelegramBotRegistry

    # Registry with a username so the no-username guard doesn't trip.
    registry = TelegramBotRegistry(
        [BotConfig(key="sreda", token="test-token", username="TestBot")],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )

    # getMe returns the correct username → verification passes.
    async def fake_get_me() -> dict:
        return {"ok": True, "result": {"username": "TestBot", "id": 1}}

    fake_client = MagicMock()
    fake_client.get_me = fake_get_me

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

    # Fix 2: _run_check_config now builds TelegramClient(token) per-token
    # inside _real_get_me. Patch TelegramClient in the worker module so the
    # fake client is used instead of making a real HTTP call.
    class _FakeTelegramClient:
        def __init__(self, token: str) -> None:
            pass
        async def get_me(self) -> dict:  # noqa: E301
            return {"ok": True, "result": {"username": "TestBot", "id": 1}}

    with _patch("sreda.config.bot_registry.TelegramBotRegistry") as mock_reg_cls:
        mock_reg_cls.from_settings.return_value = registry
        with patch.object(tlp, "TelegramClient", _FakeTelegramClient):
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
    """``handle_telegram_update`` must detach approved-user processing.

    The handler should persist inbound state and schedule the heavy
    LLM/voice/outbox work with ``asyncio.create_task``. We assert the
    coroutine is scheduled, not awaited inline; wall-clock thresholds are
    too flaky on cold Windows SQLite test setup.
    """
    from sreda.services.telegram_inbound import handle_telegram_update

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_approved_housewife_user(session)

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

    scheduled: list[tuple[object, str | None]] = []

    def fake_create_task(coro, *, name=None):
        scheduled.append((coro, name))
        coro.close()
        return MagicMock()

    monkeypatch.setattr(ti, "_process_approved_turn", fake_turn)
    monkeypatch.setattr(ti.asyncio, "create_task", fake_create_task)

    inb_id = await handle_telegram_update(payload)

    assert inb_id is not None
    assert scheduled
    assert inline_calls == []


@pytest.mark.asyncio
async def test_handle_telegram_update_idempotent_on_duplicate(fresh_db, monkeypatch):
    """Same update_id twice → second call is a true no-op:
    persist_telegram_inbound_event returns is_duplicate; we do not
    create a new InboundMessage row, do not change the existing row's
    processing_status, and do not schedule a second turn."""
    from sreda.services.telegram_inbound import handle_telegram_update
    from sreda.db.models.core import InboundMessage

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_approved_housewife_user(session)

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

    # CRITICAL: must use pytest's monkeypatch fixture so the stub gets
    # rolled back after this test. Direct ``ti._process_approved_turn = ...``
    # would persist for the rest of the session and silently break ALL
    # subsequent tests that exercise real turn processing (notably
    # test_telegram_webhook). This was the root of пункт 8.1 isolation
    # bug — fixed 2026-05-02.
    monkeypatch.setattr(ti, "_process_approved_turn", noop_turn)

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


# ---- Graceful shutdown signal handler ---------------------------------


@pytest.mark.asyncio
async def test_install_signal_handlers_returns_bool_without_crash():
    """``_install_signal_handlers`` must never crash regardless of
    platform. On Linux/macOS it returns True; on Windows or under
    threading constraints it returns False. Both paths are valid."""

    async def _runnable():
        await asyncio.sleep(60)

    task = asyncio.create_task(_runnable())
    try:
        result = tlp._install_signal_handlers(task)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_amain_returns_0_on_cancellation_during_run_forever(fresh_db):
    """When main task gets cancelled (simulating SIGTERM behaviour
    via ``main_task.cancel()``), ``_amain`` must:
    1. Catch CancelledError gracefully (not propagate)
    2. Return 0 (clean shutdown)
    3. Run finally block (poller.shutdown released advisory lock)
    """
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = True  # lock granted
    fake_engine.connect.return_value = fake_conn

    # Make _fetch_updates park forever so we can cancel cleanly.
    parking_event = asyncio.Event()

    async def park_fetch(self):
        await parking_event.wait()
        return []

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        with patch.object(
            tlp.TelegramLongPoller, "_fetch_updates", park_fetch
        ):
            main_task = asyncio.create_task(tlp._amain([]))
            # Let the task get past startup() and into run_forever().
            await asyncio.sleep(0.1)
            # Simulate SIGTERM: cancel the main task externally.
            main_task.cancel()
            rc = await main_task

    assert rc == 0
    # Verify shutdown was called: lock + unlock means execute() was
    # invoked at least twice on the dedicated lock-conn (once in startup
    # for pg_try_advisory_lock, once in shutdown for pg_advisory_unlock).
    # SQLAlchemy ``text(...)`` produces TextClause objects whose repr
    # doesn't contain the SQL string, so we count calls instead of
    # pattern-matching call args.
    assert fake_conn.execute.call_count >= 2, (
        f"poller.shutdown() must call pg_advisory_unlock after graceful "
        f"cancellation. Expected at least 2 execute() calls (lock+unlock), "
        f"got {fake_conn.execute.call_count}."
    )
    # Also verify lock conn was closed (shutdown's finally)
    assert fake_conn.close.called, (
        "poller.shutdown() must close the lock connection in finally."
    )


# ---- R-23: commit after lock/unlock (PG idle-tx-timeout enablement) ----


@pytest.mark.asyncio
async def test_lock_connection_commits_after_acquire(fresh_db):
    """R-23: after pg_try_advisory_lock succeeds, startup() must call
    commit() on the dedicated lock connection.

    Without commit(), SQLAlchemy 2.x autobegin leaves the connection
    in `idle in transaction` state forever. PostgreSQL's
    `idle_in_transaction_session_timeout` setting (defense-in-depth from
    R-18) would then kick our singleton poller after the timeout window,
    breaking the singleton guarantee and allowing duplicate pollers to
    start.

    The lock itself is session-level (`pg_try_advisory_lock`), so it
    persists across commit — committing the transaction does NOT release
    it. Connection returns to `idle` state, PG timeout no longer applies.
    """
    poller = TelegramLongPoller("test-token")
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = True  # lock acquired

    fake_engine.connect.return_value = fake_conn

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        await poller.startup()

    # commit() must have been called at least once after lock acquired
    # to close the implicit autobegin transaction.
    assert fake_conn.commit.called, (
        "startup() must call self._lock_conn.commit() after "
        "pg_try_advisory_lock to close the implicit autobegin tx. "
        "Otherwise PG sees the connection as 'idle in transaction' "
        "and idle_in_transaction_session_timeout would kill it."
    )
    # Specifically: commit called BETWEEN lock execute and the return.
    # Verify the call order: execute (lock) → commit.
    call_order = [c[0] for c in fake_conn.method_calls if c[0] in ("execute", "commit")]
    assert "commit" in call_order, "commit() never called"
    assert call_order.index("execute") < call_order.index("commit"), (
        "commit() must come AFTER execute(pg_try_advisory_lock), "
        "not before."
    )

    # Cleanup
    await poller.shutdown()


@pytest.mark.asyncio
async def test_lock_connection_commits_after_unlock(fresh_db):
    """R-23: after pg_advisory_unlock, shutdown() must also commit.

    Symmetry with startup: the unlock query also runs under SQLAlchemy
    2.x autobegin, leaving the connection in 'idle in transaction' state
    if we don't commit. An explicit commit ends the implicit transaction
    before close, keeping the lifecycle clean. (Advisory unlock is
    session-level — the lock is released by the unlock query itself,
    not by the commit.)
    """
    poller = TelegramLongPoller("test-token")
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    # Lock acquire returns True, unlock returns True
    fake_conn.execute.return_value.scalar.return_value = True
    fake_engine.connect.return_value = fake_conn

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        await poller.startup()
        commit_calls_after_startup = fake_conn.commit.call_count
        await poller.shutdown()

    # shutdown() must call commit() at least once MORE (after unlock).
    assert fake_conn.commit.call_count > commit_calls_after_startup, (
        f"shutdown() must call self._lock_conn.commit() after "
        f"pg_advisory_unlock. Got {fake_conn.commit.call_count} total "
        f"commits, expected > {commit_calls_after_startup} "
        f"(commits after startup)."
    )
    # Codex R-23 review MINOR 1: assert ORDER — commit must come AFTER
    # the unlock execute, not before. Otherwise a future regression could
    # commit-then-unlock and still pass the count check.
    method_calls = [c[0] for c in fake_conn.method_calls]
    # Find indices of last execute (= unlock, since lock was first) and
    # last commit (= post-unlock commit, since post-acquire commit was first).
    execute_indices = [i for i, m in enumerate(method_calls) if m == "execute"]
    commit_indices = [i for i, m in enumerate(method_calls) if m == "commit"]
    assert execute_indices and commit_indices, "missing execute/commit"
    last_execute = execute_indices[-1]
    last_commit = commit_indices[-1]
    assert last_commit > last_execute, (
        f"shutdown() commit() must come AFTER pg_advisory_unlock execute() "
        f"(last_execute idx={last_execute}, last_commit idx={last_commit}). "
        f"Method order: {method_calls}"
    )


@pytest.mark.asyncio
async def test_lock_connection_no_commit_when_lock_denied(fresh_db):
    """R-23 negative: when pg_try_advisory_lock returns False, we
    raise SingletonLockError WITHOUT committing the failed transaction.

    Rationale: connection is about to be closed unconditionally (lines
    146-149); commit before close is wasted work and might mask a
    legitimate error. The connection close itself rolls back the implicit
    tx.
    """
    poller = TelegramLongPoller("test-token")
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = False  # lock NOT granted
    fake_engine.connect.return_value = fake_conn

    with patch.object(tlp, "create_engine", return_value=fake_engine):
        with pytest.raises(SingletonLockError):
            await poller.startup()

    # No commit should have been called — the lock was never acquired so
    # there's nothing to keep around. close() handles the implicit tx.
    assert not fake_conn.commit.called, (
        "When lock acquisition fails, startup() should NOT commit — "
        "connection is closed unconditionally and an unsuccessful "
        "acquire has nothing to persist."
    )


# ---------------------------------------------------------------------------
# Fix 2 tests: _run_check_config must build per-token getMe client and
# distinguish network errors from API/auth errors.
# ---------------------------------------------------------------------------


def _make_two_bot_registry() -> TelegramBotRegistry:
    """Registry with two distinct bots, each with a username for swap-check."""
    return TelegramBotRegistry(
        bots=[
            BotConfig(key="sreda", token="token-sreda", username="SredaBot"),
            BotConfig(key="sreda_home", token="token-sreda-home", username="SredaHomeBot"),
        ],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )


def _make_fake_telegram_client_cls(token_to_username: dict[str, str]):
    """Return a TelegramClient replacement whose get_me returns per-token username."""

    class _FakeClient:
        def __init__(self, token: str) -> None:
            self._token = token

        async def get_me(self) -> dict:
            username = token_to_username.get(self._token)
            if username is None:
                from sreda.integrations.telegram.client import TelegramDeliveryError
                raise TelegramDeliveryError(
                    f"getMe: bad token (no username for token ...{self._token[-4:]})"
                )
            return {"ok": True, "result": {"username": username, "id": 1}}

    return _FakeClient


@pytest.mark.asyncio
async def test_check_config_two_bots_correct_tokens_passes(monkeypatch):
    """Fix 2: with two bots and correct per-token usernames, check-config passes."""
    registry = _make_two_bot_registry()

    fake_cls = _make_fake_telegram_client_cls({
        "token-sreda": "SredaBot",
        "token-sreda-home": "SredaHomeBot",
    })
    monkeypatch.setattr(tlp, "TelegramClient", fake_cls)

    # Bypass the lock/offset dance.
    monkeypatch.setattr(TelegramLongPoller, "startup", AsyncMock())
    monkeypatch.setattr(TelegramLongPoller, "shutdown", AsyncMock())
    monkeypatch.setattr(TelegramLongPoller, "_channel", "telegram:sreda", raising=False)
    monkeypatch.setattr(TelegramLongPoller, "offset", 0, raising=False)

    rc = await _run_check_config("sreda", registry, None)
    assert rc == 0, f"Expected rc=0 (pass), got {rc}"


@pytest.mark.asyncio
async def test_check_config_swapped_token_hard_fails(monkeypatch):
    """Fix 2: when a bot's token returns the wrong username, check-config hard-fails (rc=1)."""
    registry = _make_two_bot_registry()

    # Swap: token-sreda returns SredaHomeBot's username and vice versa.
    fake_cls = _make_fake_telegram_client_cls({
        "token-sreda": "SredaHomeBot",      # wrong username for sreda
        "token-sreda-home": "SredaBot",     # wrong username for sreda_home
    })
    monkeypatch.setattr(tlp, "TelegramClient", fake_cls)

    monkeypatch.setattr(TelegramLongPoller, "startup", AsyncMock())
    monkeypatch.setattr(TelegramLongPoller, "shutdown", AsyncMock())

    rc = await _run_check_config("sreda", registry, None)
    assert rc == 1, f"Expected rc=1 (hard fail on username mismatch), got {rc}"


@pytest.mark.asyncio
async def test_check_config_bad_token_hard_fails(monkeypatch):
    """Fix 2: a TelegramDeliveryError (bad token / ok=false) causes rc=1, not skip."""
    registry = _make_two_bot_registry()

    # No token resolves → every getMe raises TelegramDeliveryError.
    fake_cls = _make_fake_telegram_client_cls({})
    monkeypatch.setattr(tlp, "TelegramClient", fake_cls)

    monkeypatch.setattr(TelegramLongPoller, "startup", AsyncMock())
    monkeypatch.setattr(TelegramLongPoller, "shutdown", AsyncMock())

    rc = await _run_check_config("sreda", registry, None)
    assert rc == 1, f"Expected rc=1 (hard fail on bad token), got {rc}"


@pytest.mark.asyncio
async def test_check_config_network_error_is_skipped(monkeypatch):
    """Fix 2: a genuine network error (httpx.NetworkError) skips verification, rc=0."""
    registry = _make_two_bot_registry()

    class _NetworkErrorClient:
        def __init__(self, token: str) -> None:
            pass

        async def get_me(self) -> dict:
            raise httpx.NetworkError("connection refused")

    monkeypatch.setattr(tlp, "TelegramClient", _NetworkErrorClient)

    monkeypatch.setattr(TelegramLongPoller, "startup", AsyncMock())
    monkeypatch.setattr(TelegramLongPoller, "shutdown", AsyncMock())
    monkeypatch.setattr(TelegramLongPoller, "_channel", "telegram:sreda", raising=False)
    monkeypatch.setattr(TelegramLongPoller, "offset", 0, raising=False)

    rc = await _run_check_config("sreda", registry, None)
    assert rc == 0, f"Expected rc=0 (network skip), got {rc}"
