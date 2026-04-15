"""Memory repository (Phase 3).

CRUD + cosine-similarity recall over ``AssistantMemory`` rows. Keeps the
JSON encoding of embeddings in one place so callers deal in plain
``list[float]`` vectors.

At MVP scale (up to a few hundred memories per user) we do the cosine
in Python — no vector index needed. When that stops being fast enough,
swap this file to use pgvector (indexed ``<->`` / ``<=>`` operator) and
the rest of the app stays unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.memory import AssistantMemory
from sreda.services.embeddings import cosine_similarity


MEMORY_TIERS = frozenset({"core", "episodic"})
MEMORY_SOURCES = frozenset({"user_direct", "agent_inferred", "system"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return f"mem_{uuid4().hex[:24]}"


@dataclass(frozen=True, slots=True)
class MemoryRecallHit:
    """One recall result — the memory row plus its similarity score.

    Kept as a frozen value object so callers don't mutate the memory
    row in place; writing back (e.g. ``touch_accessed``) goes through
    repo methods that respect session state."""

    memory: AssistantMemory
    score: float


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------- save

    def save(
        self,
        tenant_id: str,
        user_id: str,
        *,
        tier: str,
        content: str,
        embedding: list[float] | None = None,
        source: str = "agent_inferred",
    ) -> AssistantMemory:
        if tier not in MEMORY_TIERS:
            raise ValueError(f"unknown tier: {tier!r}")
        if source not in MEMORY_SOURCES:
            raise ValueError(f"unknown source: {source!r}")
        text = (content or "").strip()
        if not text:
            raise ValueError("memory content cannot be empty")

        row = AssistantMemory(
            id=_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            tier=tier,
            content=text,
            embedding_json=json.dumps(embedding) if embedding else None,
            embedding_dim=len(embedding) if embedding else 0,
            source=source,
            created_at=_utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    # ------------------------------------------------------------------- read

    def list_by_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        tier: str | None = None,
        limit: int | None = None,
    ) -> list[AssistantMemory]:
        q = self.session.query(AssistantMemory).filter_by(
            tenant_id=tenant_id, user_id=user_id
        )
        if tier is not None:
            q = q.filter(AssistantMemory.tier == tier)
        q = q.order_by(AssistantMemory.created_at.desc())
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def get(self, memory_id: str) -> AssistantMemory | None:
        return self.session.get(AssistantMemory, memory_id)

    # ----------------------------------------------------------------- recall

    def recall(
        self,
        tenant_id: str,
        user_id: str,
        query_embedding: list[float],
        *,
        tier: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[MemoryRecallHit]:
        """Cosine-similarity recall over the user's memories.

        Rows without a stored embedding are skipped (they'll need an
        out-of-band re-embed migration when embeddings get turned on).
        ``min_score`` filters out near-zero matches so the LLM context
        isn't polluted with irrelevant rows."""
        candidates = self.list_by_user(tenant_id, user_id, tier=tier)
        hits: list[MemoryRecallHit] = []
        for row in candidates:
            if not row.embedding_json:
                continue
            try:
                vec = json.loads(row.embedding_json)
            except json.JSONDecodeError:
                continue
            score = cosine_similarity(query_embedding, vec)
            if score < min_score:
                continue
            hits.append(MemoryRecallHit(memory=row, score=score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    # ----------------------------------------------------------------- update

    def touch_accessed(self, memory_id: str) -> None:
        row = self.session.get(AssistantMemory, memory_id)
        if row is None:
            return
        row.access_count = (row.access_count or 0) + 1
        row.last_accessed_at = _utcnow()
        self.session.flush()

    def delete(self, memory_id: str) -> bool:
        row = self.session.get(AssistantMemory, memory_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True
