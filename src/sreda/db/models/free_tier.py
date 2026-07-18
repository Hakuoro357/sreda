"""Free-tier usage counter — per-(tenant,user,day) счётчик LLM turn'ов.

Юзер на бесплатном тарифе ограничен лимитом
(``usage_ledger.SREDA_FREE_LLM_DAILY``) LLM-вызовов в день. Счётчик инкрементируется в ``execute_conversation_chat`` сразу
ПОСЛЕ approval-гейта, но ДО вызова LLM. Если лимит превышен —
отправляется отлуп-текст с кнопками «Оформить подписку / Напомнить
завтра / Понятно».

Сбрасывается не явно — просто используется новая `day`-строка на
следующий календарный день. Старые строки не удаляем — нужны для
аналитики (retention vs лимит).

NB: не засчитываются:
- Callback rem_done / rem_snooze (служебка).
- Проактивные напоминания (worker, не юзер).
- Scripted-ответы pending-бота (нет LLM).
- Tool-iterations внутри одного turn'а (1 turn = 1 call).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class FreeTierUsage(Base):
    __tablename__ = "free_tier_usage"

    # Композитный UNIQUE на тройку — нужен для UPSERT-pattern в
    # `services/free_tier._get_or_create`. На проде создаёт миграция 0024
    # (`ix_free_tier_usage_unique`, unique=True); дублируем в модели,
    # чтобы `create_all` в тестах (SQLite) давал тот же констрейнт
    # (дрейф модель↔миграция — audit 2026-07-18 db-migrations #2;
    # эталон оформления — `web_search.py`).
    __table_args__ = (
        Index(
            "ix_free_tier_usage_unique",
            "tenant_id", "user_id", "day",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
