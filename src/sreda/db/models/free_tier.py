"""Free-tier usage counter — per-(tenant,user,day) счётчик LLM turn'ов.

Юзер на бесплатном тарифе ограничен лимитом (сейчас 20) LLM-вызовов
в день. Счётчик инкрементируется в ``execute_conversation_chat`` сразу
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

from sqlalchemy import Date, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class FreeTierUsage(Base):
    __tablename__ = "free_tier_usage"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
