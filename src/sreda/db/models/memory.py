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

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AssistantMemory(Base):
    __tablename__ = "assistant_memories"
    __table_args__ = (
        Index("ix_assistant_memories_tenant_user_tier", "tenant_id", "user_id", "tier"),
        Index("ix_assistant_memories_created_at", "created_at"),
        # #262: composite FK — факт можно привязать ТОЛЬКО к категории СВОЕГО (tenant,user).
        # Требует родительский UNIQUE(id,tenant_id,user_id) на memory_categories (см. ниже).
        # Энфорсится только при foreign_keys=ON (тесты ставят PRAGMA; g-061).
        ForeignKeyConstraint(
            ["category_id", "tenant_id", "user_id"],
            ["memory_categories.id", "memory_categories.tenant_id", "memory_categories.user_id"],
            name="fk_assistant_memories_category",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    # "core" | "episodic" — tier values enumerated in repository.
    tier: Mapped[str] = mapped_column(String(16))
    # #262: пользовательская категория. Финальное состояние = NOT NULL (миграция 20260630_0072); save() всегда
    # резолвит Common, поэтому модель совместима с nullable-окном деплоя (0070→0071→0072). Держим
    # nullable=False в модели, чтобы create_all-тесты НЕ принимали NULL (иначе дрейф модель↔прод, #74).
    # Скоуп факта↔категории гарантирует composite FK выше.
    category_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Encrypted at rest (AES-256-GCM). Reads return plaintext to the
    # ORM caller; writes accept plaintext. Legacy rows written before
    # this column was marked encrypted still decode transparently via
    # the TypeDecorator's envelope-sniffing fallback.
    content: Mapped[str] = mapped_column(EncryptedString())

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


class MemoryCategory(Base):
    """#262: пользовательская категория памяти (новый слой поверх системного ``tier``).

    «Common» (``is_system=True``, ``slug='common'``) — дом по умолчанию для фактов без явной категории;
    неизменяема (гейт в коде repo/API + partial-unique «≤1 system на (tenant,user)» как страховка БД).
    Имя — PLAINTEXT (как ``reminder.title``; решение владельца #6); уникальность по ``name_normalized``
    (``services.text_normalization.normalize_for_dedup``). Скоуп — строго per ``(tenant_id, user_id)``.
    """

    __tablename__ = "memory_categories"
    __table_args__ = (
        # Родитель composite-FK из assistant_memories (нужен явный UNIQUE на тройку, иначе СУБД отвергнет FK).
        UniqueConstraint("id", "tenant_id", "user_id", name="uq_memory_categories_id_scope"),
        # Нет визуальных дублей имени в скоупе (name_normalized = normalize_for_dedup).
        UniqueConstraint("tenant_id", "user_id", "name_normalized", name="uq_memory_categories_name"),
        # Ровно одна system-категория (Common) на (tenant,user) — гарантия БД (partial-unique).
        Index("uq_memory_categories_one_system", "tenant_id", "user_id", unique=True,
              postgresql_where=text("is_system"), sqlite_where=text("is_system")),
        Index("ix_memory_categories_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Без per-column index — лукапы идут по композитному ix_memory_categories_tenant_user (см. __table_args__);
    # миграция 20260630_0070 создаёт только его, поэтому index=True здесь дал бы дрейф create_all↔Postgres (R2).
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 'common' у системной Common; NULL у пользовательских.
    slug: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(120))           # plaintext (как reminder.title)
    name_normalized: Mapped[str] = mapped_column(String(160))  # normalize_for_dedup(name) — для уникальности
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
