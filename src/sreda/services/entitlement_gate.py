"""EntitlementGate — central pre-handler subscription check.

Phase 2 of free-tier-subscription plan
(`plans/free-tier-subscription-stalled-r7.md`).

Hooks вызываются в начале каждого inbound handler (TG webhook, MAX
webhook, voice transcribe) ПОСЛЕ `ensure_*_user_bundle()` (что
auto-grants new tenants free tier sub) — but BEFORE business
handling. Returns (allowed, reason_or_plan_key):

  - (True, plan_key)         — handler proceeds; plan_key informs
                                tier-specific behavior (sreda_free
                                triggers quota enforcement).
  - (False, 'suspended')     — admin suspended; send copy "доступ
                                ограничен".
  - (False, 'no_active_*')   — corrupted state (should never
                                happen post-Phase 2). Send same.

Дизайн (per Codex R7 O1 fix — applied proactively in Phase 2):
- **Two-step query**: первый — active sub on housewife_assistant
  feature_key, второй (если active отсутствует) — suspended sub.
  Никакой ORDER BY ambiguity.
- **Filter by feature_key**, не tenant alone — future multi-feature
  юзеры не affect housewife gate.
- **Grandfathered detection** via `grandfathered_at IS NOT NULL` —
  caller distinguishes free vs grandfathered tier ВНЕ gate (plan_key
  + grandfathered_at flag together).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


HOUSEWIFE_FEATURE_KEY = "housewife_assistant"


@dataclass(frozen=True)
class GateResult:
    """Result of EntitlementGate.check.

    Attributes:
        allowed: True если tenant can use the assistant.
        reason: 'ok' если allowed; UPGRADE_COPY key если blocked.
        plan_key: plan_key of active sub (None if blocked).
        is_grandfathered: True если sub.grandfathered_at IS NOT NULL.
            Phase 2 quota gates skip enforcement для grandfathered.
    """
    allowed: bool
    reason: str
    plan_key: str | None = None
    is_grandfathered: bool = False


class EntitlementGate:
    """Central inbound check. Caller hooks at handler start ПОСЛЕ
    ensure_*_user_bundle. Suspended/no-active → reject with copy.
    """

    FEATURE_KEY = HOUSEWIFE_FEATURE_KEY

    def __init__(self, session: Session) -> None:
        self.session = session

    def check(self, tenant_id: str) -> GateResult:
        """Two-step query: prefer active, fallback к suspended."""
        # Step 1: active sub (max 1 per partial unique index 0042)
        row = self.session.execute(text("""
            SELECT sp.plan_key, ts.grandfathered_at
            FROM tenant_subscriptions ts
            JOIN subscription_plans sp ON ts.plan_id = sp.id
            WHERE ts.tenant_id = :t
              AND ts.feature_key = :f
              AND ts.status = 'active'
            LIMIT 1
        """), {"t": tenant_id, "f": self.FEATURE_KEY}).first()
        if row:
            return GateResult(
                allowed=True,
                reason="ok",
                plan_key=row.plan_key,
                is_grandfathered=row.grandfathered_at is not None,
            )

        # Step 2: any suspended sub for this feature?
        sus = self.session.execute(text("""
            SELECT sp.plan_key
            FROM tenant_subscriptions ts
            JOIN subscription_plans sp ON ts.plan_id = sp.id
            WHERE ts.tenant_id = :t
              AND ts.feature_key = :f
              AND ts.status = 'suspended'
            ORDER BY sp.sort_order ASC
            LIMIT 1
        """), {"t": tenant_id, "f": self.FEATURE_KEY}).first()
        if sus:
            return GateResult(allowed=False, reason="suspended")

        # No active, no suspended — corrupted state OR pre-Phase-2
        # auto-grant gap. Should be rare после Phase 2 deploy.
        logger.warning(
            "entitlement_gate: tenant=%s no active or suspended sub "
            "for feature=%s — bundle hook missed?",
            tenant_id, self.FEATURE_KEY,
        )
        return GateResult(allowed=False, reason="no_active_subscription")
