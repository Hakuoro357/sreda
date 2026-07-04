"""#305 — admin login service (``services.admin_login``) locking tests.

Named tests for the acceptance-checklist invariants that live in the guarded
UPDATE state-machine (independent of routes / bot wiring):

- send-claim is at-most-once: a duplicate ``/start`` (second ``attach_bot``)
  returns None → the button is sent exactly once (checklist п.8).
- ``confirm`` only advances for the tg_id that started (wrong tg denied)
  (checklist п.3).
- ``claim`` requires the matching ``browser_bind`` and is single-use; a wrong
  bind does NOT burn the confirmed challenge (checklist п.10).
- ``get_status`` is READ-only (no mutation) (checklist п.6, poller safety).
- session mint / resolve / revoke round-trip (checklist п.2/п.4).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
import sreda.db.models.admin_auth  # noqa: F401 — register tables on Base.metadata
from sreda.services import admin_login as al


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _start(session):
    return al.start_challenge(session, "1.2.3.4")


def test_attach_bot_send_claim_at_most_once(session):
    """Дубль /start → второй attach_bot возвращает None (кнопку шлём один раз)."""
    r = _start(session)
    first = al.attach_bot(session, r.challenge_id, "999", "sreda", "chat1")
    assert first is not None
    assert first.human_code == r.human_code
    # Second /start for the SAME challenge — send-claim already won → None.
    second = al.attach_bot(session, r.challenge_id, "999", "sreda", "chat1")
    assert second is None


def test_confirm_wrong_tg_denied(session):
    """confirm тем же challenge, но ДРУГИМ tg_id → не подтверждает."""
    r = _start(session)
    al.attach_bot(session, r.challenge_id, "999", "sreda", "chat1")
    # A different tg_id tries to confirm — must not advance.
    outcome = al.confirm(session, r.challenge_id, "111")
    assert outcome == "expired_or_denied"
    # The correct tg_id confirms fine afterwards.
    assert al.confirm(session, r.challenge_id, "999") == "confirmed"
    # Idempotent repeat → 'already'.
    assert al.confirm(session, r.challenge_id, "999") == "already"


def test_claim_wrong_bind_not_burned_then_correct_single_use(session):
    """claim с чужим browser_bind → None и НЕ жжёт; правильный bind → tg_id,
    повторный claim → None (single-use)."""
    r = _start(session)
    al.attach_bot(session, r.challenge_id, "999", "sreda", "chat1")
    al.confirm(session, r.challenge_id, "999")

    # Wrong bind: no consume, challenge stays confirmed.
    assert al.claim(session, r.challenge_id, "WRONG-BIND") is None
    assert al.get_status(session, r.challenge_id, r.browser_bind_raw) == "confirmed"

    # Correct bind: returns tg_id and consumes.
    assert al.claim(session, r.challenge_id, r.browser_bind_raw) == "999"
    assert al.get_status(session, r.challenge_id, r.browser_bind_raw) == "consumed"
    # Single-use: a second claim yields None.
    assert al.claim(session, r.challenge_id, r.browser_bind_raw) is None


def test_get_status_read_only(session):
    """get_status не мутирует статус (многократный опрос — no-op)."""
    r = _start(session)
    assert al.get_status(session, r.challenge_id, r.browser_bind_raw) == "pending"
    for _ in range(3):
        assert al.get_status(session, r.challenge_id, r.browser_bind_raw) == "pending"
    # Wrong bind → 'unknown', still no mutation.
    assert al.get_status(session, r.challenge_id, "nope") == "unknown"
    assert al.get_status(session, r.challenge_id, r.browser_bind_raw) == "pending"


def test_session_mint_resolve_revoke(session):
    """mint → resolve возвращает живую сессию; revoke → resolve=None."""
    raw = al.mint_session(session, "999")
    row = al.resolve_session(session, raw)
    assert row is not None and row.tg_id == "999"
    assert al.revoke_session(session, raw) is True
    assert al.resolve_session(session, raw) is None
    # Unknown raw → None (never raises).
    assert al.resolve_session(session, "does-not-exist") is None


# --------------------------------------------------- Item F: GC wiring (#13)

@pytest.mark.asyncio
async def test_maintenance_job_invokes_admin_login_cleanups(monkeypatch):
    """#305 checklist #13: периодический maintenance-джоб (max_subscription_health)
    ВЫЗЫВАЕТ обе admin-login GC-функции (challenge'и + сессии) в реальной сессии."""
    from contextlib import contextmanager

    from sreda.workers import max_subscription_health as msh

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine)

    class _SF:
        def __call__(self):
            @contextmanager
            def _cm():
                s = Local()
                try:
                    yield s
                finally:
                    s.close()
            return _cm()

    calls: list[str] = []
    monkeypatch.setattr(
        "sreda.services.admin_login.cleanup_expired_challenges",
        lambda s, **k: (calls.append("challenges"), 0)[1],
    )
    monkeypatch.setattr(
        "sreda.services.admin_login.cleanup_expired_admin_sessions",
        lambda s, **k: (calls.append("sessions"), 0)[1],
    )
    monkeypatch.setattr(msh, "get_session_factory", lambda: _SF())
    # Neutralise the MAX-subscription + channel-token halves (unrelated).
    monkeypatch.setattr(msh, "_verify_max_subscription", lambda st: None)
    monkeypatch.setattr(msh, "_cleanup_expired_tokens", lambda: None)

    await msh.check_max_subscription_health()
    assert "challenges" in calls and "sessions" in calls
