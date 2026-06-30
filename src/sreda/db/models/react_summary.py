"""#232 способ Б: durable-выжимка истории ReAct как ОТДЕЛЬНАЯ строка (не канал чекпойнта).

Почему отдельная таблица, а не запись в checkpoint (#193):
- Выжимка — НЕ новое событие разговора, а пересказ уже существующего в БД. Она не имеет права
  числиться «самым свежим» состоянием. Хранилище чекпойнтов каждой записи ставит свежую метку →
  «в прошлое» туда не записать → запись выжимки в чекпойнт затирала бы параллельный ход (гонка,
  код-ревью R1 CRITICAL). Отдельная строка к таймлайну разговора не относится → гонки нет by
  construction, замок не нужен.

Привязка к прошлому (поправка владельца): запись несёт «докуда покрыто» (covered_message_count +
covered_hash) и метку последнего покрытого сообщения (covered_through_ts). Потребление
(build_model_input) сверяет covered_hash с реальным префиксом; новые сообщения ПОСЛЕ покрытого
показываются сырыми. ПД: summary_text шифруется EncryptedString. GC 30д — по updated_at.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


class ReactSummary(Base):
    __tablename__ = "react_summaries"

    # durable-ключ треда (react:{tenant}:{hmac(chat)} → _durable_thread_id), как в react_checkpoint.
    thread_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    # scope/ops/GC-by-tenant (НЕ FK — как react_checkpoint, без связи с tenants).
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # текст выжимки — ПД (контент переписки) → шифр.
    summary_text: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    # «докуда покрыто»: сколько ведущих сообщений и хэш их содержимого (применимость на потреблении).
    covered_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # привязка к прошлому: метка последнего покрытого сообщения (as-of). Best-effort, может быть NULL.
    covered_through_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # когда строка последний раз перезаписана — для GC (30д) и свежести. НЕ таймлайн разговора.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_react_summaries_updated_at", "updated_at"),  # GC-свип по возрасту
    )
