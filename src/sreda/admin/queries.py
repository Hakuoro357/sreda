"""Read-only queries for admin dashboard.

Intentionally separate from domain repositories — these are cross-cutting
admin views, not domain logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.core import Tenant, User
from sreda.db.models.skill_platform import SkillAIExecution
from sreda.db.models.user_profile import TenantUserProfile, TenantUserSkillConfig
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.services.budget import BudgetService
from sreda.services.housewife_onboarding import (
    HOUSEWIFE_FEATURE_KEY,
    WELCOME_V2_PROGRESS_KEY,
)


# ---------------------------------------------------------------- helpers


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    dt = _ensure_utc(dt)
    assert dt is not None
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- users page


@dataclass
class UserRow:
    tenant_id: str
    tenant_name: str
    user_id: str
    telegram_id: str | None
    max_account_id: str | None
    display_name: str | None
    timezone: str | None
    subscriptions: list[SubInfo]
    # None = pending approval (new user, admin hasn't clicked "Одобрить"
    # yet). Non-None = approved; admin UI hides the button in that case.
    approved_at: str | None = None
    is_pending: bool = False
    # Welcome v2 broadcast tour (2026-04-28) — статус прохождения
    # pending-цепочки 11 сообщений. "completed" = тапнул pb:done,
    # "in_progress" = есть started_at но нет completed_at, "not_started"
    # = ничего не записано в skill_params.welcome_v2_progress.
    welcome_v2_status: str = "not_started"


@dataclass
class SubInfo:
    plan_title: str
    feature_key: str
    status: str
    active_until: str


def get_all_users(session: Session) -> list[UserRow]:
    """All users with their profiles + subscriptions.

    Sort: newest tenant first (by ``tenants.created_at`` DESC).
    """
    tenant_rows = session.query(Tenant).all()
    tenants = {t.id: t.name for t in tenant_rows}
    # Keep approved_at handy for the "Одобрить" button on /admin/users.
    tenants_approved = {t.id: t.approved_at for t in tenant_rows}
    # Sort key: newest tenants float to the top of the table.
    tenants_created_at: dict[str, datetime | None] = {
        t.id: _ensure_utc(t.created_at) for t in tenant_rows
    }
    users = session.query(User).all()

    # Bulk-load related data keyed by tenant_id
    profiles_by_key: dict[tuple[str, str], TenantUserProfile] = {}
    for p in session.query(TenantUserProfile).all():
        profiles_by_key[(p.tenant_id, p.user_id)] = p

    subs_by_tenant: dict[str, list[SubInfo]] = {}
    sub_rows = (
        session.query(TenantSubscription, SubscriptionPlan)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .all()
    )
    for sub, plan in sub_rows:
        info = SubInfo(
            plan_title=plan.title,
            feature_key=plan.feature_key,
            status=sub.status,
            active_until=_fmt_dt(sub.active_until),
        )
        subs_by_tenant.setdefault(sub.tenant_id, []).append(info)

    # Welcome v2 broadcast tour прогресс — читаем из housewife
    # skill_params_json. Bulk: одним запросом по всем (tenant, user).
    welcome_status_by_key: dict[tuple[str, str], str] = {}
    for cfg in (
        session.query(TenantUserSkillConfig)
        .filter(TenantUserSkillConfig.feature_key == HOUSEWIFE_FEATURE_KEY)
        .all()
    ):
        params = UserProfileRepository.decode_skill_params(cfg)
        progress = params.get(WELCOME_V2_PROGRESS_KEY) or {}
        if not isinstance(progress, dict):
            continue
        if progress.get("completed_at"):
            status = "completed"
        elif progress.get("started_at"):
            status = "in_progress"
        else:
            status = "not_started"
        welcome_status_by_key[(cfg.tenant_id, cfg.user_id)] = status

    result: list[UserRow] = []
    for u in users:
        profile = profiles_by_key.get((u.tenant_id, u.id))
        approved_at = tenants_approved.get(u.tenant_id)
        welcome_v2_status = welcome_status_by_key.get(
            (u.tenant_id, u.id), "not_started"
        )
        result.append(
            UserRow(
                tenant_id=u.tenant_id,
                tenant_name=tenants.get(u.tenant_id, u.tenant_id),
                user_id=u.id,
                telegram_id=u.telegram_account_id,
                max_account_id=u.max_account_id,
                display_name=profile.display_name if profile else None,
                timezone=profile.timezone if profile else None,
                subscriptions=subs_by_tenant.get(u.tenant_id, []),
                approved_at=_fmt_dt(approved_at),
                is_pending=approved_at is None,
                welcome_v2_status=welcome_v2_status,
            )
        )
    # Newest tenants first. None goes to the end.
    _SENTINEL_OLD = datetime.min.replace(tzinfo=timezone.utc)
    result.sort(
        key=lambda r: tenants_created_at.get(r.tenant_id) or _SENTINEL_OLD,
        reverse=True,
    )
    return result


# ---------------------------------------------------------------- budget page


@dataclass
class BudgetRow:
    tenant_id: str
    tenant_name: str
    feature_key: str
    plan_title: str | None
    credits_used: int
    credits_quota: int | None
    usage_pct: float | None
    total_calls: int
    total_tokens: int
    period_start: str
    period_end: str
    last_used_at: str


def get_budget_summary(session: Session) -> list[BudgetRow]:
    """Aggregate budget per (tenant, feature_key) using active subscriptions.

    Sort: most recent activity first (MAX(skill_ai_executions.created_at)
    DESC). Subs with no activity in the current period sink to the bottom.
    """
    tenants = {t.id: t.name for t in session.query(Tenant).all()}
    budget_svc = BudgetService(session)

    # Find all distinct (tenant_id, feature_key) pairs with active subs
    active_subs = (
        session.query(TenantSubscription, SubscriptionPlan)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .filter(TenantSubscription.status == "active")
        .all()
    )

    rows_with_sort: list[tuple[datetime | None, BudgetRow]] = []
    for sub, plan in active_subs:
        quota = budget_svc.get_quota_status(sub.tenant_id, plan.feature_key)

        # Aggregate calls + tokens + last-used for the period
        q = session.query(
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(SkillAIExecution.total_tokens), 0),
            func.max(SkillAIExecution.created_at),
        ).filter(
            SkillAIExecution.tenant_id == sub.tenant_id,
            SkillAIExecution.feature_key == plan.feature_key,
        )
        if quota.period_start:
            q = q.filter(SkillAIExecution.created_at >= quota.period_start)
        if quota.period_end:
            q = q.filter(SkillAIExecution.created_at <= quota.period_end)
        total_calls, total_tokens, last_used_dt = q.one()
        last_used_dt = _ensure_utc(last_used_dt)

        usage_pct = None
        if quota.credits_quota and quota.credits_quota > 0:
            usage_pct = round(quota.credits_used / quota.credits_quota * 100, 1)

        rows_with_sort.append((
            last_used_dt,
            BudgetRow(
                tenant_id=sub.tenant_id,
                tenant_name=tenants.get(sub.tenant_id, sub.tenant_id),
                feature_key=plan.feature_key,
                plan_title=plan.title,
                credits_used=quota.credits_used,
                credits_quota=quota.credits_quota,
                usage_pct=usage_pct,
                total_calls=int(total_calls),
                total_tokens=int(total_tokens),
                period_start=_fmt_dt(quota.period_start),
                period_end=_fmt_dt(quota.period_end),
                last_used_at=_fmt_dt(last_used_dt),
            ),
        ))
    # Most-recently-used first. None (no activity in period) sinks last.
    _SENTINEL_OLD = datetime.min.replace(tzinfo=timezone.utc)
    rows_with_sort.sort(key=lambda x: x[0] or _SENTINEL_OLD, reverse=True)
    return [r for _, r in rows_with_sort]


# ---- per-day budget view (admin /budget?date=YYYY-MM-DD) -------------------

MSK_TZ = ZoneInfo("Europe/Moscow")


def _msk_day_window_utc(for_date: date) -> tuple[datetime, datetime]:
    """Convert an MSK calendar date to ``[start_utc, end_utc)`` window.

    Used by ``get_budget_summary_for_day`` and tests. Returns naive-free
    UTC datetimes so SQLAlchemy comparisons with ``created_at`` (stored
    UTC, tz-aware in models) work identically.
    """
    start_msk = datetime.combine(for_date, time.min, tzinfo=MSK_TZ)
    end_msk = start_msk + timedelta(days=1)
    return start_msk.astimezone(timezone.utc), end_msk.astimezone(timezone.utc)


def get_budget_summary_for_day(
    session: Session, for_date: date
) -> list[BudgetRow]:
    """Per-day budget aggregate for the admin /budget page.

    Aggregates ``SkillAIExecution`` rows whose ``created_at`` falls into
    the Europe/Moscow calendar day ``for_date`` — i.e. UTC window
    ``[for_date 00:00 MSK, for_date+1 00:00 MSK)``. Subscriptions are
    iterated the same way ``get_budget_summary`` does (one row per
    active sub) so the table layout stays identical; rows with zero
    activity for the day are still surfaced so the admin can see "no
    consumption today" rather than an empty page when the user simply
    hadn't talked to the bot that day.

    ``credits_used`` here is **day-scoped** (not the full month). The
    existing monthly ``BudgetService.get_quota_status`` is *not* called
    because its window is the subscription period, not the day; daily
    consumption is summed directly from ``credits_consumed``. The
    monthly quota cap is still rendered so the admin can eyeball
    "today's burn vs the monthly cap".
    """
    tenants = {t.id: t.name for t in session.query(Tenant).all()}

    # Pre-fetch plan quotas once. Cheap, dedup by (tenant_id, feature_key).
    active_subs = (
        session.query(TenantSubscription, SubscriptionPlan)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .filter(TenantSubscription.status == "active")
        .all()
    )

    day_start_utc, day_end_utc = _msk_day_window_utc(for_date)

    rows_with_sort: list[tuple[datetime | None, BudgetRow]] = []
    for sub, plan in active_subs:
        q = session.query(
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(SkillAIExecution.total_tokens), 0),
            func.coalesce(func.sum(SkillAIExecution.credits_consumed), 0),
            func.max(SkillAIExecution.created_at),
        ).filter(
            SkillAIExecution.tenant_id == sub.tenant_id,
            SkillAIExecution.feature_key == plan.feature_key,
            SkillAIExecution.created_at >= day_start_utc,
            SkillAIExecution.created_at < day_end_utc,
        )
        total_calls, total_tokens, credits_used, last_used_dt = q.one()
        # Skip subscriptions with no consumption that day — page becomes
        # a noise wall otherwise (Boris feedback 2026-05-25 after first
        # /admin/budget render showed N empty rows).
        if int(total_calls) == 0:
            continue
        last_used_dt = _ensure_utc(last_used_dt)

        usage_pct: float | None = None
        if plan.credits_monthly_quota and plan.credits_monthly_quota > 0:
            # Day-share-of-monthly: helpful for "burning quota too fast?"
            # awareness. May exceed 100% on a hot day — capped only in
            # the bar width, raw % is reported as-is.
            usage_pct = round(
                int(credits_used) / plan.credits_monthly_quota * 100, 1
            )

        rows_with_sort.append((
            last_used_dt,
            BudgetRow(
                tenant_id=sub.tenant_id,
                tenant_name=tenants.get(sub.tenant_id, sub.tenant_id),
                feature_key=plan.feature_key,
                plan_title=plan.title,
                credits_used=int(credits_used),
                credits_quota=plan.credits_monthly_quota,
                usage_pct=usage_pct,
                total_calls=int(total_calls),
                total_tokens=int(total_tokens),
                period_start=_fmt_dt(day_start_utc),
                period_end=_fmt_dt(day_end_utc),
                last_used_at=_fmt_dt(last_used_dt),
            ),
        ))
    _SENTINEL_OLD = datetime.min.replace(tzinfo=timezone.utc)
    rows_with_sort.sort(key=lambda x: x[0] or _SENTINEL_OLD, reverse=True)
    return [r for _, r in rows_with_sort]


# ----------------------------------------------------------- llm calls page


@dataclass
class LLMCallRow:
    id: str
    created_at: str
    model: str | None
    task_type: str | None
    status: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    credits_consumed: int
    latency_ms: int | None
    run_id: str | None


@dataclass
class LLMCallsPage:
    rows: list[LLMCallRow]
    total: int
    page: int
    per_page: int
    total_pages: int
    tenant_name: str
    feature_key: str


def get_llm_calls(
    session: Session,
    tenant_id: str,
    feature_key: str,
    page: int = 1,
    per_page: int = 50,
) -> LLMCallsPage:
    """Paginated skill_ai_executions for a tenant + feature."""
    tenants = {t.id: t.name for t in session.query(Tenant).all()}

    base = session.query(SkillAIExecution).filter(
        SkillAIExecution.tenant_id == tenant_id,
        SkillAIExecution.feature_key == feature_key,
    )
    total = base.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    execs = (
        base.order_by(SkillAIExecution.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    rows = [
        LLMCallRow(
            id=e.id,
            created_at=_fmt_dt(e.created_at),
            model=e.model,
            task_type=e.task_type,
            status=e.status,
            finish_reason=e.finish_reason,
            prompt_tokens=e.prompt_tokens,
            completion_tokens=e.completion_tokens,
            total_tokens=e.total_tokens,
            credits_consumed=e.credits_consumed,
            latency_ms=e.latency_ms,
            run_id=e.run_id,
        )
        for e in execs
    ]

    return LLMCallsPage(
        rows=rows,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        tenant_name=tenants.get(tenant_id, tenant_id),
        feature_key=feature_key,
    )
