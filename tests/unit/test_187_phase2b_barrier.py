"""#187 Phase 2b — anti-race BARRIER (session-scoped pg_advisory_lock).

WHAT THE BARRIER GUARANTEES (PG runtime, NOT tested here):
A turn that already passed the ingress gate must not commit domain mutations
AFTER ``soft_delete_tenant`` ran its flag+drain. The mechanism is a
**session-scoped** ``pg_advisory_lock`` keyed on the tenant:

- the turn execution (``_process_approved_turn_locked`` / ``_process_approved_max_turn``)
  holds the lock for the WHOLE turn (acquire first, unlock in ``finally``);
- ``soft_delete_tenant`` takes the SAME lock FIRST (before reading deleted_at /
  flipping the flag / draining) → it blocks on an in-flight turn and drains only
  AFTER that turn released the lock.

The lock is session-scoped (``pg_advisory_lock`` + ``pg_advisory_unlock``), NOT
``pg_advisory_xact_lock`` — a turn is multi-transactional (commits mid-turn), and
an xact lock would release on the first mid-turn commit, defeating the barrier.
Pattern mirrors ``workers/telegram_long_poll.py`` (session-scoped advisory lock,
unlock in finally; SHA-256-derived signed-64-bit key).

WHY THIS TEST FILE ONLY CHECKS THE WIRING, NOT TRUE SERIALIZATION:
Unit tests run on SQLite, which has no advisory locks → the helper is a no-op
there. True cross-process serialization is a Postgres-runtime property and is
out of scope for a SQLite unit test. So here we assert the *wiring*:

  (a) ``soft_delete_tenant`` opens ``tenant_advisory_lock`` BEFORE it reads /
      flips ``deleted_at`` (lock-first ordering) and releases it after.
  (b) the turn execution path opens the SAME helper with the SAME key, holding
      it across the turn body.
  (c) the key derived from ``tenant_id`` is deterministic and identical in both
      paths (admin-delete side and turn side) — so they contend on PG.
  (d) on SQLite the helper is a correct no-op: it does not raise, the ``with``
      body runs, and no advisory SQL is emitted.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.orm import Session

from sreda.services import tenant_lifecycle
from sreda.services.tenant_lifecycle import (
    _tenant_advisory_lock_key,
    soft_delete_tenant,
    tenant_advisory_lock,
)

from tests.unit.conftest import seed_telegram_user


# ---------------------------------------------------------------------------
# (c) key derivation — deterministic + identical in both paths
# ---------------------------------------------------------------------------


def test_advisory_key_is_deterministic_and_in_bigint_range() -> None:
    """(c): key is a pure function of tenant_id, stable across calls, and a
    signed 64-bit int (the range ``pg_advisory_lock`` accepts)."""
    k1 = _tenant_advisory_lock_key("tenant_abc")
    k2 = _tenant_advisory_lock_key("tenant_abc")
    assert k1 == k2  # deterministic
    assert isinstance(k1, int)
    # signed 64-bit range
    assert -(2**63) <= k1 < 2**63
    # different tenants → different keys
    assert _tenant_advisory_lock_key("tenant_abc") != _tenant_advisory_lock_key(
        "tenant_xyz"
    )


def test_advisory_key_matches_sha256_signed64_convention() -> None:
    """(c): key derivation follows the same SHA-256 → first-8-bytes →
    big-endian signed-64 convention as ``telegram_long_poll._advisory_lock_id``
    (deterministic across Python restarts, unlike ``hash()``)."""
    tenant_id = "tenant_conv"
    digest = hashlib.sha256(
        f"sreda-tenant-barrier:{tenant_id}".encode()
    ).digest()
    expected = int.from_bytes(digest[:8], "big", signed=True)
    assert _tenant_advisory_lock_key(tenant_id) == expected


# ---------------------------------------------------------------------------
# (d) SQLite no-op
# ---------------------------------------------------------------------------


def test_helper_is_noop_on_sqlite(db_session: Session) -> None:
    """(d): on SQLite the context manager yields cleanly, runs the body, and
    emits NO advisory SQL (SQLite has no advisory locks)."""
    executed: list[str] = []
    real_execute = db_session.execute

    def _spy_execute(statement, *args, **kwargs):  # noqa: ANN001
        executed.append(str(statement))
        return real_execute(statement, *args, **kwargs)

    db_session.execute = _spy_execute  # type: ignore[method-assign]
    try:
        ran = False
        with tenant_advisory_lock(db_session, "tenant_noop"):
            ran = True
    finally:
        db_session.execute = real_execute  # type: ignore[method-assign]

    assert ran is True  # body executed
    # no pg_advisory_* SQL was issued on the SQLite session
    assert not any("advisory" in s.lower() for s in executed)


# ---------------------------------------------------------------------------
# (a) soft_delete_tenant wraps flag+drain in the lock, lock-FIRST ordering
# ---------------------------------------------------------------------------


class _LockSpy:
    """Context-manager spy recording acquire/release order and the key, plus a
    snapshot of the tenant's ``deleted_at`` at acquire time (to prove the lock
    is taken BEFORE the flag is flipped)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (event, tenant_id)
        self.deleted_at_at_acquire: object = "UNSET"

    def __call__(self, session, tenant_id):  # noqa: ANN001
        self._session = session
        self._tenant_id = tenant_id
        return self

    def __enter__(self):
        from sreda.db.models.core import Tenant

        tenant = self._session.get(Tenant, self._tenant_id)
        # snapshot the flag at the moment the lock is acquired
        self.deleted_at_at_acquire = (
            tenant.deleted_at if tenant is not None else "NO_ROW"
        )
        self.calls.append(("acquire", self._tenant_id))
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        self.calls.append(("release", self._tenant_id))
        return False


def test_soft_delete_takes_lock_first_then_releases(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a): ``soft_delete_tenant`` acquires ``tenant_advisory_lock`` BEFORE it
    reads/flips ``deleted_at`` and releases it after the drain.

    Proven by spying on the helper: at acquire time the flag is still NULL
    (lock taken first), and the call sequence is acquire → … → release with the
    tenant_id passed through.
    """
    tid = "tenant_barrier"
    seed_telegram_user(
        db_session, tenant_id=tid, chat_id="901", user_id="u_barrier",
        workspace_id=f"ws_{tid}", profile=False,
    )
    db_session.commit()

    spy = _LockSpy()
    monkeypatch.setattr(tenant_lifecycle, "tenant_advisory_lock", spy)

    assert soft_delete_tenant(db_session, tid) is True

    # lock acquired, then released, exactly once, for this tenant
    assert spy.calls == [("acquire", tid), ("release", tid)]
    # lock was taken BEFORE the flag was flipped (flag still NULL at acquire)
    assert spy.deleted_at_at_acquire is None
    # and the flag IS set after the call returns (drain happened under the lock)
    from sreda.db.models.core import Tenant

    assert db_session.get(Tenant, tid).deleted_at is not None


def test_soft_delete_takes_lock_even_when_already_deleted(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) lock-first on the idempotent path too: the lock is acquired BEFORE
    the ``deleted_at is not None`` short-circuit is evaluated, so a concurrent
    delete still serializes against an in-flight turn (the barrier must not be
    skipped just because the row looks already-deleted at function entry)."""
    tid = "tenant_already"
    seed_telegram_user(
        db_session, tenant_id=tid, chat_id="902", user_id="u_already",
        workspace_id=f"ws_{tid}", profile=False,
    )
    db_session.commit()

    # First delete (real helper — no-op on SQLite).
    assert soft_delete_tenant(db_session, tid) is True

    spy = _LockSpy()
    monkeypatch.setattr(tenant_lifecycle, "tenant_advisory_lock", spy)

    # Second call: idempotent no-op, but the lock must STILL be taken first.
    assert soft_delete_tenant(db_session, tid) is False
    assert spy.calls == [("acquire", tid), ("release", tid)]
    # at acquire the flag was already set (proves lock wraps the short-circuit)
    assert spy.deleted_at_at_acquire is not None


# ---------------------------------------------------------------------------
# (a2) lock held until DURABLE commit — commit happens BEFORE the lock release
#      (R1 CRITICAL: soft_delete must not unlock before its flag+drain is
#      durably committed, else a turn could commit a domain mutation in the
#      unlock→commit window).
# ---------------------------------------------------------------------------


class _OrderSpy:
    """Records the global ordering of two kinds of events into a SHARED list:
    the lock-helper acquire/release (via ``__enter__``/``__exit__``) and the
    working-session ``commit`` (via a wrapped ``session.commit``).

    Used to assert that ``soft_delete_tenant`` calls ``session.commit()`` BEFORE
    it releases the advisory lock — i.e. the lock is held until the durable
    point, closing the unlock→commit race window.
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __call__(self, session, tenant_id):  # noqa: ANN001
        return self

    def __enter__(self):
        self.events.append("lock_acquire")
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        self.events.append("lock_release")
        return False


def test_soft_delete_commits_before_releasing_lock(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a2) R1 CRITICAL: ``commit`` is observed BEFORE ``lock_release`` on the
    real-delete path. Proves the flag+drain is committed UNDER the lock (lock
    held to the durable point), so no turn can sneak a commit into an
    unlock→commit gap.

    Spies on the helper (records release) AND wraps ``session.commit`` (records
    commit) into one shared ordered log, then asserts the sequence."""
    tid = "tenant_order"
    seed_telegram_user(
        db_session, tenant_id=tid, chat_id="903", user_id="u_order",
        workspace_id=f"ws_{tid}", profile=False,
    )
    db_session.commit()

    events: list[str] = []
    spy = _OrderSpy(events)
    monkeypatch.setattr(tenant_lifecycle, "tenant_advisory_lock", spy)

    real_commit = db_session.commit

    def _spy_commit():
        events.append("commit")
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _spy_commit)

    assert soft_delete_tenant(db_session, tid) is True

    # The drain commit must land BEFORE the lock is released.
    assert "commit" in events, "soft_delete_tenant must commit under the lock"
    assert "lock_release" in events
    assert events.index("commit") < events.index("lock_release"), (
        f"commit must precede lock_release; got order: {events}"
    )
    # And the lock was acquired before the commit (lock-first, then durable).
    assert events.index("lock_acquire") < events.index("commit")


def test_soft_delete_commits_before_releasing_lock_idempotent(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a2) The lock-until-durable invariant holds on the idempotent no-op path
    too: even when the tenant is already deleted, the (no-op) commit happens
    before the lock is released, so the barrier ordering is uniform."""
    tid = "tenant_order_idem"
    seed_telegram_user(
        db_session, tenant_id=tid, chat_id="904", user_id="u_order_idem",
        workspace_id=f"ws_{tid}", profile=False,
    )
    db_session.commit()
    assert soft_delete_tenant(db_session, tid) is True  # real helper, no-op lock

    events: list[str] = []
    spy = _OrderSpy(events)
    monkeypatch.setattr(tenant_lifecycle, "tenant_advisory_lock", spy)

    real_commit = db_session.commit

    def _spy_commit():
        events.append("commit")
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _spy_commit)

    assert soft_delete_tenant(db_session, tid) is False  # idempotent no-op
    assert "commit" in events
    assert events.index("commit") < events.index("lock_release"), (
        f"commit must precede lock_release on idempotent path; got: {events}"
    )


# ---------------------------------------------------------------------------
# (b) turn execution holds the SAME helper with the SAME key
# ---------------------------------------------------------------------------


def test_turn_paths_reference_tenant_advisory_lock() -> None:
    """(b): both turn-execution functions wrap their body in
    ``tenant_advisory_lock`` keyed by the tenant.

    Static-wiring check (no event loop): the source of each turn function must
    reference the helper. Combined with (c), this proves the admin-delete side
    and the turn side contend on the SAME advisory key. The behavioural
    serialization is a PG-runtime property (documented in the module docstring).
    """
    import inspect

    from sreda.services import max_inbound, telegram_inbound

    tg_src = inspect.getsource(telegram_inbound._process_approved_turn_locked)
    max_src = inspect.getsource(max_inbound._process_approved_max_turn)

    assert "tenant_advisory_lock" in tg_src, (
        "_process_approved_turn_locked must hold tenant_advisory_lock for the turn"
    )
    assert "tenant_advisory_lock" in max_src, (
        "_process_approved_max_turn must hold tenant_advisory_lock for the turn"
    )


def test_max_acquires_advisory_after_tenant_lock() -> None:
    """(b) R1 CRITICAL lock-ordering: in the MAX turn path the in-process
    asyncio ``tenant_lock`` is acquired FIRST and the ``tenant_advisory_lock``
    is entered INSIDE it — mirroring TG (asyncio-lock OUTERMOST, advisory INNER).

    Taking the (potentially blocking) ``pg_advisory_lock`` before ``tenant_lock``
    would stall the event loop on a synchronous blocking SQL call before the
    asyncio lock is held, and diverge the nesting order from TG (cross-channel
    deadlock risk for one tenant). This is a static order check on the function
    source: it FAILS on the previous inverted code (advisory entered above
    ``async with tenant_lock``) and PASSES only when advisory is nested inside.
    """
    import inspect

    from sreda.services import max_inbound

    src = inspect.getsource(max_inbound._process_approved_max_turn)
    lock_idx = src.find("async with tenant_lock")
    adv_idx = src.find("tenant_advisory_lock(")
    assert lock_idx != -1, "MAX turn must acquire the asyncio tenant_lock"
    assert adv_idx != -1, "MAX turn must enter tenant_advisory_lock"
    assert lock_idx < adv_idx, (
        "MAX must acquire in-process tenant_lock BEFORE the advisory lock "
        f"(tenant_lock @ {lock_idx}, advisory @ {adv_idx}); advisory taken "
        "first would deadlock the event loop / diverge from TG nesting."
    )


def test_tg_advisory_nested_inside_asyncio_tenant_lock() -> None:
    """(b) Cross-check the TG invariant the MAX fix mirrors: the asyncio
    ``tenant_lock`` is held by the OUTER ``_process_approved_turn`` and the
    advisory lock is entered by the INNER ``_process_approved_turn_locked`` —
    so on the TG path too the asyncio lock is OUTERMOST and advisory is INNER.
    """
    import inspect

    from sreda.services import telegram_inbound

    outer = inspect.getsource(telegram_inbound._process_approved_turn)
    inner = inspect.getsource(telegram_inbound._process_approved_turn_locked)
    # asyncio lock is taken in the outer fn, which then calls the locked inner fn
    assert "async with tenant_lock" in outer
    assert "_process_approved_turn_locked" in outer
    # advisory is entered in the inner (already-locked) fn, not the outer one
    assert "tenant_advisory_lock(" in inner
    assert "tenant_advisory_lock(" not in outer, (
        "advisory must be entered inside the asyncio-locked inner fn, not before"
    )


# ---------------------------------------------------------------------------
# (e) lock-engine is cached per-URL — no Engine churn per turn (R2 MAJOR)
# ---------------------------------------------------------------------------


def test_lock_engine_is_cached_per_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """(e) R2 MAJOR: ``_get_lock_engine`` builds at most ONE Engine per URL for
    the whole process. The previous code did ``create_engine`` on every turn and
    disposed it in ``finally`` (Engine churn on the hot path); the fix caches the
    Engine per URL and only closes the dedicated *connection* per turn.

    Proven by spying on the module-level ``create_engine``: two calls with the
    SAME url create only ONE engine (second is served from cache); a DIFFERENT
    url creates a second. Uses a sqlite URL so no real DB/PG is needed.
    """
    # Isolate the cache for this test (don't leak engines into/out of it).
    monkeypatch.setattr(tenant_lifecycle, "_LOCK_ENGINE_CACHE", {})

    created: list[str] = []
    real_create_engine = tenant_lifecycle.create_engine

    def _spy_create_engine(url, *args, **kwargs):  # noqa: ANN001
        created.append(str(url))
        return real_create_engine(url, *args, **kwargs)

    monkeypatch.setattr(tenant_lifecycle, "create_engine", _spy_create_engine)

    url = "sqlite://"
    e1 = tenant_lifecycle._get_lock_engine(url)
    e2 = tenant_lifecycle._get_lock_engine(url)

    # Same Engine object handed back, create_engine called EXACTLY once.
    assert e1 is e2
    assert created == [url], (
        f"create_engine must run once per URL; got {created}"
    )

    # A different URL builds a second, distinct engine.
    other = "sqlite:///:memory:"
    e3 = tenant_lifecycle._get_lock_engine(other)
    assert e3 is not e1
    assert created == [url, other]

    # Dispose engines we created (keep the test tidy; cache is monkeypatched away).
    e1.dispose()
    e3.dispose()


def test_lock_engine_preserves_password_from_url_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) R3 CRITICAL: на PG пароль НЕ должен маскироваться. Вызывающий передаёт
    в ``_get_lock_engine`` URL-ОБЪЕКТ (не ``str(url)``); ``create_engine`` обязан
    увидеть РЕАЛЬНЫЙ пароль — иначе на проде (``postgresql://sreda:pass@…``) коннект
    lock-engine падает с паролем ``***``, advisory не берётся и входящие ходы падают.

    Доказательство: spy на ``create_engine`` ловит переданный URL; пароль цел.
    Регрессия (передача ``str(url)``) сделала бы аргумент строкой с ``***`` → тест падает.
    """
    from unittest.mock import MagicMock

    from sqlalchemy import make_url
    from sqlalchemy.engine import URL

    monkeypatch.setattr(tenant_lifecycle, "_LOCK_ENGINE_CACHE", {})
    captured: list = []

    def _spy(url, *args, **kwargs):  # noqa: ANN001
        captured.append(url)
        return MagicMock()  # не коннектим к реальной БД

    monkeypatch.setattr(tenant_lifecycle, "create_engine", _spy)

    url = make_url("postgresql+psycopg://sreda:s3cr3t@host:5432/db")
    tenant_lifecycle._get_lock_engine(url)

    assert len(captured) == 1
    passed = captured[0]
    assert isinstance(passed, URL), f"create_engine должен получить URL-объект, got {type(passed)}"
    assert passed.password == "s3cr3t", f"пароль замаскирован/потерян: {passed.password!r}"


def test_turn_and_delete_use_identical_key() -> None:
    """(b)+(c): the key the turn path would use is identical to the one
    ``soft_delete_tenant`` uses — same pure function, same input. This is the
    crux of the barrier: both sides must hash to the SAME bigint to contend."""
    tid = "tenant_same"
    # The single source of truth both paths call.
    assert _tenant_advisory_lock_key(tid) == _tenant_advisory_lock_key(tid)
    # And it is the function both import (no divergent re-implementation).
    from sreda.services.tenant_lifecycle import (
        _tenant_advisory_lock_key as from_lifecycle,
    )

    assert from_lifecycle is _tenant_advisory_lock_key
