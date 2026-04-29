"""WebSearchUsageCounter — квоты Tavily web_search'а per-user + global.

Tavily free tier — 1000 query/мес на API key (общий пул всех юзеров).
Чтобы один heavy-юзер не съел квоту у остальных + не выйти за
1000 в месяц — два слоя:

* `PER_USER_LIMIT = 30/мес` — soft cap. После — fallback на DDG.
* `GLOBAL_LIMIT = 950/мес` — hard cap. 50 запас от 1000 для edge cases.

Период reset'ится по календарным месяцам (`year_month` = "YYYY-MM").

Mirror паттерна `FreeTierCounter` (`services/free_tier.py`):
read-modify-write через `_get_or_create`, без INSERT...ON CONFLICT
(SQLite тянет, но текущий codebase везде через get-or-create).

Admin-API: `admin_summary()` — общий total/remaining за текущий
месяц для dashboard'а; `admin_per_user(year_month)` — список строк
по юзерам, sorted descending по tavily_calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from sreda.db.models.web_search import WebSearchUsage

logger = logging.getLogger(__name__)


PER_USER_LIMIT = 30
GLOBAL_LIMIT = 950


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _current_year_month() -> str:
    """Returns YYYY-MM (UTC). reset rolls over по календарным месяцам."""
    return _utcnow().strftime("%Y-%m")


@dataclass(slots=True)
class WebSearchAdminSummary:
    """Глобальная сводка для админ-страницы за текущий месяц."""

    year_month: str
    tavily_calls_total: int
    fallback_calls_total: int
    tavily_remaining: int  # GLOBAL_LIMIT - tavily_calls_total (clamp ≥0)
    global_limit: int
    per_user_limit: int


@dataclass(slots=True)
class WebSearchUserRow:
    """Per-юзер строка в админ-таблице."""

    tenant_id: str
    user_id: str
    tenant_name: str | None
    tavily_calls: int
    fallback_calls: int
    user_remaining: int  # PER_USER_LIMIT - tavily_calls (clamp ≥0)


class WebSearchUsageCounter:
    """Шлюз для квот web_search-tool'а.

    Используется внутри tool'а (`_call_tavily` / `_call_ddg_fallback`)
    + админ-эндпоинт читает summary/per_user.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Quota check / record
    # ------------------------------------------------------------------

    def can_use_tavily(self, *, tenant_id: str, user_id: str) -> bool:
        """True если юзер может использовать Tavily.

        Блокируется при:
        * `tavily_calls >= PER_USER_LIMIT` для конкретного юзера
        * `tavily_calls_total >= GLOBAL_LIMIT` суммарно за месяц

        В обоих случаях caller должен fall'нуться на DDG.
        """
        if not tenant_id or not user_id:
            return False
        ym = _current_year_month()

        # Per-user check
        row = self._get_or_create(tenant_id, user_id, ym)
        if (row.tavily_calls or 0) >= PER_USER_LIMIT:
            return False

        # Global check
        if self._global_tavily_total(ym) >= GLOBAL_LIMIT:
            return False

        return True

    def record_tavily(self, *, tenant_id: str, user_id: str) -> None:
        """+1 к `tavily_calls` для текущего месяца."""
        if not tenant_id or not user_id:
            return
        ym = _current_year_month()
        row = self._get_or_create(tenant_id, user_id, ym)
        row.tavily_calls = (row.tavily_calls or 0) + 1
        row.updated_at = _utcnow()
        self.session.flush()

    def record_fallback(self, *, tenant_id: str, user_id: str) -> None:
        """+1 к `fallback_calls` (DDG-fallback hit)."""
        if not tenant_id or not user_id:
            return
        ym = _current_year_month()
        row = self._get_or_create(tenant_id, user_id, ym)
        row.fallback_calls = (row.fallback_calls or 0) + 1
        row.updated_at = _utcnow()
        self.session.flush()

    # ------------------------------------------------------------------
    # Admin views
    # ------------------------------------------------------------------

    def admin_summary(self, year_month: str | None = None) -> WebSearchAdminSummary:
        """Сводка для текущего месяца (или указанного)."""
        ym = year_month or _current_year_month()
        tavily_total = self._global_tavily_total(ym)
        fallback_total = self._global_fallback_total(ym)
        return WebSearchAdminSummary(
            year_month=ym,
            tavily_calls_total=tavily_total,
            fallback_calls_total=fallback_total,
            tavily_remaining=max(0, GLOBAL_LIMIT - tavily_total),
            global_limit=GLOBAL_LIMIT,
            per_user_limit=PER_USER_LIMIT,
        )

    def admin_per_user(
        self, year_month: str | None = None,
    ) -> list[WebSearchUserRow]:
        """Per-юзер строки за указанный месяц (default — текущий).

        Joins на `tenants` для отображения имени тенанта в админке.
        Sorted descending по tavily_calls (топ юзеров сверху).
        """
        from sreda.db.models.core import Tenant

        ym = year_month or _current_year_month()
        rows = (
            self.session.query(
                WebSearchUsage.tenant_id,
                WebSearchUsage.user_id,
                Tenant.name,
                WebSearchUsage.tavily_calls,
                WebSearchUsage.fallback_calls,
            )
            .outerjoin(Tenant, Tenant.id == WebSearchUsage.tenant_id)
            .filter(WebSearchUsage.year_month == ym)
            .order_by(WebSearchUsage.tavily_calls.desc())
            .all()
        )
        return [
            WebSearchUserRow(
                tenant_id=r[0],
                user_id=r[1],
                tenant_name=r[2],
                tavily_calls=r[3] or 0,
                fallback_calls=r[4] or 0,
                user_remaining=max(0, PER_USER_LIMIT - (r[3] or 0)),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_create(
        self, tenant_id: str, user_id: str, year_month: str,
    ) -> WebSearchUsage:
        row = (
            self.session.query(WebSearchUsage)
            .filter(
                WebSearchUsage.tenant_id == tenant_id,
                WebSearchUsage.user_id == user_id,
                WebSearchUsage.year_month == year_month,
            )
            .one_or_none()
        )
        if row is not None:
            return row
        row = WebSearchUsage(
            id=f"wsu_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            year_month=year_month,
            tavily_calls=0,
            fallback_calls=0,
            updated_at=_utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _global_tavily_total(self, year_month: str) -> int:
        result = (
            self.session.query(func.coalesce(func.sum(WebSearchUsage.tavily_calls), 0))
            .filter(WebSearchUsage.year_month == year_month)
            .scalar()
        )
        return int(result or 0)

    def _global_fallback_total(self, year_month: str) -> int:
        result = (
            self.session.query(func.coalesce(func.sum(WebSearchUsage.fallback_calls), 0))
            .filter(WebSearchUsage.year_month == year_month)
            .scalar()
        )
        return int(result or 0)
