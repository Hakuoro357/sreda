"""Phase 5-lite: decide_to_speak policy + throttle + dedup."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.runtime.proactive_policy import (
    ProactiveDecisionKind,
    decide_proactive,
)
from sreda.services.embeddings import FakeEmbeddingClient


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="W"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _seed_outbox(
    session,
    *,
    text: str,
    feature_key: str = "skill_a",
    user_id: str = "u1",
    status: str = "sent",
    is_interactive: bool = False,
    created_at: datetime | None = None,
) -> OutboxMessage:
    row = OutboxMessage(
        id=f"out_{uuid4().hex[:16]}",
        tenant_id="t1",
        workspace_id="w1",
        user_id=user_id,
        channel_type="telegram",
        feature_key=feature_key,
        is_interactive=is_interactive,
        status=status,
        payload_json=json.dumps({"chat_id": "42", "text": text, "reply_markup": None}),
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row


def _utc(h_offset_from_now: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=h_offset_from_now)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def test_send_when_no_prior_outbox(session):
    decision = decide_proactive(
        session=session,
        reply_text="Новое событие по заявке",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 0},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send


def test_drops_exact_substring_duplicate(session):
    _seed_outbox(session, text="Заявка #42 обновлена")
    decision = decide_proactive(
        session=session,
        reply_text="Заявка #42 обновлена",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 0},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.drop
    assert decision.drop_reason == "duplicate"


def test_drop_scoped_to_user_and_feature(session):
    _seed_outbox(session, text="Дубль", user_id="u1", feature_key="skill_a")
    # Same text, different feature → not a dup
    decision = decide_proactive(
        session=session,
        reply_text="Дубль",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_b",
        profile={"proactive_throttle_minutes": 0},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send


def test_ignores_interactive_rows_for_duplicate_check(session):
    """Interactive rows (user command replies) shouldn't count as
    proactive dupes — user asked for it, we answered, that's fine."""
    _seed_outbox(
        session, text="Привет", is_interactive=True, feature_key="skill_a"
    )
    decision = decide_proactive(
        session=session,
        reply_text="Привет",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 0},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send


def test_embedding_based_duplicate_catches_paraphrase(session):
    """With the fake embedding client two IDENTICAL strings hash to
    the same vector → cosine 1.0 → caught as duplicate even before
    the substring path. Verifies the embedding codepath is wired."""
    _seed_outbox(session, text="Новое событие по заявке")
    client = FakeEmbeddingClient()
    # Monkey-round: even with same text the substring check catches
    # it; we're primarily checking no crash when embedding client is
    # present.
    decision = decide_proactive(
        session=session,
        reply_text="Новое событие по заявке",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 0},
        embedding_client=client,
    )
    assert decision.kind == ProactiveDecisionKind.drop


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


def test_throttle_defers_when_recent_sent_exists(session):
    # A proactive sent message 5 minutes ago + 30-minute throttle.
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_outbox(session, text="что-то ещё", created_at=five_min_ago)
    decision = decide_proactive(
        session=session,
        reply_text="новый уникальный текст",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 30},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.defer
    # Defer target = prior_created_at + 30min; should be ~25min from now
    assert decision.defer_until_utc is not None
    delta = decision.defer_until_utc - datetime.now(timezone.utc)
    assert timedelta(minutes=20) < delta < timedelta(minutes=30)


def test_throttle_zero_disables_throttle(session):
    # Even with a recent sent message, throttle=0 → send
    two_min_ago = datetime.now(timezone.utc) - timedelta(minutes=2)
    _seed_outbox(session, text="first", created_at=two_min_ago)
    decision = decide_proactive(
        session=session,
        reply_text="second unique",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 0},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send


def test_throttle_ignores_dropped_rows(session):
    """If the only recent row was itself dropped (muted/policy), it
    didn't reach the user — throttle window should be clear."""
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_outbox(
        session,
        text="dropped text",
        status="dropped",
        created_at=five_min_ago,
    )
    decision = decide_proactive(
        session=session,
        reply_text="new text",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 30},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send


def test_throttle_window_expires(session):
    """Sent message 40 min ago + 30-min throttle → window clear, send."""
    forty_min_ago = datetime.now(timezone.utc) - timedelta(minutes=40)
    _seed_outbox(session, text="old", created_at=forty_min_ago)
    decision = decide_proactive(
        session=session,
        reply_text="new",
        tenant_id="t1",
        user_id="u1",
        feature_key="skill_a",
        profile={"proactive_throttle_minutes": 30},
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send


# ---------------------------------------------------------------------------
# No-user edge case
# ---------------------------------------------------------------------------


def test_no_user_skips_policy(session):
    """System-wide proactive messages (no user_id) bypass policy —
    operator-authored broadcasts etc."""
    decision = decide_proactive(
        session=session,
        reply_text="broadcast",
        tenant_id="t1",
        user_id=None,
        feature_key="skill_a",
        profile=None,
        embedding_client=None,
    )
    assert decision.kind == ProactiveDecisionKind.send
    assert decision.reason == "no_user"
