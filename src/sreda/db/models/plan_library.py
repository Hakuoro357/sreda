"""#135 — библиотека эталонных планов (срез 1).

СЫРЬЁ НЕ ХРАНИТСЯ (план r4, R2 high): только redacted-форма без PII.
TTL: candidate/retired/anti 90д, approved 180д (retention_cleanup).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class PlanLibraryEntry(Base):
    __tablename__ = "plan_library_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # у synthetic-кейсов run_id нет; уникальность — частичные индексы ниже
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_variant: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="candidate",
                                        nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="prod_turn",
                                        nullable=False)
    # БЕЗ PII: typed-allowlist проекция (property-тест в чек-листе)
    form_json: Mapped[str] = mapped_column(Text, nullable=False)
    form_tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    outcome_json: Mapped[str] = mapped_column(Text, nullable=False,
                                              default="{}")
    # вектор от redacted-формы (добор фоном; НЕ от текста пользователя)
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_form_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    registry_version: Mapped[str | None] = mapped_column(String(64),
                                                         nullable=True)
    composer_snapshot_hash: Mapped[str | None] = mapped_column(String(64),
                                                               nullable=True)
    # аудит ручной курации (авто-approve запрещён планом)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("ix_plan_library_tenant_status_created",
              "tenant_id", "status", "created_at"),
        Index("ux_plan_library_tenant_run", "tenant_id", "run_id",
              unique=True,
              postgresql_where=sa_text("run_id IS NOT NULL"),
              sqlite_where=sa_text("run_id IS NOT NULL")),
        Index("ux_plan_library_tenant_case", "tenant_id", "case_id",
              "case_variant", unique=True,
              postgresql_where=sa_text("case_id IS NOT NULL"),
              sqlite_where=sa_text("case_id IS NOT NULL")),
    )
