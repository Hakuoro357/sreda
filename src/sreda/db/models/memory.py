"""Assistant memory store (Phase 3).

Single table for all per-user memory tiers; ``tier`` discriminates:

  * ``core``     — stable facts the agent learned about the user
                   ("у меня дочь Маша 9 лет"). Long-lived.
  * ``episodic`` — events / conversation summaries. Short-lived context
                   the agent can refer to ("вчера жаловался на сроки").

``procedural`` tier (auto-learned behaviour patterns) is NOT in MVP — it
requires a background consolidation process that's out of scope for
Phase 3. Plan to add later when needed.

Embeddings live as JSON-encoded float arrays in ``embedding_json``. For
MVP scale (10-200 memories per user) Python-side cosine similarity at
recall time is fast enough; migration to pgvector or dedicated vector
DB is a drop-in replacement that swaps only the repository layer.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AssistantMemory(Base):
    __tablename__ = "assistant_memories"
    __table_args__ = (
        Index("ix_assistant_memories_tenant_user_tier", "tenant_id", "user_id", "tier"),
        Index("ix_assistant_memories_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    # "core" | "episodic" — tier values enumerated in repository.
    tier: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)

    # JSON-encoded list[float]. Nullable because a memory can be saved
    # even if embeddings are disabled — we'll just skip it in cosine
    # recall and find it via other means (e.g. LLM scanning all).
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)

    # Provenance. Useful for audit + future "forget everything the agent
    # inferred without my confirmation" feature.
    #   "user_direct"    — user stated it directly ("я живу в Москве")
    #   "agent_inferred" — agent extracted from conversation
    #   "system"         — seeded from profile or elsewhere
    source: Mapped[str] = mapped_column(String(32), default="agent_inferred")

    # Access tracking — future recency boosting can multiply cosine by
    # decay(last_accessed_at); also used as LRU eviction signal.
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
