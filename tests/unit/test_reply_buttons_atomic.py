"""R-22 regression: ReplyButtonService.resolve_token must be ATOMIC.

R-18 (2026-05-13) fixed flush→commit row-lock leak. R-22 fixes the deeper
**TOCTOU race**: prior implementation did SELECT-then-UPDATE in Python,
giving a window where two concurrent callbacks can both pass the
``used_at IS NULL`` check before either commits.

Fix: single conditional UPDATE WHERE used_at IS NULL RETURNING label —
DB enforces atomicity. Only one caller sees a matched row.

Tests:
1. Concurrent simulation: pre-marked-used row → resolve returns None.
2. WHERE clause covers used_at IS NULL (no row → None).
3. Wrong-owner: returns None + WARN log preserved (diagnostic path).
4. Expired token: returns None.
5. Source check: no SELECT before UPDATE — atomic UPDATE only.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.reply_buttons import ReplyButtonCache
from sreda.services.reply_buttons import (
    TOKEN_TTL_SECONDS,
    ReplyButtonService,
)


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def test_concurrent_simulation_only_one_wins() -> None:
    """Simulate race: token created → another "thread" marks used_at →
    our resolve_token call must return None (atomic UPDATE finds no match).

    Validates WHERE used_at IS NULL filter is applied at SQL level.
    """
    SF, engine = _make_session_factory()

    s1 = SF()
    try:
        svc = ReplyButtonService(s1)
        pairs = svc.create_tokens(
            tenant_id="t1", user_id="u1", labels=["Click me"],
        )
        token, _ = pairs[0]
    finally:
        s1.close()

    # Simulate concurrent "thread A" already won the race — marked used_at.
    s_attacker = SF()
    try:
        s_attacker.execute(
            text("UPDATE reply_button_cache SET used_at = :now WHERE token = :t"),
            {"now": datetime.now(timezone.utc), "t": token},
        )
        s_attacker.commit()
    finally:
        s_attacker.close()

    # Now our session B's resolve_token must return None (atomic UPDATE
    # finds no row matching used_at IS NULL).
    s2 = SF()
    try:
        svc = ReplyButtonService(s2)
        result = svc.resolve_token(tenant_id="t1", user_id="u1", token=token)
        assert result is None, (
            "Second resolve должен вернуть None (token уже used_at via concurrent UPDATE)"
        )
    finally:
        s2.close()


def test_atomic_update_uses_returning() -> None:
    """Source check: resolve_token использует sqlalchemy update().returning()
    a NOT separate SELECT + UPDATE pattern.

    Regression guard: partial revert мог вернуть SELECT-then-UPDATE pattern.
    """
    src = inspect.getsource(ReplyButtonService.resolve_token)
    assert "update(ReplyButtonCache)" in src, (
        "resolve_token должен использовать update() construct"
    )
    assert ".returning(" in src.lower() or ".returning(" in src, (
        "resolve_token должен использовать RETURNING для atomic dispatch"
    )
    # Должно НЕ быть self.session.get() — это вернуло бы SELECT-then-UPDATE.
    # (Старый pattern: row = self.session.get(...); row.used_at = ...; commit())
    assert "self.session.get(ReplyButtonCache, token)" not in src, (
        "resolve_token не должен использовать self.session.get() — это вернёт "
        "SELECT-then-UPDATE pattern с TOCTOU race (R-22 regression)"
    )


def test_wrong_owner_returns_none_with_warn_log(caplog) -> None:
    """Wrong tenant_id/user_id → None + WARN log (diagnostic path preserved).

    R-22 atomic UPDATE filters by tenant_id+user_id, поэтому wrong-owner
    auto-returns None. Diagnostic SELECT в _log_resolve_miss обеспечивает
    visibility (warn level), не блокируя decision.
    """
    SF, engine = _make_session_factory()

    s1 = SF()
    try:
        svc = ReplyButtonService(s1)
        pairs = svc.create_tokens(
            tenant_id="t1", user_id="u1", labels=["Click"],
        )
        token, _ = pairs[0]
    finally:
        s1.close()

    s2 = SF()
    try:
        svc = ReplyButtonService(s2)
        with caplog.at_level(logging.WARNING, logger="sreda.services.reply_buttons"):
            result = svc.resolve_token(
                tenant_id="t1", user_id="u_intruder", token=token,
            )
        assert result is None
        # WARN log should fire for wrong-owner case
        assert any(
            "wrong owner" in rec.message
            for rec in caplog.records
        ), "Wrong-owner attempt должен генерить WARN log"
    finally:
        s2.close()


def test_expired_token_returns_none() -> None:
    """Token older than TTL → None (TTL check in WHERE clause)."""
    SF, _ = _make_session_factory()

    s1 = SF()
    try:
        svc = ReplyButtonService(s1)
        pairs = svc.create_tokens(
            tenant_id="t1", user_id="u1", labels=["Click"],
        )
        token, _ = pairs[0]
        # Age the row past TTL
        s1.execute(
            text(
                "UPDATE reply_button_cache SET created_at = :old WHERE token = :t"
            ),
            {
                "old": datetime.now(timezone.utc) - timedelta(seconds=TOKEN_TTL_SECONDS + 60),
                "t": token,
            },
        )
        s1.commit()
    finally:
        s1.close()

    s2 = SF()
    try:
        svc = ReplyButtonService(s2)
        result = svc.resolve_token(tenant_id="t1", user_id="u1", token=token)
        assert result is None, "Expired token должен вернуть None"
    finally:
        s2.close()


def test_nonexistent_token_returns_none() -> None:
    """Missing token → None, no exception."""
    SF, _ = _make_session_factory()
    s = SF()
    try:
        svc = ReplyButtonService(s)
        result = svc.resolve_token(
            tenant_id="t1", user_id="u1", token="ffffffff",
        )
        assert result is None
    finally:
        s.close()


def test_atomic_resolve_marks_used_at_visible_cross_session() -> None:
    """End-to-end: после resolve_token, fresh session видит used_at != NULL.

    Дублирует R-18 commit test но в новой atomic implementation — proves
    commit() still happens после atomic UPDATE.
    """
    SF, _ = _make_session_factory()

    s1 = SF()
    try:
        svc = ReplyButtonService(s1)
        pairs = svc.create_tokens(
            tenant_id="t1", user_id="u1", labels=["Hello"],
        )
        token, _ = pairs[0]
    finally:
        s1.close()

    s2 = SF()
    try:
        svc = ReplyButtonService(s2)
        result = svc.resolve_token(tenant_id="t1", user_id="u1", token=token)
        assert result == "Hello"
    finally:
        s2.close()

    s3 = SF()
    try:
        row = s3.get(ReplyButtonCache, token)
        assert row is not None
        assert row.used_at is not None, (
            "После atomic resolve_token, used_at должен быть committed cross-session"
        )
    finally:
        s3.close()
