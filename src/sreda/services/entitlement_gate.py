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
from datetime import datetime, timezone

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
        # Step 1: active sub (max 1 per partial unique index 0042).
        # 2026-07-18 audit fix: окно подписки проверяется ЯВНО — раньше гейт
        # смотрел только status='active', и подписка с истёкшим active_until
        # давала доступ бессрочно (свипера истёкших в системе нет — см.
        # BillingService.sweep_expired_subscriptions). Контракт окна =
        # billing._is_subscription_active: quantity>0 И active_until в будущем;
        # active_until IS NULL = бессрочная (auto-grant sreda_free,
        # onboarding.py). Сравнение — в Python: SQLite хранит naive-строки,
        # сырое SQL-сравнение с aware-datetime было бы диалект-зависимым.
        row = self.session.execute(text("""
            SELECT sp.plan_key, ts.grandfathered_at, ts.active_until, ts.quantity
            FROM tenant_subscriptions ts
            JOIN subscription_plans sp ON ts.plan_id = sp.id
            WHERE ts.tenant_id = :t
              AND ts.feature_key = :f
              AND ts.status = 'active'
            LIMIT 1
        """), {"t": tenant_id, "f": self.FEATURE_KEY}).first()
        if row and self._within_active_window(row.active_until, row.quantity):
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

    @staticmethod
    def _within_active_window(active_until: datetime | None, quantity: int | None) -> bool:
        """Окно действия подписки (контракт billing._is_subscription_active).

        quantity<=0 — подписка фактически выключена. active_until IS NULL —
        бессрочная (auto-grant бесплатных планов). Истёкший active_until —
        не активна, даже если status='active' (свипер мог ещё не дойти).
        """
        if not quantity or quantity <= 0:
            return False
        if active_until is None:
            return True
        if isinstance(active_until, str):
            # Сырой text()-SQL: SQLite отдаёт DATETIME строкой («YYYY-MM-DD
            # HH:MM:SS.ffffff»), PG — datetime'ом. Парсим дефенсивно.
            active_until = datetime.fromisoformat(active_until)
        if active_until.tzinfo is None:
            # SQLite возвращает naive — хранимое значение UTC.
            active_until = active_until.replace(tzinfo=timezone.utc)
        return active_until > datetime.now(timezone.utc)
