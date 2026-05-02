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


@dataclass(frozen=True, slots=True)
class RecallStats:
    """Diagnostic stats for one recall() invocation.

    Used by ``node_load_memories`` (Stage 2 observability of the
    memory retrieval staged plan, see ``docs/tomorrow-plan.md`` пункт
    11). Lets us answer questions like:

    * how many memories does this user actually have on disk?
    * how many had embeddings (vs. needing re-embed)?
    * how many were dropped by ``min_score``?
    * what does the score distribution look like?

    All numeric. Logged structured so we can grep/awk later.
    """

    candidates_total: int       # all memories for (tenant, user, tier)
    with_embedding: int         # subset that produced a valid cosine score
                                # (= passed the JSON parse + has embedding_json
                                # gate; equivalent to filtered_below_min +
                                # len(passing_scores))
    filtered_below_min: int     # scored but dropped by min_score
    seeded_count: int           # final returned (after top_k cut)
    min_score: float            # threshold used
    top_k: int                  # cap used
    scores_min: float | None    # min over passing scores (None if none passed)
    scores_max: float | None    # max over passing scores
    scores_p50: float | None    # median over passing scores


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
        isn't polluted with irrelevant rows.

        Backward-compatible thin wrapper over ``recall_with_stats``;
        callers that don't need observability stats get the bare hit
        list as before. Use ``recall_with_stats`` when logging the
        retrieval-distribution diagnostics (e.g. ``node_load_memories``
        Stage 2).
        """
        hits, _stats = self.recall_with_stats(
            tenant_id, user_id, query_embedding,
            tier=tier, top_k=top_k, min_score=min_score,
        )
        return hits

    def recall_with_stats(
        self,
        tenant_id: str,
        user_id: str,
        query_embedding: list[float],
        *,
        tier: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> tuple[list[MemoryRecallHit], RecallStats]:
        """Same as ``recall`` but also returns ``RecallStats``.

        Stats are computed over the *full* candidate pool — useful for
        observability (Stage 2 of memory-retrieval roadmap). Score
        percentiles are over the rows that passed ``min_score`` (i.e.
        what the LLM might actually see), not the raw cosine
        distribution, because pre-filter scores include noise.
        """
        candidates = self.list_by_user(tenant_id, user_id, tier=tier)
        candidates_total = len(candidates)
        with_embedding = 0
        passing_scores: list[float] = []
        filtered_below_min = 0
        hits: list[MemoryRecallHit] = []
        for row in candidates:
            if not row.embedding_json:
                continue
            try:
                vec = json.loads(row.embedding_json)
            except json.JSONDecodeError:
                continue
            with_embedding += 1
            score = cosine_similarity(query_embedding, vec)
            if score < min_score:
                filtered_below_min += 1
                continue
            passing_scores.append(score)
            hits.append(MemoryRecallHit(memory=row, score=score))
        hits.sort(key=lambda h: h.score, reverse=True)
        seeded = hits[:top_k]
        scores_min = min(passing_scores) if passing_scores else None
        scores_max = max(passing_scores) if passing_scores else None
        scores_p50 = (
            sorted(passing_scores)[len(passing_scores) // 2]
            if passing_scores
            else None
        )
        stats = RecallStats(
            candidates_total=candidates_total,
            with_embedding=with_embedding,
            filtered_below_min=filtered_below_min,
            seeded_count=len(seeded),
            min_score=min_score,
            top_k=top_k,
            scores_min=scores_min,
            scores_max=scores_max,
            scores_p50=scores_p50,
        )
        return seeded, stats

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
