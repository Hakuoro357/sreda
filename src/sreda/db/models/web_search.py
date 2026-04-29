"""Web-search usage counter — per-(tenant, user, year_month) счётчик
вызовов `web_search` tool через Tavily.

Tavily free tier — 1000 query/мес на API key (общий пул, не per-user).
Чтобы heavy-юзер не съедал quota у остальных + не выйти за общие 1000,
вводим два слоя:

* `PER_USER_LIMIT = 30/мес` — soft cap, после fallback на DDG
* `GLOBAL_LIMIT = 950/мес` — 50 запас на edge cases

Период reset'ится по календарным месяцам (`year_month` = "YYYY-MM").
Старые строки не удаляем — нужны для admin-аналитики (топ юзеров,
конверсия в fallback).

Mirror'им паттерн `FreeTierUsage` (`free_tier.py`): UNIQUE(tenant_id,
user_id, year_month), read-modify-write через `_get_or_create`.
Отдельная колонка `fallback_calls` — DDG-fallback hits (когда квота
исчерпана), для отделения от основных Tavily-вызовов в админке.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class WebSearchUsage(Base):
    __tablename__ = "web_search_usage"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    year_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    tavily_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
