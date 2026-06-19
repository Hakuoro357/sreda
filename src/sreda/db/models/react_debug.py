"""ВРЕМЕННАЯ debug-таблица ходов ReAct-цикла (vex#170).

Назначение: пока тестируем новый разговорный механизм, видеть ВСЮ переписку (вопрос
пользователя + ответ бота + какие инструменты звались). На react-пути ответ шлётся инлайн и
штатно НЕ сохраняется (privacy by design), поэтому без этого разбор инцидентов невозможен из
хранилища. Текст шифруется (EncryptedString), запись гейтится ALLOWLIST'ом тенантов
``SREDA_REACT_DEBUG_TENANTS`` (settings.react_debug_tenants; дефолт пуст → никому, пишем только
явно перечисленным дебаг/тест-тенантам). **Временное — удалить вместе с таблицей после
тестирования** (отдельная миграция drop).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


class ReactDebugTurn(Base):
    __tablename__ = "react_debug_turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), default="react")
    # kind: final | pause | error — что за ответ ушёл пользователю
    kind: Mapped[str] = mapped_column(String(16), default="final")
    # ПД → шифруем на уровне приложения (как inbound_messages); читать через ORM
    user_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    reply_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    # имена вызванных инструментов за ход (JSON-список), без аргументов — не ПД
    tools_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_react_debug_tenant_created", "tenant_id", "created_at"),
        Index("ix_react_debug_created", "created_at"),  # ручная очистка/ретеншн
    )
