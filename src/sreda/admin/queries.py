"""Read-only queries for admin dashboard.

Intentionally separate from domain repositories — these are cross-cutting
admin views, not domain logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from sreda.services.llm_pricing import cost_estimate

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
    # Дата регистрации = tenants.created_at (момент создания аккаунта).
    # Formatted UTC через _fmt_dt (как approved_at); None → "—".
    registered_at: str | None = None


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
                registered_at=_fmt_dt(tenants_created_at.get(u.tenant_id)),
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
    # #150: ≈стоимость USD (priced subtotal по моделям строки) + покрытие.
    # None — нет ни одной priced-модели в строке (mixed обрабатывается покрытием).
    est_usd: Decimal | None = None
    cost_coverage_pct: int | None = None


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

    # #150: карта стоимости за день, ОДНИМ запросом по
    # (tenant, feature, provider, model) — без N+1 на строку бюджета.
    cost_map: dict[tuple[str, str], list[tuple[str, str, int, int, int]]] = {}
    for tid, fkey, pk, model, c_calls, c_prompt, c_completion in (
        session.query(
            SkillAIExecution.tenant_id,
            SkillAIExecution.feature_key,
            SkillAIExecution.provider_key,
            SkillAIExecution.model,
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(SkillAIExecution.prompt_tokens), 0),
            func.coalesce(func.sum(SkillAIExecution.completion_tokens), 0),
        )
        .filter(
            SkillAIExecution.created_at >= day_start_utc,
            SkillAIExecution.created_at < day_end_utc,
            _VALID_TOKENS,
        )
        .group_by(
            SkillAIExecution.tenant_id, SkillAIExecution.feature_key,
            SkillAIExecution.provider_key, SkillAIExecution.model,
        )
        .all()
    ):
        cost_map.setdefault((tid, fkey), []).append(
            (pk or "", model or "", int(c_calls), int(c_prompt), int(c_completion))
        )

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

        # #150: ≈стоимость строки = priced subtotal по её (provider, model).
        _est = Decimal("0")
        _has_priced = False
        _priced_calls = _row_calls = 0
        for _pk, _model, _c, _p, _comp in cost_map.get(
            (sub.tenant_id, plan.feature_key), []
        ):
            _row_calls += _c
            _ce = cost_estimate(_pk, _model, prompt_tokens=_p, completion_tokens=_comp)
            if _ce is not None:
                _est += _ce.est_usd
                _has_priced = True
                _priced_calls += _c
        _est_usd = _est if _has_priced else None
        # Знаменатель покрытия — ВАЛИДНЫЕ вызовы строки (_VALID_TOKENS), а не
        # показанный total_calls (тот без фильтра аномалий): «доля valid с ценой».
        _cost_cov = round(_priced_calls * 100 / _row_calls) if _row_calls else None

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
                est_usd=_est_usd,
                cost_coverage_pct=_cost_cov,
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


# ------------------------------------------------ #150 F2: траты по моделям

# Валидная строка для денег: оба счётчика токенов неотрицательны. Отрицательные
# — аномалия данных, исключаются из сумм И считаются отдельно (Codex-ревью:
# нельзя суммировать в группе — отрицательная и положительная взаимогасятся).
_VALID_TOKENS = (
    SkillAIExecution.prompt_tokens.isnot(None)
    & SkillAIExecution.completion_tokens.isnot(None)
    & (SkillAIExecution.prompt_tokens >= 0)
    & (SkillAIExecution.completion_tokens >= 0)
    # incomplete split: 0/0 при total>0 (legacy «только total_tokens») — НЕ valid
    # (иначе priced-модель даст ложный $0, занизит spend) → попадёт в аномалии.
    & ~(
        (SkillAIExecution.prompt_tokens == 0)
        & (SkillAIExecution.completion_tokens == 0)
        & (func.coalesce(SkillAIExecution.total_tokens, 0) > 0)
    )
)


def period_window_utc(
    period: str, anchor: datetime | None = None
) -> tuple[datetime, datetime]:
    """MSK-календарное окно периода → ``[start_utc, end_utc)`` (aware-UTC).

    ``period`` ∈ {day, week, month}, считается в Europe/Moscow: день — с 00:00;
    неделя — с понедельника 00:00; месяц — с 1-го 00:00 (след. месяц —
    календарной арифметикой). Границы aware-UTC, полуоткрытые — как
    ``_msk_day_window_utc`` (сравнение с ``created_at`` идентично SQLite/Postgres).
    """
    now = anchor or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(MSK_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start_local, end_local = midnight, midnight + timedelta(days=1)
    elif period == "week":
        start_local = midnight - timedelta(days=local.weekday())
        end_local = start_local + timedelta(days=7)
    elif period == "month":
        start_local = midnight.replace(day=1)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    else:
        raise ValueError(f"unknown period: {period!r} (day|week|month)")
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


@dataclass(frozen=True)
class SpendModelRow:
    provider_key: str
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    est_usd: Decimal | None   # None если беспрайсово (→ «—» на UI)
    upper_usd: Decimal | None
    priced: bool


@dataclass(frozen=True)
class SpendReport:
    period: str
    start_utc: datetime
    end_utc: datetime
    rows: list[SpendModelRow]            # priced (по est убыв.), затем unpriced
    priced_subtotal_usd: Decimal
    upper_subtotal_usd: Decimal
    unpriced_models: list[tuple[str, str]]
    unpriced_calls: int
    unpriced_tokens: int
    coverage_calls_pct: int | None       # None если 0 валидных вызовов
    coverage_tokens_pct: int | None
    anomaly_count: int


def get_spend_by_model(
    session: Session,
    period: str,
    anchor: datetime | None = None,
    tenant_id: str | None = None,
) -> SpendReport:
    """Траты по (provider_key, model) за MSK-окно периода (опц. по тенанту).

    Стоимость — через ``llm_pricing.cost_estimate`` (беспрайсовое → est=None → «—»).
    Итог — «priced subtotal» + покрытие (% валидных вызовов/токенов с известной
    ценой) + число аномалий. Историю считаем по текущему прайсу (оценка).
    """
    start, end = period_window_utc(period, anchor)
    in_win_list = [
        SkillAIExecution.created_at >= start,
        SkillAIExecution.created_at < end,
    ]
    if tenant_id is not None:
        in_win_list.append(SkillAIExecution.tenant_id == tenant_id)
    in_win = tuple(in_win_list)

    anomaly_count = (
        session.query(func.count(SkillAIExecution.id))
        .filter(*in_win, ~_VALID_TOKENS)
        .scalar()
    ) or 0

    grouped = (
        session.query(
            SkillAIExecution.provider_key,
            SkillAIExecution.model,
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(SkillAIExecution.prompt_tokens), 0),
            func.coalesce(func.sum(SkillAIExecution.completion_tokens), 0),
        )
        .filter(*in_win, _VALID_TOKENS)
        .group_by(SkillAIExecution.provider_key, SkillAIExecution.model)
        .all()
    )

    rows: list[SpendModelRow] = []
    priced_subtotal = Decimal("0")
    upper_subtotal = Decimal("0")
    unpriced_models: list[tuple[str, str]] = []
    unpriced_calls = unpriced_tokens = 0
    total_calls = total_tokens = 0
    priced_calls = priced_tokens = 0

    for provider_key, model, calls, prompt, completion in grouped:
        provider_key = provider_key or ""
        model = model or ""
        calls = int(calls)
        prompt = int(prompt)
        completion = int(completion)
        toks = prompt + completion
        total_calls += calls
        total_tokens += toks
        est = cost_estimate(
            provider_key, model,
            prompt_tokens=prompt, completion_tokens=completion,
        )
        if est is not None:
            priced_subtotal += est.est_usd
            upper_subtotal += est.upper_usd
            priced_calls += calls
            priced_tokens += toks
            rows.append(SpendModelRow(
                provider_key, model, calls, prompt, completion,
                est.est_usd, est.upper_usd, True,
            ))
        else:
            unpriced_models.append((provider_key, model))
            unpriced_calls += calls
            unpriced_tokens += toks
            rows.append(SpendModelRow(
                provider_key, model, calls, prompt, completion, None, None, False,
            ))

    # priced (по est убыв.) сперва, беспрайсовые — в конец
    rows.sort(key=lambda r: (r.priced, r.est_usd or Decimal("0")), reverse=True)

    cov_calls = round(priced_calls * 100 / total_calls) if total_calls else None
    cov_tokens = round(priced_tokens * 100 / total_tokens) if total_tokens else None

    return SpendReport(
        period=period, start_utc=start, end_utc=end, rows=rows,
        priced_subtotal_usd=priced_subtotal, upper_subtotal_usd=upper_subtotal,
        unpriced_models=unpriced_models, unpriced_calls=unpriced_calls,
        unpriced_tokens=unpriced_tokens, coverage_calls_pct=cov_calls,
        coverage_tokens_pct=cov_tokens, anomaly_count=int(anomaly_count),
    )


@dataclass(frozen=True)
class TenantSpendDetail:
    tenant_id: str
    tenant_name: str
    day: SpendReport
    week: SpendReport
    month: SpendReport


def get_tenant_spend_detail(
    session: Session, tenant_id: str, anchor: datetime | None = None
) -> TenantSpendDetail:
    """Траты ОДНОГО тенанта за день/неделю/месяц (карточка). UI: «расходы
    тенанта/аккаунта, не пользователя» (у тенанта может быть несколько юзеров)."""
    name = (
        session.query(Tenant.name).filter(Tenant.id == tenant_id).scalar()
        or tenant_id
    )
    return TenantSpendDetail(
        tenant_id=tenant_id,
        tenant_name=name,
        day=get_spend_by_model(session, "day", anchor, tenant_id=tenant_id),
        week=get_spend_by_model(session, "week", anchor, tenant_id=tenant_id),
        month=get_spend_by_model(session, "month", anchor, tenant_id=tenant_id),
    )


@dataclass(frozen=True)
class UsersPage:
    rows: list[UserRow]
    page: int
    per_page: int
    total: int
    total_pages: int


def get_users_page(
    session: Session, page: int = 1, per_page: int = 50
) -> UsersPage:
    """Пагинация над get_all_users (полный список мал — внутренние + альфа
    200-300; per-page bulk-load — оптимизация на будущее при росте базы)."""
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    all_rows = get_all_users(session)
    total = len(all_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    return UsersPage(
        rows=all_rows[start:start + per_page],
        page=page, per_page=per_page, total=total, total_pages=total_pages,
    )


# -------------------------------------------------- #150 dashboard aggregates


def get_cost_volume_summary(
    session: Session, anchor: datetime | None = None
) -> dict[str, SpendReport]:
    """Затраты+объём за день/неделю/месяц (все тенанты). Переиспользует
    get_spend_by_model → каждый отчёт несёт priced subtotal + покрытие."""
    return {
        p: get_spend_by_model(session, p, anchor) for p in ("day", "week", "month")
    }


@dataclass(frozen=True)
class TopTenants:
    # (tenant_id, tenant_name, est_usd, calls) — по priced subtotal убыв.
    by_spend: list[tuple[str, str, Decimal, int]]
    # (tenant_id, tenant_name, unpriced_tokens) — слепая зона: тяжёлые
    # беспрайсовые тенанты, которых нет в by_spend (Codex R2 high).
    by_unpriced: list[tuple[str, str, int]]


def get_top_tenants_by_spend(
    session: Session, period: str = "month", limit: int = 10,
    anchor: datetime | None = None,
) -> TopTenants:
    start, end = period_window_utc(period, anchor)
    grouped = (
        session.query(
            SkillAIExecution.tenant_id,
            SkillAIExecution.provider_key,
            SkillAIExecution.model,
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(SkillAIExecution.prompt_tokens), 0),
            func.coalesce(func.sum(SkillAIExecution.completion_tokens), 0),
        )
        .filter(
            SkillAIExecution.created_at >= start,
            SkillAIExecution.created_at < end,
            _VALID_TOKENS,
        )
        .group_by(
            SkillAIExecution.tenant_id, SkillAIExecution.provider_key,
            SkillAIExecution.model,
        )
        .all()
    )
    est_by_tenant: dict[str, Decimal] = {}
    calls_by_tenant: dict[str, int] = {}
    unpriced_tok_by_tenant: dict[str, int] = {}
    for tid, pk, model, calls, prompt, completion in grouped:
        tid = tid or "—"
        ce = cost_estimate(pk or "", model or "", prompt_tokens=int(prompt),
                           completion_tokens=int(completion))
        if ce is not None:
            est_by_tenant[tid] = est_by_tenant.get(tid, Decimal("0")) + ce.est_usd
            calls_by_tenant[tid] = calls_by_tenant.get(tid, 0) + int(calls)
        else:
            unpriced_tok_by_tenant[tid] = (
                unpriced_tok_by_tenant.get(tid, 0) + int(prompt) + int(completion)
            )
    names = {t.id: t.name for t in session.query(Tenant).all()}
    by_spend = sorted(
        ((tid, names.get(tid, tid), est, calls_by_tenant.get(tid, 0))
         for tid, est in est_by_tenant.items()),
        key=lambda x: x[2], reverse=True,
    )[:limit]
    by_unpriced = sorted(
        ((tid, names.get(tid, tid), tok)
         for tid, tok in unpriced_tok_by_tenant.items()),
        key=lambda x: x[2], reverse=True,
    )[:limit]
    return TopTenants(by_spend=by_spend, by_unpriced=by_unpriced)


def get_dialogue_health(session: Session):
    """Здоровье диалога за окно (gather_day_counts). best-effort — при сбое
    None (UI покажет «—»). breakdowns_precounted=0: без glob-логов в вебе."""
    try:
        from sreda.workers.reliability_report import gather_day_counts

        return gather_day_counts(
            session, now=datetime.now(timezone.utc), breakdowns_precounted=0,
        )
    except Exception:  # noqa: BLE001 — секция дашборда не валит страницу
        return None
