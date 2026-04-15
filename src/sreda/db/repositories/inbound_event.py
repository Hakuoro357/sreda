"""Inbound event CRUD + lookup helpers (Phase 4)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.inbound_event import InboundEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class InboundEventDraft:
    """Skill-produced event ready for platform ingestion.

    Fields:
      * ``external_event_key`` — stable identifier so repeat submissions
        of the same logical event dedupe via UNIQUE constraint.
      * ``relevance_score`` — skill's own judgement 0..1. Platform takes
        it as-is; no cheap-LLM classifier in MVP.
      * ``user_id`` — optional; when present the proactive handler will
        resolve profile + budget for this user specifically.
    """

    tenant_id: str
    feature_key: str
    event_type: str
    external_event_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    # ``None`` → skill hasn't scored this event; row lands with
    # ``status='needs_classification'`` and waits for the future
    # relevance-classifier worker (LLM-based). Most MVP skills set
    # a concrete score here from their own domain rules.
    relevance_score: float | None = 0.0
    relevance_reason: str | None = None


class InboundEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- write

    def create_from_draft(
        self,
        draft: InboundEventDraft,
        *,
        threshold: float = 0.5,
    ) -> InboundEvent | None:
        """Insert a new event, swallowing dedup conflicts.

        When the skill already sets a ``relevance_score`` >= threshold,
        we mark the row ``classified`` immediately — no need for a
        separate classifier pass. Returns the inserted row, or ``None``
        if the external_event_key was already ingested."""
        now = _utcnow()
        # Three-way status at ingest time:
        #   * draft.relevance_score is None       → needs_classification
        #     (waiting for a future LLM-classifier worker to score it)
        #   * draft.relevance_score >= threshold  → classified
        #   * otherwise                           → new (below threshold,
        #     picked up by nothing until manually re-scored)
        if draft.relevance_score is None:
            initial_status = "needs_classification"
            score_value = 0.0
            classified = False
        else:
            score_value = float(draft.relevance_score)
            classified = score_value >= threshold
            initial_status = "classified" if classified else "new"
        # Explicit dedup check first — using rollback() on IntegrityError
        # would throw away the caller's other pending work in the same
        # session, which is surprising. The UNIQUE constraint still
        # protects against true races at the DB layer.
        existing = (
            self.session.query(InboundEvent)
            .filter_by(
                feature_key=draft.feature_key,
                external_event_key=draft.external_event_key,
            )
            .first()
        )
        if existing is not None:
            return None

        row = InboundEvent(
            id=f"ie_{uuid4().hex[:24]}",
            tenant_id=draft.tenant_id,
            user_id=draft.user_id,
            feature_key=draft.feature_key,
            event_type=draft.event_type,
            external_event_key=draft.external_event_key,
            payload_json=json.dumps(draft.payload, ensure_ascii=False, sort_keys=True),
            relevance_score=score_value,
            relevance_reason=draft.relevance_reason,
            status=initial_status,
            created_at=now,
            classified_at=now if classified else None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def mark_status(
        self,
        event_id: str,
        *,
        status: str,
        reason: str | None = None,
    ) -> InboundEvent | None:
        if status not in {"new", "needs_classification", "classified", "consumed", "skipped"}:
            raise ValueError(f"unknown status: {status!r}")
        row = self.session.get(InboundEvent, event_id)
        if row is None:
            return None
        row.status = status
        row.status_reason = reason
        if status == "classified" and row.classified_at is None:
            row.classified_at = _utcnow()
        if status == "consumed" and row.consumed_at is None:
            row.consumed_at = _utcnow()
        self.session.flush()
        return row

    # ---------------------------------------------------------------- read

    def list_ready_for_delivery(
        self, *, limit: int = 50, min_score: float = 0.5
    ) -> list[InboundEvent]:
        """Classified + not-yet-consumed events over the score threshold."""
        return (
            self.session.query(InboundEvent)
            .filter(
                InboundEvent.status == "classified",
                InboundEvent.relevance_score >= min_score,
            )
            .order_by(InboundEvent.created_at.asc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def decode_payload(event: InboundEvent) -> dict[str, Any]:
        try:
            value = json.loads(event.payload_json or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
