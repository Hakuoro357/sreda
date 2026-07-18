"""Agent capability lookups — answers "does this tenant have X?".

Capabilities are not standalone subscriptions; they live on the agent
manifest (``SkillManifestBase``). The tenant gets a capability if any
of their active subscriptions is tied to an agent whose manifest
declares the capability.

Current capabilities:
- ``includes_voice`` — inbound voice messages auto-transcribed

When a new shared capability is added (e.g. ``includes_contacts``),
just add a field to the manifest and a helper here. The voice gate in
``services.telegram_bot`` is the reference consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.features.app_registry import get_feature_registry


@dataclass(frozen=True, slots=True)
class VoiceAccessResult:
    """Resolution of a tenant's voice entitlement + billing attribution.

    ``allowed`` — does the tenant currently have voice access (same
      criterion as ``has_voice_access``).
    ``billing_feature_key`` — feature_key of the carrier agent (manifest
      ``includes_voice=True``) under which voice usage must be billed; the
      deterministic ``sorted``-first among all qualifying carriers. ``None``
      when ``allowed`` is False, or — as an invariant safeguard — when a
      carrier qualified but somehow has no ``feature_key`` (fail-closed at
      the call site).
    ``reason`` — short machine/debug tag for the decision.
    """

    allowed: bool
    billing_feature_key: str | None
    reason: str


def _iter_active_subscriptions(session: Session, tenant_id: str):
    """Единый entitlement-фильтр подписок (audit-fix 2026-07-18,
    svc-ops MINOR #9 — раньше критерий дублировался в
    ``active_feature_keys`` и ``_active_voice_carrier_keys`` с
    комментарием-костылём «Mirror active_feature_keys»; рассинхрон давал
    voice-гейт, расходящийся с меню Mini App).

    Yield'ит ``(sub, plan)`` для подписок, которые считаются АКТИВНЫМИ:
    status in {active, scheduled_for_cancel}, quantity > 0,
    ``active_until`` в будущем ИЛИ NULL (= бессрочная free/grandfathered
    подписка — Phase 2 fix 2026-05-08, Codex MAJOR-5: раньше NULL
    ИСКЛЮЧАЛ такие подписки → voice пропадал для новых sreda_free юзеров).
    """
    now = datetime.now(UTC)
    rows = (
        session.query(TenantSubscription, SubscriptionPlan)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .filter(TenantSubscription.tenant_id == tenant_id)
        .all()
    )
    for sub, plan in rows:
        if not sub.quantity or sub.quantity <= 0:
            continue
        if sub.status not in {"active", "scheduled_for_cancel"}:
            continue
        # NULL active_until = unlimited (sreda_free), пропускаем
        # temporal-check; иначе дата должна быть в будущем.
        if sub.active_until is not None:
            active_until = sub.active_until
            if active_until.tzinfo is None:
                active_until = active_until.replace(tzinfo=UTC)
            if active_until <= now:
                continue
        yield sub, plan


def active_feature_keys(session: Session, tenant_id: str) -> set[str]:
    """Return feature_keys of agents the tenant currently has an active
    subscription on. "Active" = status in {active, scheduled_for_cancel}
    and ``active_until`` still in the future.

    Public — the Mini App menu endpoint uses this to decide which
    agents to poll for ``get_miniapp_sections``."""
    active: set[str] = set()
    for _sub, plan in _iter_active_subscriptions(session, tenant_id):
        if plan.feature_key:
            active.add(plan.feature_key)
    return active


def _active_voice_carrier_keys(session: Session, tenant_id: str) -> list[str]:
    """Return feature_keys of the tenant's ACTIVE subscriptions whose agent
    manifest declares ``includes_voice=True`` (the voice "carriers").

    Uses the SAME entitlement criterion as ``active_feature_keys`` (общий
    ``_iter_active_subscriptions``) but preserves a deterministic order:
    the returned list is ``sorted`` by feature_key so the first element is
    a stable choice across runs. We deliberately do NOT reuse
    ``active_feature_keys`` — it returns a ``set`` and loses ordering
    (#204 Решение 1)."""
    registry = get_feature_registry()
    carriers: set[str] = set()
    for _sub, plan in _iter_active_subscriptions(session, tenant_id):
        feature_key = plan.feature_key
        if not feature_key:
            continue
        manifest = registry.get_manifest(feature_key)
        if manifest is not None and getattr(manifest, "includes_voice", False):
            carriers.add(feature_key)
    return sorted(carriers)


def resolve_voice_access(session: Session, tenant_id: str) -> VoiceAccessResult:
    """Resolve voice entitlement AND the carrier feature_key to bill against.

    Same entitlement criterion as ``has_voice_access`` (#204 Решение 1): a
    tenant has voice if any ACTIVE subscription is on an agent whose manifest
    sets ``includes_voice=True``. The billing attribution goes to the
    deterministic ``sorted``-first such carrier — so voice STT usage is
    recorded under the agent that REALLY grants access (e.g.
    ``housewife_assistant``), making it visible in that agent's budget.

    Returns a ``VoiceAccessResult``; ``allowed`` matches ``has_voice_access``
    exactly (criterion is shared → no behaviour change)."""
    if not tenant_id:
        return VoiceAccessResult(False, None, "no_tenant")
    carriers = _active_voice_carrier_keys(session, tenant_id)
    if not carriers:
        return VoiceAccessResult(False, None, "no_voice_carrier")
    return VoiceAccessResult(True, carriers[0], "ok")


def has_voice_access(session: Session, tenant_id: str) -> bool:
    """True if any of the tenant's active agent subscriptions includes
    voice transcription. Dead subscriptions on the legacy standalone
    ``voice_transcription`` plan do NOT grant access — only agents with
    ``includes_voice=True`` in their manifest do.

    Thin wrapper over ``resolve_voice_access`` (#204 Решение 1) — they share
    the entitlement criterion, so behaviour is unchanged."""
    return resolve_voice_access(session, tenant_id).allowed
