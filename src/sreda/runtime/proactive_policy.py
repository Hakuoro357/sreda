"""Proactive policy — the ``decide_to_speak`` filter (Phase 5-lite).

Sits between a skill's proactive handler and the outbox. Takes the
reply the handler wants to send plus recent outbox history + user
profile, returns one of:

  * ``Send``   — write to outbox as normal (delivery worker handles
                 quiet-hours/mute afterwards)
  * ``Defer``  — write to outbox with ``scheduled_at`` in the future,
                 status='pending'; delivery worker picks it up then
  * ``Drop``   — don't deliver; write an outbox row with status='dropped'
                 + ``drop_reason`` for observability in ``/stats``

Rules (in order; first match wins):

  1. **Duplicate detection.** If outbox already has a proactive row
     for (user, feature_key) in the last 24h whose text is ≥ 0.85
     cosine-similar to the candidate → Drop(duplicate). Embeddings
     come from the injected client; when unavailable, fall back to
     substring equality (weaker but still catches near-identical).

  2. **Throttle.** If outbox has ≥ 1 proactive row for (user,
     feature_key) with ``created_at >= now - throttle_minutes`` →
     Defer(until the oldest such row + throttle window). Zero-minute
     throttle = disabled.

  3. **LLM filter** (hook) — placeholder for Phase 5-full. Called
     only if ``settings.mimo_classifier_model`` is configured. Stub
     returns Send for now.

  4. Default → Send.

Interactive replies (replies to user commands) never reach here —
they're written directly by ``node_persist_replies`` with
``is_interactive=True`` and bypass this path entirely.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage
from sreda.services.embeddings import cosine_similarity

logger = logging.getLogger(__name__)


DUPLICATE_LOOKBACK_HOURS = 24
DUPLICATE_COSINE_THRESHOLD = 0.85


class ProactiveDecisionKind(str, Enum):
    send = "send"
    defer = "defer"
    drop = "drop"


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    kind: ProactiveDecisionKind
    defer_until_utc: datetime | None = None
    drop_reason: str | None = None
    reason: str = ""


class _EmbeddingClientProto(Protocol):
    def embed_document(self, text: str) -> list[float]: ...
    def embed_query(self, text: str) -> list[float]: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def decide_proactive(
    *,
    session: Session,
    reply_text: str,
    tenant_id: str,
    user_id: str | None,
    feature_key: str,
    profile: dict[str, Any] | None,
    embedding_client: _EmbeddingClientProto | None,
    now_utc: datetime | None = None,
) -> ProactiveDecision:
    """Main entry point. Pure function of its inputs + DB reads."""
    now_utc = now_utc or _utcnow()

    if not user_id:
        # System-wide proactive messages (no specific user) skip policy
        # — there's no profile to consult. Operator-authored channels.
        return ProactiveDecision(kind=ProactiveDecisionKind.send, reason="no_user")

    recent = _recent_proactive_outbox(
        session,
        user_id=user_id,
        feature_key=feature_key,
        since=now_utc - timedelta(hours=DUPLICATE_LOOKBACK_HOURS),
    )

    # Rule 1: duplicate detection
    if _is_duplicate(reply_text, recent, embedding_client):
        return ProactiveDecision(
            kind=ProactiveDecisionKind.drop,
            drop_reason="duplicate",
            reason="similar message sent in last 24h",
        )

    # Rule 2: throttle
    throttle_minutes = int((profile or {}).get("proactive_throttle_minutes", 30) or 0)
    if throttle_minutes > 0:
        defer_until = _throttle_defer_until(
            recent, now_utc=now_utc, throttle_minutes=throttle_minutes
        )
        if defer_until is not None:
            return ProactiveDecision(
                kind=ProactiveDecisionKind.defer,
                defer_until_utc=defer_until,
                reason="throttle",
            )

    # Rule 3: LLM filter (stub — Phase 5-full adds the actual call)
    # ...

    # Rule 4: default
    return ProactiveDecision(kind=ProactiveDecisionKind.send, reason="default")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _recent_proactive_outbox(
    session: Session,
    *,
    user_id: str,
    feature_key: str,
    since: datetime,
) -> list[OutboxMessage]:
    """Proactive rows (is_interactive=False) for this user+skill since
    ``since``, regardless of their delivery status — we need to count
    both what was sent AND what was dropped to reason about churn."""
    return (
        session.query(OutboxMessage)
        .filter(
            OutboxMessage.user_id == user_id,
            OutboxMessage.feature_key == feature_key,
            OutboxMessage.is_interactive.is_(False),
            OutboxMessage.created_at >= since,
        )
        .order_by(OutboxMessage.created_at.desc())
        .all()
    )


def _is_duplicate(
    new_text: str,
    recent: list[OutboxMessage],
    embedding_client: _EmbeddingClientProto | None,
) -> bool:
    """Two-tier check:
    * exact substring equality (cheap, catches "same handler, same
      text");
    * embedding cosine (semantic, catches paraphrase)."""
    new_text_norm = new_text.strip()
    if not new_text_norm:
        return False
    recent_texts = [_decode_outbox_text(row) for row in recent]
    recent_texts = [t for t in recent_texts if t]

    if any(t == new_text_norm for t in recent_texts):
        return True

    if embedding_client is None:
        return False

    try:
        new_vec = embedding_client.embed_document(new_text_norm)
    except Exception:  # noqa: BLE001
        logger.warning("proactive policy: embedding failed, skipping dedup")
        return False

    for text in recent_texts:
        try:
            vec = embedding_client.embed_document(text)
        except Exception:  # noqa: BLE001
            continue
        if cosine_similarity(new_vec, vec) >= DUPLICATE_COSINE_THRESHOLD:
            return True
    return False


def _throttle_defer_until(
    recent: list[OutboxMessage],
    *,
    now_utc: datetime,
    throttle_minutes: int,
) -> datetime | None:
    """Return the UTC timestamp until which we must defer, or None if
    the throttle window is clear.

    We treat only messages that were actually delivered (or pending
    delivery) as occupying the throttle window. Dropped/muted rows
    don't count — they never reached the user.
    """
    window = timedelta(minutes=throttle_minutes)
    cutoff = now_utc - window
    # Messages that "touched" the user: sent OR still pending delivery.
    touching = [
        row
        for row in recent
        if row.status in {"sent", "pending"} and _ensure_utc(row.created_at) >= cutoff
    ]
    if not touching:
        return None
    # Defer to ``oldest_touching + window`` — preserves rate while
    # pushing new messages to the end of the rolling window.
    oldest = min(_ensure_utc(row.created_at) for row in touching)
    return oldest + window


def _decode_outbox_text(row: OutboxMessage) -> str:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return ""
    text = payload.get("text")
    return text.strip() if isinstance(text, str) else ""
