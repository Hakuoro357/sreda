"""Per-skill LLM budget tracking + enforcement (Phase 4.5).

One service, three responsibilities:

  1. ``get_quota_status(tenant_id, feature_key)`` — check whether the
     tenant has a live subscription for this feature with credits left
     in the current billing period.
  2. ``record_llm_usage(...)`` — persist one LLM call into
     ``skill_ai_executions`` with model + tokens + computed credits.
  3. Small helpers for ``/stats`` and quota-exhausted UX.

Billing period is ``TenantSubscription.starts_at`` →
``TenantSubscription.active_until`` of the ACTIVE subscription for the
given ``feature_key``. No subscription → no quota (skill is off for
this tenant). Plan with NULL quota → unmetered (returned as
``remaining=None``, ``is_exhausted=False``).

Design notes:
  * Plans are attached by ``SubscriptionPlan.feature_key``, so a tenant
    can subscribe to multiple skills independently.
  * Race tolerance: we check quota once at turn start and then let the
    tool-call loop run. A heavy turn can overshoot by 1-2 LLM calls —
    acceptable UX (don't want to cut a response mid-tool-loop).
  * Unknown model in the rate formula falls back to the pessimistic
    2x rate — usage counter over-estimates rather than under-estimates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.skill_platform import SkillAIExecution
from sreda.services.credit_formula import credits_for


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """Per-feature quota snapshot for a tenant.

    ``is_subscribed`` — does the tenant have an active subscription?
    ``is_exhausted`` — did they hit the quota in the current period?
    ``credits_used`` / ``credits_quota`` — for UX (e.g. ``/stats``).
      ``credits_quota=None`` means "unmetered plan"; ``credits_used``
      is still populated for observability.
    ``period_start`` / ``period_end`` — current billing window; useful
      for "next reset on X" UX.
    """

    feature_key: str
    is_subscribed: bool
    is_exhausted: bool
    credits_used: int
    credits_quota: int | None
    period_start: datetime | None
    period_end: datetime | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """SQLite strips tzinfo on roundtrip; be defensive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class BudgetService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- quota

    def get_quota_status(self, tenant_id: str, feature_key: str) -> QuotaStatus:
        """Resolve active subscription → plan → usage for a feature."""
        sub, plan = self._active_subscription(tenant_id, feature_key)
        if sub is None or plan is None:
            return QuotaStatus(
                feature_key=feature_key,
                is_subscribed=False,
                is_exhausted=True,  # no sub = functionally exhausted
                credits_used=0,
                credits_quota=None,
                period_start=None,
                period_end=None,
            )

        period_start = _ensure_utc(sub.starts_at) or _utcnow()
        period_end = _ensure_utc(sub.active_until)
        used = self._sum_credits(tenant_id, feature_key, period_start, period_end)
        quota = plan.credits_monthly_quota

        if quota is None:
            exhausted = False  # unmetered plan
        else:
            exhausted = used >= quota

        return QuotaStatus(
            feature_key=feature_key,
            is_subscribed=True,
            is_exhausted=exhausted,
            credits_used=used,
            credits_quota=quota,
            period_start=period_start,
            period_end=period_end,
        )

    def has_quota(self, tenant_id: str, feature_key: str) -> bool:
        """Shortcut: subscribed AND not exhausted."""
        status = self.get_quota_status(tenant_id, feature_key)
        return status.is_subscribed and not status.is_exhausted

    # -------------------------------------------------------------- record

    def record_llm_usage(
        self,
        *,
        tenant_id: str,
        feature_key: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        run_id: str,
        attempt_id: str | None = None,
        task_type: str | None = None,
        structured_output_json: str | None = None,
        status: str = "succeeded",
    ) -> int:
        """Write a ``skill_ai_executions`` row and return credits_consumed."""
        credits = credits_for(model, prompt_tokens, completion_tokens)
        now = _utcnow()
        row = SkillAIExecution(
            id=f"skai_{_short_uuid()}",
            run_id=run_id,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            feature_key=feature_key,
            task_type=task_type or "llm_call",
            provider_key="mimo",
            model=model,
            ai_schema_version=1,
            status=status,
            structured_output_json=structured_output_json,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            credits_consumed=credits,
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        self.session.add(row)
        self.session.flush()
        return credits

    # -------------------------------------------------------------- helpers

    def _active_subscription(
        self, tenant_id: str, feature_key: str
    ) -> tuple[TenantSubscription | None, SubscriptionPlan | None]:
        """Return the active sub + plan for this feature, or (None, None)."""
        now = _utcnow()
        query = (
            self.session.query(TenantSubscription, SubscriptionPlan)
            .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
            .filter(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.status == "active",
                SubscriptionPlan.feature_key == feature_key,
            )
        )
        # Prefer subscriptions that are currently within their active
        # window. SQLite comparisons handle naive vs aware datetimes
        # inconsistently, so we hold off on filter here and walk the
        # list in Python.
        rows = query.all()
        for sub, plan in rows:
            period_end = _ensure_utc(sub.active_until)
            if period_end is None or period_end > now:
                return sub, plan
        return None, None

    def _sum_credits(
        self,
        tenant_id: str,
        feature_key: str,
        period_start: datetime,
        period_end: datetime | None,
    ) -> int:
        query = self.session.query(
            func.coalesce(func.sum(SkillAIExecution.credits_consumed), 0)
        ).filter(
            SkillAIExecution.tenant_id == tenant_id,
            SkillAIExecution.feature_key == feature_key,
            SkillAIExecution.created_at >= period_start,
        )
        if period_end is not None:
            query = query.filter(SkillAIExecution.created_at <= period_end)
        result = query.scalar()
        return int(result or 0)


def _short_uuid() -> str:
    from uuid import uuid4

    return uuid4().hex[:24]
