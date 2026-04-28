"""Read-only queries for admin dashboard.

Intentionally separate from domain repositories — these are cross-cutting
admin views, not domain logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.connect import TenantEDSAccount
from sreda.db.models.core import Tenant, User
from sreda.db.models.skill_platform import SkillAIExecution, TenantSkillState
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
    display_name: str | None
    timezone: str | None
    subscriptions: list[SubInfo]
    eds_accounts: list[EDSInfo]
    skill_states: list[SkillInfo]
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


@dataclass
class EDSInfo:
    login_masked: str
    status: str
    last_poll_at: str
    last_error: str | None


@dataclass
class SkillInfo:
    feature_key: str
    lifecycle_status: str
    health_status: str
    last_successful_run_at: str


def get_all_users(session: Session) -> list[UserRow]:
    """All users with their profiles, subscriptions, EDS accounts, skills."""
    tenant_rows = session.query(Tenant).all()
    tenants = {t.id: t.name for t in tenant_rows}
    # Keep approved_at handy for the "Одобрить" button on /admin/users.
    tenants_approved = {t.id: t.approved_at for t in tenant_rows}
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

    eds_by_tenant: dict[str, list[EDSInfo]] = {}
    for acc in session.query(TenantEDSAccount).all():
        info = EDSInfo(
            login_masked=acc.login_masked or "—",
            status=acc.status,
            last_poll_at=_fmt_dt(acc.last_poll_at),
            last_error=acc.last_error_code,
        )
        eds_by_tenant.setdefault(acc.tenant_id, []).append(info)

    skills_by_tenant: dict[str, list[SkillInfo]] = {}
    for sk in session.query(TenantSkillState).all():
        info = SkillInfo(
            feature_key=sk.feature_key,
            lifecycle_status=sk.lifecycle_status,
            health_status=sk.health_status,
            last_successful_run_at=_fmt_dt(sk.last_successful_run_at),
        )
        skills_by_tenant.setdefault(sk.tenant_id, []).append(info)

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
                display_name=profile.display_name if profile else None,
                timezone=profile.timezone if profile else None,
                subscriptions=subs_by_tenant.get(u.tenant_id, []),
                eds_accounts=eds_by_tenant.get(u.tenant_id, []),
                skill_states=skills_by_tenant.get(u.tenant_id, []),
                approved_at=_fmt_dt(approved_at),
                is_pending=approved_at is None,
                welcome_v2_status=welcome_v2_status,
            )
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


def get_budget_summary(session: Session) -> list[BudgetRow]:
    """Aggregate budget per (tenant, feature_key) using active subscriptions."""
    tenants = {t.id: t.name for t in session.query(Tenant).all()}
    budget_svc = BudgetService(session)

    # Find all distinct (tenant_id, feature_key) pairs with active subs
    active_subs = (
        session.query(TenantSubscription, SubscriptionPlan)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .filter(TenantSubscription.status == "active")
        .all()
    )

    rows: list[BudgetRow] = []
    for sub, plan in active_subs:
        quota = budget_svc.get_quota_status(sub.tenant_id, plan.feature_key)

        # Aggregate calls + tokens for the period
        q = session.query(
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(SkillAIExecution.total_tokens), 0),
        ).filter(
            SkillAIExecution.tenant_id == sub.tenant_id,
            SkillAIExecution.feature_key == plan.feature_key,
        )
        if quota.period_start:
            q = q.filter(SkillAIExecution.created_at >= quota.period_start)
        if quota.period_end:
            q = q.filter(SkillAIExecution.created_at <= quota.period_end)
        total_calls, total_tokens = q.one()

        usage_pct = None
        if quota.credits_quota and quota.credits_quota > 0:
            usage_pct = round(quota.credits_used / quota.credits_quota * 100, 1)

        rows.append(
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
            )
        )
    return rows


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
