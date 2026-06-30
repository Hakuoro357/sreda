"""#163 субстрат прод-идемпотентности — append-only результат durable-операции.

claim-first (план #163, принят R5): на каждую durable-операцию пишем строку с PII-free
``operation_id`` (GLOBALLY unique — как ``audit_outbox.operation_id`` / ``step_execution_ledger``).
На повторе того же ``operation_id`` с тем же ``args_hmac`` и статусом ``committed`` — возвращаем
сохранённый ``stable_return_payload`` БЕЗ повторной мутации (exact-replay). Делает идемпотентность
устойчивой даже для hard-delete (доменной строки уже нет, а tombstone-строка результата жива до
``tombstone_until``).

ПД: ``stable_return_payload`` может нести название напоминания → ``JSONEncryptedString`` (шифр на
уровне модели, vex p-002). ``args_hmac`` — HMAC, не сырые аргументы.

Фаза 1в (#163): таблица + exact-replay в helper. Семантический межходовой замок — Фаза 2.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import JSONEncryptedString

# статусы claim/результата (источник истины — здесь; helper ветвится по ним)
TOOL_OP_STATUSES = ("pending", "committed", "failed_transient", "failed_permanent")


class ToolOperationResult(Base):
    """Одна строка на durable-операцию (claim → committed/failed). Append-only по смыслу;
    status переходит pending→committed/failed в той же per-operation транзакции helper'а."""

    __tablename__ = "tool_operation_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # плоский String (без FK) — как react_debug_turns; скоуп тенанта проверяется в коде helper'а.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # user_id nullable — общесемейные правила (как family_reminders); скоуп проверяется в коде.
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # логический эффект (НЕ имя tool — один эффект может звать разные tool по путям; план #163).
    operation_family: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # PII-free opaque idempotency key, GLOBALLY unique (keyspace audit_outbox/step_ledger).
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # HMAC-SHA256(secret) от canonical args — НЕ сырые аргументы (ПД). mismatch = внутр. ошибка.
    args_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # tool-контрактный return (НЕ полный snapshot). ПД → шифр.
    stable_return_payload: Mapped[str | None] = mapped_column(JSONEncryptedString, nullable=True)
    # ссылка на доменную сущность (no-FK — переживает hard-delete; для отладки/replay).
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # до этого момента строка-tombstone жива (hard-delete replay «уже сделано»); прунинг после.
    tombstone_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # GLOBAL уникальность operation_id → exact-replay lookup по нему (как step_ledger),
        # без NULL-user-коллизии (operation_id сам по себе уникален на операцию).
        UniqueConstraint("operation_id", name="uq_tool_operation_results_operation_id"),
        Index("ix_tool_operation_results_scope", "tenant_id", "user_id", "operation_family"),
    )
