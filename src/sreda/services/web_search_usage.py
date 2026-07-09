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

import enum
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
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


# ----------------------------------------------------------------------
# Atomic per-call consume (Фаза 1 #200) — образец UsageLedgerService
# ----------------------------------------------------------------------


class QuotaDecision(str, enum.Enum):
    """Решение атомарного `try_consume_tavily`.

    * ``ALLOW`` — слот зарезервирован (per-user И global), можно звать Tavily.
    * ``USER_EXHAUSTED`` — per-user cap достигнут (free 6-й вызов): инкремента
      НЕ было, Tavily не звать, DDG не звать.
    * ``GLOBAL_EXHAUSTED`` — глобальные 950 исчерпаны: инкремента НЕ было,
      Tavily не звать никому; non-free → DDG, free → транзиентная ошибка.
    """

    ALLOW = "allow"
    USER_EXHAUSTED = "user_exhausted"
    GLOBAL_EXHAUSTED = "global_exhausted"


def try_consume_tavily(
    engine: Engine,
    tenant_id: str,
    user_id: str,
    per_user_cap: int | None,
    global_cap: int = GLOBAL_LIMIT,
    year_month: str | None = None,
) -> QuotaDecision:
    """Атомарно зарезервировать один Tavily-вызов за текущий месяц.

    По образцу ``UsageLedgerService.try_consume``: СОБСТВЕННАЯ
    ``engine.begin()``-транзакция (НЕ session хода → нет autobegin-trap),
    PG SERIALIZABLE + retry SQLSTATE 40001 (3× backoff 50ms).

    Порядок в одной транзакции:
      1. Глобальная проверка: ``SUM(tavily_calls) >= global_cap`` →
         ``GLOBAL_EXHAUSTED`` (rollback, без инкремента).
      2. Per-user условный upsert:
         * ``per_user_cap is None`` — безусловный +1 (INSERT…ON CONFLICT
           DO UPDATE SET tavily_calls=tavily_calls+1 RETURNING).
         * ``per_user_cap`` задан — INSERT-путь ``WHERE 1 <= cap``,
           UPDATE-путь ``WHERE tavily_calls + 1 <= cap``; вернул строку →
           ``ALLOW``, нет → ``USER_EXHAUSTED``.

    Args:
        engine: SQLAlchemy Engine (из ``session.get_bind()`` в замыкании).
        tenant_id, user_id: scope.
        per_user_cap: per-user лимит (free=5) или None (grandfathered/платные).
        global_cap: глобальный hard cap (default 950).
        year_month: 'YYYY-MM' UTC; default — текущий месяц.

    Returns:
        QuotaDecision.

    Note:
        `year_month` — UTC-семантика существующей таблицы (НЕ MSK ledger).
    """
    if not tenant_id or not user_id:
        # Защита: без scope трактуем как исчерпание (fail-closed снаружи
        # решает по тиру). Без инкремента.
        return QuotaDecision.USER_EXHAUSTED
    ym = year_month or _current_year_month()
    is_pg = engine.dialect.name == "postgresql"
    # #331: изоляция через execution_options (на соединении, ДО BEGIN), а НЕ ручным `SET TRANSACTION`
    # внутри tx — тот конфликтует с begin-событием #138 (set_config GUC первым запросом) →
    # ActiveSqlTransaction, крэшив ход с web_search. См. usage_ledger.try_consume.
    _engine = engine.execution_options(isolation_level="SERIALIZABLE") if is_pg else engine

    for attempt in range(3):
        try:
            with _engine.begin() as conn:
                # 1. Глобальный резерв в ТОЙ ЖЕ txn (SERIALIZABLE +
                #    retry 40001 ловит phantom/write-skew у параллельных).
                global_total = conn.execute(text("""
                    SELECT COALESCE(SUM(tavily_calls), 0)
                    FROM web_search_usage
                    WHERE year_month = :ym
                """), {"ym": ym}).scalar()
                if int(global_total or 0) >= global_cap:
                    # rollback (engine.begin() закроет без коммита через raise)
                    raise _GlobalExhausted()

                # 2. Per-user условный upsert.
                row = _upsert_one(conn, tenant_id, user_id, ym, per_user_cap)
                if row is None:
                    raise _UserExhausted()
                return QuotaDecision.ALLOW
        except _GlobalExhausted:
            return QuotaDecision.GLOBAL_EXHAUSTED
        except _UserExhausted:
            return QuotaDecision.USER_EXHAUSTED
        except DBAPIError as exc:
            sqlstate = (
                getattr(exc.orig, "sqlstate", None)
                or getattr(exc.orig, "pgcode", None)
            )
            if sqlstate == "40001":  # serialization_failure
                backoff = 0.05 * (attempt + 1)
                logger.info(
                    "web_search consume serialization conflict, "
                    "retry attempt=%d after %.0fms",
                    attempt + 1, backoff * 1000,
                )
                time.sleep(backoff)
                continue
            raise
    raise RuntimeError(
        "try_consume_tavily retried 3× — все serialization failures (40001)"
    )


class _GlobalExhausted(Exception):
    """Внутренний сигнал — global cap исчерпан, rollback txn."""


class _UserExhausted(Exception):
    """Внутренний сигнал — per-user cap исчерпан, rollback txn."""


def _upsert_one(
    conn, tenant_id: str, user_id: str, year_month: str,
    per_user_cap: int | None,
):
    """Один условный upsert +1. Returns row (truthy) или None.

    INSERT…ON CONFLICT DO UPDATE с RETURNING (PG ≥9.5, SQLite ≥3.35).
    `per_user_cap is None` → безусловный инкремент; иначе guard на
    INSERT-пути (``WHERE 1 <= cap``) И UPDATE-пути (``+1 <= cap``).
    """
    new_id = f"wsu_{uuid4().hex[:24]}"
    if per_user_cap is None:
        sql = text("""
            INSERT INTO web_search_usage
                (id, tenant_id, user_id, year_month,
                 tavily_calls, fallback_calls, updated_at)
            SELECT :id, :t, :u, :ym, 1, 0, CURRENT_TIMESTAMP
            ON CONFLICT (tenant_id, user_id, year_month)
            DO UPDATE SET
                tavily_calls = web_search_usage.tavily_calls + 1,
                updated_at = CURRENT_TIMESTAMP
            RETURNING tavily_calls
        """)
        params = {"id": new_id, "t": tenant_id, "u": user_id, "ym": year_month}
    else:
        sql = text("""
            INSERT INTO web_search_usage
                (id, tenant_id, user_id, year_month,
                 tavily_calls, fallback_calls, updated_at)
            SELECT :id, :t, :u, :ym, 1, 0, CURRENT_TIMESTAMP
            WHERE 1 <= :cap
            ON CONFLICT (tenant_id, user_id, year_month)
            DO UPDATE SET
                tavily_calls = web_search_usage.tavily_calls + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE web_search_usage.tavily_calls + 1 <= :cap
            RETURNING tavily_calls
        """)
        params = {
            "id": new_id, "t": tenant_id, "u": user_id,
            "ym": year_month, "cap": per_user_cap,
        }
    return conn.execute(sql, params).first()


def release_tavily(
    engine: Engine,
    tenant_id: str,
    user_id: str,
    year_month: str | None = None,
) -> None:
    """Компенсирующий decrement (provider-fail после успешного резерва).

    Своя короткая ``engine.begin()``-txn, коммитит независимо. Floor at 0
    (``MAX(tavily_calls - 1, 0)``) — защитно. Идемпотентен в ветке
    provider-fail (вызывается ровно раз на неудачный Tavily).
    """
    if not tenant_id or not user_id:
        return
    ym = year_month or _current_year_month()
    is_pg = engine.dialect.name == "postgresql"
    floor_fn = "GREATEST" if is_pg else "MAX"
    with engine.begin() as conn:
        conn.execute(text(f"""
            UPDATE web_search_usage
            SET tavily_calls = {floor_fn}(tavily_calls - 1, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :t AND user_id = :u AND year_month = :ym
        """), {"t": tenant_id, "u": user_id, "ym": ym})


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
    """Per-юзер строка в админ-таблице.

    `user_remaining` тир-aware НЕ доступен в листинге без per-row тира
    (free=5, grandfathered/платные=без лимита). Чтобы не врать «30−calls»,
    оставляем None + помечаем «по тарифу» (Фаза 1 #200). Глобальный
    остаток 950 показываем как раньше через `admin_summary`.
    """

    tenant_id: str
    user_id: str
    tenant_name: str | None
    tavily_calls: int
    fallback_calls: int
    user_remaining: int | None  # None → показывать «по тарифу» (тир-aware)


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
                # Тир-aware остаток в листинге недоступен (free=5, иначе без
                # лимита) → None («по тарифу»), не врём «30−calls» (#200 Фаза 1).
                user_remaining=None,
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
