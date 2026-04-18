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

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.features.app_registry import get_feature_registry


def active_feature_keys(session: Session, tenant_id: str) -> set[str]:
    """Return feature_keys of agents the tenant currently has an active
    subscription on. "Active" = status in {active, scheduled_for_cancel}
    and ``active_until`` still in the future.

    Public — the Mini App menu endpoint uses this to decide which
    agents to poll for ``get_miniapp_sections``."""
    now = datetime.now(UTC)
    rows = (
        session.query(TenantSubscription, SubscriptionPlan)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .filter(TenantSubscription.tenant_id == tenant_id)
        .all()
    )

    active: set[str] = set()
    for sub, plan in rows:
        if not sub.quantity or sub.quantity <= 0:
            continue
        if sub.status not in {"active", "scheduled_for_cancel"}:
            continue
        if not sub.active_until:
            continue
        active_until = sub.active_until
        if active_until.tzinfo is None:
            active_until = active_until.replace(tzinfo=UTC)
        if active_until <= now:
            continue
        if plan.feature_key:
            active.add(plan.feature_key)
    return active


def has_voice_access(session: Session, tenant_id: str) -> bool:
    """True if any of the tenant's active agent subscriptions includes
    voice transcription. Dead subscriptions on the legacy standalone
    ``voice_transcription`` plan do NOT grant access — only agents with
    ``includes_voice=True`` in their manifest do."""
    if not tenant_id:
        return False
    active_keys = active_feature_keys(session, tenant_id)
    if not active_keys:
        return False
    registry = get_feature_registry()
    for feature_key in active_keys:
        manifest = registry.get_manifest(feature_key)
        if manifest is not None and getattr(manifest, "includes_voice", False):
            return True
    return False
