"""#181 Phase 1 — EDS (eds_monitor) deactivation regression suite.

Two concerns, both RED-before-implementation:

1. **Invariant (mixed-tenant):** a tenant that migrated EDS→Среда keeps an
   active ``housewife_assistant`` subscription, a paid
   ``voice_transcription`` subscription, AND stale disabled-EDS rows
   (TenantFeature ``eds_monitor``, base+extra EDS subscriptions,
   tenant_eds_accounts, a pending ``eds.verify_account_connect`` job, an
   EDS secure_record). After every Phase-1 deactivation:
     - ``renew_cycle`` renews voice but leaves the EDS subscriptions AND
       their TenantFeature byte-for-byte unchanged;
     - the verification worker no-ops and does NOT mutate the pending
       eds-job.

2. **Denylist (parametrized):** every EDS-dedicated billing mutator, the
   generic simple-subscription mutators against an EDS plan_key, every
   ``EDSConnectService`` mutator, and the ``FeatureRegistry`` registration
   methods all no-op for ``eds_monitor`` with **zero DB mutations**.

3. **Display / route / policy tombstone (R2, #181 Ph1 review):** the
   surfaces that only READ or RENDER EDS state — they do not mutate, so they
   are not in concern (2), but they would otherwise leak stale disabled-EDS
   state back to the user:
     - ``GET /api/v1/eds/accounts`` (the only EDS READ route) returns an
       empty accounts shape;
     - ``subscriptions.show`` legacy fallback (no ``connect_public_base_url``)
       renders NO EDS lines/buttons and performs 0 DB mutations;
     - ``runtime.policy`` short-circuits ``claim.lookup`` /
       ``subscription.add_eds`` / ``eds.connect.start`` / ``eds.connect.retry``
       to the disabled tombstone BEFORE the legacy "подключи EDS" checks —
       even for an old ``TenantFeature(eds_monitor, enabled=False)`` tenant;
     - the inline ``process_job`` entry (not only ``process_pending_jobs``)
       no-ops.

These tests are GREEN across all later phases (the deactivation is the
floor, not a transient state).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.billing import (
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.db.models.connect import TenantEDSAccount
from sreda.db.models.core import (
    Assistant,
    Job,
    SecureRecord,
    Tenant,
    TenantFeature,
    User,
    Workspace,
)
from sreda.services.billing import (
    PLAN_EDS_MONITOR_BASE,
    PLAN_EDS_MONITOR_EXTRA,
    PLAN_VOICE_TRANSCRIPTION,
    BillingService,
)
from sreda.services.eds_connect import EDSConnectService

TENANT = "tenant_mixed"
WORKSPACE = "ws_mixed"
ASSISTANT = "asst_mixed"
NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _plan(
    *,
    id: str,
    plan_key: str,
    feature_key: str,
    title: str,
    price_rub: int,
) -> SubscriptionPlan:
    return SubscriptionPlan(
        id=id,
        plan_key=plan_key,
        feature_key=feature_key,
        title=title,
        description=f"{title} plan",
        price_rub=price_rub,
        billing_period_days=30,
        is_public=True,
        is_active=True,
        sort_order=10,
    )


def _seed_plans(session) -> None:
    """Seed the four plans the suite touches. EDS plans mirror the
    PLAN_SEEDS in billing.py so ``ensure_default_plans`` (run by the
    read-path) would be a no-op even if it fired."""
    session.add_all(
        [
            _plan(
                id="plan_eds_monitor_base",
                plan_key=PLAN_EDS_MONITOR_BASE,
                feature_key="eds_monitor",
                title="EDS Monitor",
                price_rub=2990,
            ),
            _plan(
                id="plan_eds_monitor_extra_account",
                plan_key=PLAN_EDS_MONITOR_EXTRA,
                feature_key="eds_monitor",
                title="Доп. кабинет EDS",
                price_rub=2990,
            ),
            _plan(
                id="plan_voice",
                plan_key=PLAN_VOICE_TRANSCRIPTION,
                feature_key="voice_transcription",
                title="Распознавание голоса",
                price_rub=299,
            ),
            _plan(
                id="plan_housewife",
                plan_key="housewife_assistant_base",
                feature_key="housewife_assistant",
                title="Помощник домохозяйки",
                price_rub=0,
            ),
        ]
    )


def _subscription(
    *,
    id: str,
    plan_id: str,
    feature_key: str,
    active_until: datetime,
    status: str = "active",
    quantity: int = 1,
    next_cycle_quantity: int = 1,
) -> TenantSubscription:
    return TenantSubscription(
        id=id,
        tenant_id=TENANT,
        plan_id=plan_id,
        feature_key=feature_key,
        status=status,
        starts_at=NOW - timedelta(days=5),
        active_until=active_until,
        cancel_at_period_end=False,
        quantity=quantity,
        next_cycle_quantity=next_cycle_quantity,
    )


def _seed_mixed_tenant(session) -> None:
    """Build the migrated-user fixture: active housewife + paid voice +
    stale disabled-EDS rows."""
    session.add(Tenant(id=TENANT, name="Mixed", approved_at=NOW - timedelta(days=30)))
    session.add(Workspace(id=WORKSPACE, tenant_id=TENANT, name="WS"))
    session.add(User(id="user_mixed", tenant_id=TENANT, telegram_account_id="40921122"))
    session.add(
        Assistant(id=ASSISTANT, tenant_id=TENANT, workspace_id=WORKSPACE, name="A")
    )
    _seed_plans(session)

    # Billing cycle whose due date is in the past so renew_cycle advances it.
    session.add(
        TenantBillingCycle(
            id="cycle_mixed",
            tenant_id=TENANT,
            billing_anchor_at=NOW - timedelta(days=30),
            next_payment_due_at=NOW - timedelta(days=1),
            currency="RUB",
            status="active",
        )
    )

    active_until = NOW - timedelta(days=1)
    # Voice — paid, renewable.
    session.add(
        _subscription(
            id="sub_voice",
            plan_id="plan_voice",
            feature_key="voice_transcription",
            active_until=active_until,
        )
    )
    # Housewife — active free, perpetual.
    session.add(
        _subscription(
            id="sub_housewife",
            plan_id="plan_housewife",
            feature_key="housewife_assistant",
            active_until=NOW + timedelta(days=36500),
        )
    )
    # Stale EDS base + extra subscriptions (the user migrated; these are
    # leftover disabled rows that MUST NOT be mutated). At most one row per
    # feature_key may carry status='active' (partial unique index
    # ux_tenant_subs_active_per_feature, migration 0042) — mirrors the prod
    # state after the 2026-05-07 EDS scrub. The extra row is a non-active
    # leftover (scheduled_for_cancel), which the partition must STILL leave
    # byte-for-byte.
    session.add(
        _subscription(
            id="sub_eds_base",
            plan_id="plan_eds_monitor_base",
            feature_key="eds_monitor",
            active_until=active_until,
            status="active",
        )
    )
    session.add(
        _subscription(
            id="sub_eds_extra",
            plan_id="plan_eds_monitor_extra_account",
            feature_key="eds_monitor",
            active_until=active_until,
            status="scheduled_for_cancel",
        )
    )

    # Feature flags.
    session.add(
        TenantFeature(
            id=f"{TENANT}:eds_monitor",
            tenant_id=TENANT,
            feature_key="eds_monitor",
            enabled=True,
        )
    )
    session.add(
        TenantFeature(
            id=f"{TENANT}:voice_transcription",
            tenant_id=TENANT,
            feature_key="voice_transcription",
            enabled=True,
        )
    )
    session.add(
        TenantFeature(
            id=f"{TENANT}:housewife_assistant",
            tenant_id=TENANT,
            feature_key="housewife_assistant",
            enabled=True,
        )
    )

    # Stale EDS account + secure_record + pending verify job.
    session.add(
        TenantEDSAccount(
            id="teds_mixed",
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            assistant_id=ASSISTANT,
            account_index="1",
            account_role="primary",
            status="active",
            login_masked="***99",
        )
    )
    session.add(
        SecureRecord(
            id="sec_eds_mixed",
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            record_type="eds_account_credentials",
            record_key="teds_mixed",
            encrypted_json="{}",
        )
    )
    session.add(
        Job(
            id="job_eds_mixed",
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            job_type="eds.verify_account_connect",
            status="pending",
            payload_json=json.dumps({"connect_session_id": "cs_mixed"}),
        )
    )
    session.commit()


def _coerce_naive(dt: datetime) -> datetime:
    """SQLite round-trips DateTime as naive; normalise both sides to naive
    UTC so comparisons never hit the offset-naive/aware TypeError."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _snapshot(sub: TenantSubscription) -> dict:
    return {
        "status": sub.status,
        "active_until": sub.active_until,
        "quantity": sub.quantity,
        "next_cycle_quantity": sub.next_cycle_quantity,
        "cancel_at_period_end": sub.cancel_at_period_end,
    }


def _feature_snapshot(session, feature_key: str) -> dict:
    feat = (
        session.query(TenantFeature)
        .filter(
            TenantFeature.tenant_id == TENANT,
            TenantFeature.feature_key == feature_key,
        )
        .one()
    )
    return {"id": feat.id, "enabled": feat.enabled}


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------


def test_renew_cycle_renews_voice_but_leaves_eds_byte_for_byte(session) -> None:
    _seed_mixed_tenant(session)
    voice_before = _snapshot(session.get(TenantSubscription, "sub_voice"))
    eds_base_before = _snapshot(session.get(TenantSubscription, "sub_eds_base"))
    eds_extra_before = _snapshot(session.get(TenantSubscription, "sub_eds_extra"))
    eds_feature_before = _feature_snapshot(session, "eds_monitor")

    BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()

    voice_after = _snapshot(session.get(TenantSubscription, "sub_voice"))
    eds_base_after = _snapshot(session.get(TenantSubscription, "sub_eds_base"))
    eds_extra_after = _snapshot(session.get(TenantSubscription, "sub_eds_extra"))
    eds_feature_after = _feature_snapshot(session, "eds_monitor")

    # Voice renewed: active_until advanced.
    assert voice_after["active_until"] > voice_before["active_until"]
    assert voice_after["status"] == "active"

    # EDS subscriptions + feature untouched.
    assert eds_base_after == eds_base_before
    assert eds_extra_after == eds_extra_before
    assert eds_feature_after == eds_feature_before


@pytest.mark.asyncio
async def test_verification_worker_noop_leaves_pending_eds_job(session) -> None:
    _seed_mixed_tenant(session)
    job_before = session.get(Job, "job_eds_mixed")
    status_before = job_before.status
    payload_before = job_before.payload_json

    from sreda.services.eds_account_verification import (
        EDSAccountVerificationService,
    )

    service = EDSAccountVerificationService(session)
    processed = await service.process_pending_jobs(limit=20)
    session.expire_all()

    assert processed == 0
    job_after = session.get(Job, "job_eds_mixed")
    assert job_after.status == status_before == "pending"
    assert job_after.payload_json == payload_before


# ---------------------------------------------------------------------------
# Denylist tests
# ---------------------------------------------------------------------------

DISABLED_TEXT = "Это умение больше не поддерживается."


def _count_mutations(session) -> dict[str, int]:
    counts: dict[str, int] = {}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lstrip().lower()
        for verb in ("insert", "update", "delete"):
            if lowered.startswith(verb):
                counts[verb] = counts.get(verb, 0) + 1

    return counts, _listener


_EDS_BILLING_CALLS = [
    ("start_base_subscription", lambda s: BillingService(s).start_base_subscription(TENANT, now=NOW)),
    ("add_extra_eds_account", lambda s: BillingService(s).add_extra_eds_account(TENANT, now=NOW)),
    ("cancel_base_at_period_end", lambda s: BillingService(s).cancel_base_at_period_end(TENANT)),
    ("resume_base_renewal", lambda s: BillingService(s).resume_base_renewal(TENANT)),
    ("remove_extra_account_at_period_end", lambda s: BillingService(s).remove_extra_account_at_period_end(TENANT, now=NOW)),
    ("schedule_connected_eds_account_cancel", lambda s: BillingService(s).schedule_connected_eds_account_cancel(TENANT, "teds_mixed")),
    ("restore_extra_account_slot", lambda s: BillingService(s).restore_extra_account_slot(TENANT, now=NOW)),
    ("restore_connected_eds_account_cancel", lambda s: BillingService(s).restore_connected_eds_account_cancel(TENANT, "teds_mixed", now=NOW)),
    # Generic simple-subscription mutators against an EDS plan_key.
    ("start_simple_subscription[eds]", lambda s: BillingService(s).start_simple_subscription(TENANT, PLAN_EDS_MONITOR_BASE, now=NOW)),
    ("cancel_simple_subscription[eds]", lambda s: BillingService(s).cancel_simple_subscription(TENANT, PLAN_EDS_MONITOR_BASE)),
]


@pytest.mark.parametrize("name,call", _EDS_BILLING_CALLS, ids=[c[0] for c in _EDS_BILLING_CALLS])
def test_billing_eds_method_is_noop_disabled(session, name, call) -> None:
    _seed_mixed_tenant(session)
    engine = session.get_bind()
    counts, listener = _count_mutations(session)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        result = call(session)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert result.message_text == DISABLED_TEXT
    assert result.reply_markup == {}
    assert counts == {}, f"{name} performed DB mutations: {counts}"


def test_generic_simple_subscription_voice_still_works(session) -> None:
    """The generic guard must cut ONLY the EDS plan_key — voice/housewife
    (different feature_key) keep flowing through start/cancel_simple."""
    _seed_mixed_tenant(session)
    result = BillingService(session).cancel_simple_subscription(
        TENANT, PLAN_VOICE_TRANSCRIPTION
    )
    assert result.message_text != DISABLED_TEXT
    session.expire_all()
    voice = session.get(TenantSubscription, "sub_voice")
    assert voice.status == "cancelled"


_EDS_CONNECT_CALLS = [
    (
        "create_connect_link",
        lambda s: EDSConnectService(s, get_settings()).create_connect_link(
            tenant_id=TENANT, workspace_id=WORKSPACE, user_id="user_mixed", slot_type="extra"
        ),
    ),
    ("open_form", lambda s: EDSConnectService(s, get_settings()).open_form("tok")),
    (
        "submit_form",
        lambda s: EDSConnectService(s, get_settings()).submit_form(
            "tok", login="u@example.com", password="pw"
        ),
    ),
]


@pytest.mark.parametrize("name,call", _EDS_CONNECT_CALLS, ids=[c[0] for c in _EDS_CONNECT_CALLS])
def test_eds_connect_service_noop_disabled(session, name, call) -> None:
    _seed_mixed_tenant(session)
    engine = session.get_bind()
    counts, listener = _count_mutations(session)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with pytest.raises(Exception) as exc_info:
            call(session)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    # Disabled → ConnectSessionError (no row created).
    from sreda.services.eds_connect import ConnectSessionError

    assert isinstance(exc_info.value, ConnectSessionError)
    assert counts == {}, f"{name} performed DB mutations: {counts}"


def test_registry_does_not_register_eds_monitor() -> None:
    from sreda.features.registry import FeatureRegistry

    registry = FeatureRegistry()

    class _Module:
        feature_key = "eds_monitor"

        def register_api(self, app):  # pragma: no cover - never called
            raise AssertionError("eds_monitor module should not be registered")

        def register_runtime(self):  # pragma: no cover
            raise AssertionError

        def register_workers(self):  # pragma: no cover
            raise AssertionError

    registry.register(_Module())
    assert registry.modules == {}

    async def _handler(*a, **k):  # pragma: no cover
        return None

    registry.register_skill_job_handler(
        feature_key="eds_monitor", job_type="eds.verify_account_connect", handler=_handler
    )
    assert registry.skill_job_types() == []

    registry.register_proactive_handler(feature_key="eds_monitor", handler=lambda ctx: [])
    assert registry.get_proactive_handler("eds_monitor") is None

    registry.register_delivery_hook(feature_key="eds_monitor", hook=_handler)
    assert registry.get_delivery_hook("eds_monitor") is None


def test_registry_still_registers_non_eds_feature() -> None:
    from sreda.features.registry import FeatureRegistry

    registry = FeatureRegistry()

    async def _handler(*a, **k):
        return None

    registry.register_skill_job_handler(
        feature_key="housewife_assistant", job_type="housewife.reminder", handler=_handler
    )
    assert registry.skill_job_types() == ["housewife.reminder"]


# ---------------------------------------------------------------------------
# Display / route / policy tombstone tests (R2 — #181 Ph1 review)
# ---------------------------------------------------------------------------

DISABLED_TOMBSTONE_TEXT = "Это умение отключено."


# --- Route: GET /api/v1/eds/accounts → empty (no EDS account details) -------


@pytest.fixture()
def miniapp_client(monkeypatch, tmp_path):
    """TestClient with in-memory SQLite + a pre-seeded migrated tenant whose
    eds_monitor TenantFeature is DISABLED (enabled=False) and who has stale
    TenantEDSAccount rows. Mirrors tests/unit/test_miniapp_api.py wiring."""
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    from fastapi.testclient import TestClient

    bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    db_path = tmp_path / "test_eds_route.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.test.local")

    from sreda.api.deps import reset_rate_limiters
    from sreda.config.settings import get_settings as _get_settings
    from sreda.db.base import Base as _Base
    from sreda.db.session import get_engine, get_session_factory
    from sreda.main import create_app

    from sreda.db.repositories.seed import SeedRepository

    _get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()
    _Base.metadata.create_all(get_engine())

    # Seed a migrated tenant (eds_monitor TenantFeature DISABLED) with a stale
    # active TenantEDSAccount, so a non-tombstoned route WOULD leak the account
    # (login_masked "***42"). Use SeedRepository for the bundle so all FKs are
    # satisfied (the in-app engine enforces FK constraints).
    s = get_session_factory()()
    try:
        SeedRepository(s).ensure_tenant_bundle(
            tenant_id="tenant_route",
            tenant_name="Route",
            workspace_id="ws_route",
            workspace_name="WS",
            user_id="user_route",
            telegram_account_id="352612382",
            assistant_id="asst_route",
            assistant_name="A",
            eds_monitor_enabled=False,
        )
        s.add(
            TenantEDSAccount(
                id="teds_route",
                tenant_id="tenant_route",
                workspace_id="ws_route",
                assistant_id="asst_route",
                account_index="1",
                account_role="primary",
                status="active",
                login_masked="***42",
            )
        )
        s.commit()
    finally:
        s.close()

    def _init_data() -> str:
        auth_date = int(time.time())
        user_json = json.dumps(
            {"id": 352612382, "first_name": "Test", "username": "t"},
            separators=(",", ":"),
        )
        params = {"auth_date": str(auth_date), "user": user_json}
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        return urlencode(params)

    with TestClient(create_app()) as c:
        c._eds_auth_header = {"Authorization": f"tma {_init_data()}"}  # type: ignore[attr-defined]
        yield c

    _get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()


def test_route_eds_accounts_returns_empty_when_disabled(miniapp_client) -> None:
    resp = miniapp_client.get(
        "/miniapp/api/v1/eds/accounts", headers=miniapp_client._eds_auth_header
    )
    assert resp.status_code == 200
    data = resp.json()
    # Tombstoned: empty accounts, no stale "***42" login leaked back.
    assert data == {"accounts": []}
    assert "***42" not in resp.text


# --- subscriptions.show legacy fallback: no EDS lines/buttons, 0 mutations --


def test_subscriptions_show_fallback_has_no_eds_and_no_mutation(session) -> None:
    """``subscriptions.show`` with NO connect_public_base_url falls back to
    ``build_subscriptions_message``; with eds_monitor retired it must render
    voice + nav only — no EDS active/available lines, no EDS buttons — and
    perform ZERO DB mutations."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)
    # Drain the unrelated plan-catalog self-heal (ensure_default_plans) BEFORE
    # measuring, so the mutation count reflects ONLY the tombstone render path
    # (which must touch no tenant EDS state). ensure_default_plans writes to the
    # subscription_plans catalog, not to tenant subscriptions/accounts.
    billing.ensure_default_plans()
    session.commit()

    eds_base_before = _snapshot(session.get(TenantSubscription, "sub_eds_base"))
    eds_extra_before = _snapshot(session.get(TenantSubscription, "sub_eds_extra"))

    engine = session.get_bind()
    counts, listener = _count_mutations(session)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        text, markup = billing.build_subscriptions_message(TENANT, now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    # No EDS lines in the rendered body.
    assert "EDS" not in text
    assert "ЛК" not in text
    # Voice is still rendered (non-EDS part survives).
    assert "Распознавание голоса" in text

    # No EDS buttons (connect / add / remove / restore / onboarding:connect_eds).
    flat = [btn for row in markup.get("inline_keyboard", []) for btn in row]
    callbacks = [b.get("callback_data", "") for b in flat]
    labels = [b.get("text", "") for b in flat]
    assert not any("eds" in (cb or "").lower() for cb in callbacks)
    assert not any("EDS" in lbl for lbl in labels)
    assert "onboarding:connect_eds" not in callbacks
    # Navigation button remains.
    assert any("Мой статус" in lbl for lbl in labels)

    # Pure render → zero mutations of tenant state.
    assert counts == {}, f"build_subscriptions_message mutated DB: {counts}"
    # And the stale EDS subscriptions are byte-for-byte untouched.
    session.expire_all()
    assert _snapshot(session.get(TenantSubscription, "sub_eds_base")) == eds_base_before
    assert _snapshot(session.get(TenantSubscription, "sub_eds_extra")) == eds_extra_before


def test_subscriptions_show_handler_fallback_no_eds(session, monkeypatch) -> None:
    """End-to-end through the runtime handler: with connect_public_base_url
    unset the handler hits the legacy fallback, which must carry no EDS
    content for a retired skill."""
    _seed_mixed_tenant(session)

    from sreda.runtime import handlers as handlers_mod
    from sreda.runtime.dispatcher import ActionEnvelope

    class _Settings:
        connect_public_base_url = None

    monkeypatch.setattr(handlers_mod, "get_settings", lambda: _Settings())

    envelope = ActionEnvelope(
        action_type="subscriptions.show",
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        assistant_id=ASSISTANT,
        user_id="user_mixed",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_callback",
        source_value="subscriptions.show",
        params={},
    )
    replies = handlers_mod.execute_subscriptions_show(session, envelope, {})
    assert len(replies) == 1
    reply = replies[0]
    assert "EDS" not in reply.text
    flat = [btn for row in reply.reply_markup.get("inline_keyboard", []) for btn in row]
    assert not any("EDS" in b.get("text", "") for b in flat)
    assert not any("eds" in (b.get("callback_data", "") or "").lower() for b in flat)


# --- policy: bypass legacy "подключи EDS" checks for a retired skill --------

_POLICY_DISABLED_ACTIONS = [
    "claim.lookup",
    "subscription.add_eds",
    "eds.connect.start",
    "eds.connect.retry",
]


def _policy_envelope(action_type: str):
    from sreda.runtime.dispatcher import ActionEnvelope

    return ActionEnvelope(
        action_type=action_type,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        assistant_id=ASSISTANT,
        user_id="user_mixed",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_callback",
        source_value=action_type,
        params={"claim_id": "6230173"} if action_type == "claim.lookup" else {},
    )


@pytest.mark.parametrize("action_type", _POLICY_DISABLED_ACTIONS)
def test_policy_bypasses_legacy_eds_prompt_when_disabled(action_type) -> None:
    """For a stale tenant whose ``TenantFeature(eds_monitor)`` is enabled=False
    (context ``eds_monitor_enabled=False``) AND base_active=False, the policy
    must NOT emit the legacy "подключи EDS" / "Сначала подключи EDS" prompt for
    any EDS action type. It passes through (returns None) so the downstream
    tombstoned handler/service answers — never the old connect/subscribe text.

    Pre-#181 this returned an ``eds_monitor_disabled`` /
    ``subscription_required`` error carrying the legacy connect prompt; the
    bypass closes that gap.
    """
    from sreda.runtime.policy import evaluate_policy

    # Mirrors graph.py context for a TenantFeature(enabled=False) tenant with
    # no active EDS subscription — the exact state the reviewers flagged.
    context = {
        "tenant_id": TENANT,
        "workspace_id": WORKSPACE,
        "eds_monitor_enabled": False,
        "billing_summary": {
            "base_active": False,
            "allowed_count": 0,
            "connected_count": 0,
            "free_count": 0,
        },
    }

    error = evaluate_policy(_policy_envelope(action_type), context)
    # Bypass → pass through to the (tombstoned) handler/service.
    assert error is None


def test_policy_claim_lookup_disabled_reaches_handler_tombstone(session) -> None:
    """End-to-end: policy bypass + handler tombstone → the disabled notice
    "Это умение отключено.", NOT the legacy "подключи EDS" prompt."""
    from sreda.runtime import handlers as handlers_mod
    from sreda.runtime.policy import evaluate_policy

    _seed_mixed_tenant(session)
    envelope = _policy_envelope("claim.lookup")
    context = {
        "tenant_id": TENANT,
        "workspace_id": WORKSPACE,
        "eds_monitor_enabled": False,
        "billing_summary": {
            "base_active": False,
            "allowed_count": 0,
            "connected_count": 0,
            "free_count": 0,
        },
    }
    assert evaluate_policy(envelope, context) is None  # policy lets it through
    replies = handlers_mod.execute_claim_lookup(session, envelope, context)
    assert len(replies) == 1
    assert replies[0].text == DISABLED_TOMBSTONE_TEXT
    assert "EDS" not in replies[0].text


def test_policy_allows_non_eds_action() -> None:
    """The global EDS bypass must NOT swallow unrelated actions."""
    from sreda.runtime.policy import evaluate_policy

    # help.show short-circuits to None (allowed) regardless of context.
    assert evaluate_policy(_policy_envelope("help.show"), {}) is None


# --- inline process_job (not only process_pending_jobs) no-op ---------------


@pytest.mark.asyncio
async def test_process_job_inline_noop_leaves_pending(session) -> None:
    """The inline (connect.py) ``process_job`` entry must no-op to "skipped"
    BEFORE touching the job, leaving the pending eds-job byte-for-byte."""
    _seed_mixed_tenant(session)
    job_before = session.get(Job, "job_eds_mixed")
    status_before = job_before.status
    payload_before = job_before.payload_json

    from sreda.services.eds_account_verification import EDSAccountVerificationService

    engine = session.get_bind()
    counts, listener = _count_mutations(session)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        result = await EDSAccountVerificationService(session).process_job("job_eds_mixed")
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert result == "skipped"
    session.expire_all()
    job_after = session.get(Job, "job_eds_mixed")
    assert job_after.status == status_before == "pending"
    assert job_after.payload_json == payload_before
    assert counts == {}, f"process_job mutated DB: {counts}"


# ---------------------------------------------------------------------------
# Runtime executor-level tombstone tests (R3 — #181 Ph1 review)
#
# evaluate_policy(...) is None proves the policy BYPASS, but not that the full
# action lands as a COMPLETED run with a tombstone reply and zero EDS mutations.
# These run the whole executor (graph → policy → handler → persist) and assert:
#   - run.status == "completed" (NOT failed — the connect actions must be a
#     completed tombstone like /claim, the regression the reviewers flagged);
#   - the user-facing reply is a disabled notice, NOT a connect link / EDS sum;
#   - the seeded stale-EDS rows are byte-for-byte untouched (0 mutations).
# ---------------------------------------------------------------------------

ENGINE_TENANT = "tenant_1"
ENGINE_WORKSPACE = "workspace_1"
ENGINE_ASSISTANT = "assistant_1"
ENGINE_CHAT = "100000003"


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(
        self, chat_id, text, parse_mode=None, reply_markup=None
    ) -> dict:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    async def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None) -> dict:
        return {"ok": True}


def _seed_engine_tenant(session) -> None:
    """Seed a migrated tenant on the env-backed engine: voice + stale EDS
    base sub (next_cycle_quantity>0) + EDS feature + pending eds-job."""
    from sreda.db.models.core import Assistant, Job, Tenant, TenantFeature, User, Workspace

    session.add(Tenant(id=ENGINE_TENANT, name="T"))
    session.add(Workspace(id=ENGINE_WORKSPACE, tenant_id=ENGINE_TENANT, name="WS"))
    session.flush()
    session.add(
        Assistant(id=ENGINE_ASSISTANT, tenant_id=ENGINE_TENANT, workspace_id=ENGINE_WORKSPACE, name="A")
    )
    session.add(User(id="user_1", tenant_id=ENGINE_TENANT, telegram_account_id=ENGINE_CHAT))
    _seed_plans(session)

    active_until = datetime.now(UTC) - timedelta(days=1)
    session.add(
        TenantSubscription(
            id="sub_eds_base",
            tenant_id=ENGINE_TENANT,
            plan_id="plan_eds_monitor_base",
            feature_key="eds_monitor",
            status="active",
            starts_at=active_until - timedelta(days=29),
            active_until=active_until,
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    session.add(
        TenantSubscription(
            id="sub_voice",
            tenant_id=ENGINE_TENANT,
            plan_id="plan_voice",
            feature_key="voice_transcription",
            status="active",
            starts_at=active_until - timedelta(days=29),
            active_until=active_until,
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    session.add(
        TenantFeature(
            id=f"{ENGINE_TENANT}:eds_monitor",
            tenant_id=ENGINE_TENANT,
            feature_key="eds_monitor",
            enabled=False,
        )
    )
    session.add(
        Job(
            id="job_eds_engine",
            tenant_id=ENGINE_TENANT,
            workspace_id=ENGINE_WORKSPACE,
            job_type="eds.verify_account_connect",
            status="pending",
            payload_json=json.dumps({"connect_session_id": "cs"}),
        )
    )
    session.commit()


def _eds_state_snapshot(session) -> dict:
    """Snapshot the stale-EDS rows that must survive an executor run."""
    from sreda.db.models.core import Job, TenantFeature

    base = session.get(TenantSubscription, "sub_eds_base")
    feat = (
        session.query(TenantFeature)
        .filter(TenantFeature.tenant_id == ENGINE_TENANT, TenantFeature.feature_key == "eds_monitor")
        .one()
    )
    job = session.get(Job, "job_eds_engine")
    return {
        "base": _snapshot(base),
        "feature_enabled": feat.enabled,
        "job_status": job.status,
        "job_payload": job.payload_json,
    }


_RUNTIME_TOMBSTONE_ACTIONS = [
    # (action_type, source_value, params, expected_tombstone_text)
    ("subscription.add_eds", "billing:add_eds_account", {}, "Это умение больше не поддерживается."),
    ("eds.connect.start", "onboarding:connect_eds", {"slot_type": "primary"}, "Это умение отключено."),
    ("eds.connect.retry", "eds:connect_retry", {"slot_type": "extra"}, "Это умение отключено."),
]


@pytest.mark.parametrize(
    "action_type,source_value,params,expected_text",
    _RUNTIME_TOMBSTONE_ACTIONS,
    ids=[a[0] for a in _RUNTIME_TOMBSTONE_ACTIONS],
)
def test_runtime_executor_eds_action_completed_tombstone_no_mutation(
    monkeypatch, tmp_path: Path, action_type, source_value, params, expected_text
) -> None:
    from sreda.config.settings import get_settings
    from sreda.db.base import Base
    from sreda.db.models import AgentRun, Job, OutboxMessage
    from sreda.db.session import get_engine, get_session_factory
    from sreda.runtime.dispatcher import ActionEnvelope
    from sreda.runtime.executor import ActionRuntimeService

    db_path = tmp_path / f"runtime_{action_type.replace('.', '_')}.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        _seed_engine_tenant(session)
        before = _eds_state_snapshot(session)

        telegram_client = _FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)
        queued = service.enqueue_action(
            ActionEnvelope(
                action_type=action_type,
                tenant_id=ENGINE_TENANT,
                workspace_id=ENGINE_WORKSPACE,
                assistant_id=ENGINE_ASSISTANT,
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id=ENGINE_CHAT,
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_callback",
                source_value=source_value,
                params=params,
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))

        # Refresh from DB and capture scalars BEFORE closing the session.
        session.expire_all()
        after = _eds_state_snapshot(session)
        run_status = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one().status
        job_status = session.query(Job).filter(Job.id == queued.job_id).one().status
        outbox_count = session.query(OutboxMessage).count()
    finally:
        session.close()

    # Completed run (NOT failed) — the connect actions must be a completed
    # tombstone like /claim, not the ConnectSessionError → failed-run path.
    assert result == "completed"
    assert run_status == "completed"
    assert job_status == "completed"

    # User sees the disabled tombstone, never a connect link / EDS amount.
    assert len(telegram_client.sent_messages) == 1
    sent_text = telegram_client.sent_messages[0]["text"]
    assert expected_text in sent_text
    assert "защищенн" not in sent_text  # no connect-link intro
    assert "Ввести логин" not in sent_text
    markup = telegram_client.sent_messages[0]["reply_markup"] or {}
    flat = [b for row in markup.get("inline_keyboard", []) for b in row]
    assert not any("EDS" in b.get("text", "") for b in flat)

    # Zero EDS-state mutations: the stale rows are byte-for-byte unchanged.
    assert after == before
    # Outbox carries exactly one (the tombstone), not a failure notice.
    assert outbox_count == 1


# ---------------------------------------------------------------------------
# Display tombstone — stale-EDS payment amount/date must NOT leak (R3)
# ---------------------------------------------------------------------------


def test_build_status_message_hides_eds_amount_for_stale_tenant(session) -> None:
    """A migrated tenant whose ONLY renewable subs are the stale EDS base+extra
    (next_cycle_quantity>0) must NOT see "Сумма к оплате" at all — get_summary
    would surface 5980 ₽ (base+extra) for the retired skill. Voice here is
    cancelled, so there is no non-EDS renewal → whole payment block suppressed."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)
    # Cancel voice so the ONLY renewable contributor would be EDS.
    billing.cancel_simple_subscription(TENANT, PLAN_VOICE_TRANSCRIPTION)
    session.commit()

    # get_summary still reports the EDS charge (quarantined read-path).
    summary = billing.get_summary(TENANT, now=NOW)
    assert summary.next_amount_rub == 5980  # base+extra EDS still counted internally

    text, _markup = billing.build_status_message(TENANT, now=NOW)
    # Display tombstone: NO payment lines, NO EDS sum, NO EDS lines.
    assert "Сумма к оплате" not in text
    assert "Следующий платеж" not in text
    assert "5980" not in text
    assert "2990" not in text
    assert "EDS" not in text


def test_build_status_message_keeps_voice_amount_for_stale_tenant(session) -> None:
    """The recompute excludes ONLY disabled-feature plans: a stale-EDS tenant
    who ALSO has a renewable voice sub still sees the voice amount (299 ₽),
    never the EDS contribution (the get_summary figure of 299+2990)."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)

    text, _markup = billing.build_status_message(TENANT, now=NOW)
    # Payment block present, voice amount shown, EDS amount excluded.
    assert "Сумма к оплате: 299 ₽" in text
    assert "2990" not in text
    assert "3289" not in text  # never the EDS+voice sum
    assert "EDS" not in text


def test_api_summary_excludes_eds_amount_for_stale_tenant(miniapp_client) -> None:
    """``GET /api/v1/summary`` for a stale-EDS tenant must echo a display
    amount that EXCLUDES the retired EDS plan. We seed a stale EDS base sub
    (next_cycle_quantity>0) + a billing cycle so that WITHOUT the display fix
    get_summary would surface 2990 ₽ for the retired skill. With no non-EDS
    renewal the banner is hidden (amount 0 / due null)."""
    from sreda.db.session import get_session_factory

    # Seed the stale EDS state into the route tenant so the leak is real.
    s = get_session_factory()()
    try:
        BillingService(s).ensure_default_plans()  # creates EDS plan rows
        s.add(
            TenantBillingCycle(
                id="cycle_route",
                tenant_id="tenant_route",
                billing_anchor_at=datetime.now(UTC) - timedelta(days=30),
                next_payment_due_at=datetime.now(UTC) + timedelta(days=1),
                currency="RUB",
                status="active",
            )
        )
        base_plan_id = s.query(SubscriptionPlan).filter(
            SubscriptionPlan.plan_key == PLAN_EDS_MONITOR_BASE
        ).one().id
        s.add(
            TenantSubscription(
                id="sub_eds_base_route",
                tenant_id="tenant_route",
                plan_id=base_plan_id,
                feature_key="eds_monitor",
                status="active",
                starts_at=datetime.now(UTC) - timedelta(days=29),
                active_until=datetime.now(UTC) + timedelta(days=1),
                cancel_at_period_end=False,
                quantity=1,
                next_cycle_quantity=1,
            )
        )
        s.commit()
        # Sanity: the quarantined read-path WOULD surface the EDS charge.
        assert BillingService(s).get_summary("tenant_route").next_amount_rub == 2990
    finally:
        s.close()

    resp = miniapp_client.get(
        "/miniapp/api/v1/summary", headers=miniapp_client._eds_auth_header
    )
    assert resp.status_code == 200
    data = resp.json()
    # No EDS charge leaks; with no non-EDS renewal the banner is hidden.
    assert data["next_amount_rub"] == 0
    assert data["next_payment_due_at"] is None
    assert data["eds_subscriptions"] == []


# ---------------------------------------------------------------------------
# Display tombstone — renew_cycle success message must NOT leak stale-EDS sum
# (R4 — code-grounded reviewer found a 3rd amount-render surface). renew_cycle
# is intentionally NOT disabled (it renews voice/housewife), but its success
# message rendered ``summary.next_amount_rub`` / ``next_payment_due_at`` from
# the quarantined get_summary read-path (EDS-only sum). A stale-EDS tenant
# (eds next_cycle_quantity>0) renewing voice would see a phantom EDS charge.
# Reachable from BOTH Telegram callback (billing:renew →
# execute_subscription_renew_cycle) and miniapp POST /api/v1/renew.
# ---------------------------------------------------------------------------


def test_renew_cycle_message_excludes_eds_amount_keeps_voice(session) -> None:
    """A stale-EDS tenant (eds next_cycle_quantity>0) renewing the renewable
    voice subscription must see the VOICE amount (299 ₽) — never the EDS
    contribution (2990 base / 5980 base+extra). The success message recomputes
    the figure disabled-aware via ``compute_display_next_payment`` instead of
    the EDS-only get_summary sum."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)

    # Sanity: the quarantined get_summary read-path WOULD surface the EDS
    # charge (base 2990 + extra 2990, both with next_cycle_quantity=1 → 5980).
    assert billing.get_summary(TENANT, now=NOW).next_amount_rub == 5980

    result = billing.renew_cycle(TENANT, now=NOW)
    msg = result.message_text

    assert "Подписка продлена." in msg
    # Voice amount shown, EDS amount excluded.
    assert "Сумма следующего платежа: 299 ₽" in msg
    assert "2990" not in msg
    assert "5980" not in msg
    assert "3289" not in msg  # never the EDS+voice sum
    assert "6279" not in msg  # never the EDS-base+extra+voice sum
    assert "EDS" not in msg

    # And the renewal itself still renewed voice / left EDS byte-for-byte.
    session.expire_all()
    voice = session.get(TenantSubscription, "sub_voice")
    assert voice.status == "active"
    assert _coerce_naive(voice.active_until) > _coerce_naive(NOW)
    eds_base = session.get(TenantSubscription, "sub_eds_base")
    assert eds_base.status == "active"
    assert eds_base.quantity == 1
    assert eds_base.next_cycle_quantity == 1


def test_renew_cycle_message_suppresses_payment_line_when_only_eds(session) -> None:
    """If the ONLY renewable contributor would be the stale EDS sub (voice
    cancelled), the disabled-aware recompute yields nothing to charge → the
    "Сумма следующего платежа" line is dropped entirely (no phantom 2990 ₽)."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)
    # Cancel voice so the only renewable contributor would be EDS.
    billing.cancel_simple_subscription(TENANT, PLAN_VOICE_TRANSCRIPTION)
    session.commit()

    result = billing.renew_cycle(TENANT, now=NOW)
    msg = result.message_text

    # Voice cancelled → still a renewable? housewife is free (price 0). Either
    # way no EDS sum, and no phantom charge line.
    assert "2990" not in msg
    assert "5980" not in msg
    assert "EDS" not in msg


def test_renew_cycle_through_executor_renews_voice_no_eds_amount(
    monkeypatch, tmp_path: Path
) -> None:
    """End-to-end through the executor (``subscription.renew_cycle`` →
    execute_subscription_renew_cycle → BillingService.renew_cycle): a migrated
    stale-EDS tenant renewing voice lands as a COMPLETED run, the user-facing
    reply renews voice (299 ₽) and carries NO EDS amount (2990/5980), and the
    stale EDS subscription is byte-for-byte untouched."""
    from sreda.config.settings import get_settings
    from sreda.db.base import Base
    from sreda.db.models import AgentRun, Job
    from sreda.db.session import get_engine, get_session_factory
    from sreda.runtime.dispatcher import ActionEnvelope
    from sreda.runtime.executor import ActionRuntimeService

    db_path = tmp_path / "runtime_renew_cycle.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        _seed_engine_tenant(session)
        # _seed_engine_tenant has voice + stale EDS base but NO billing cycle;
        # renew_cycle needs a cycle whose due date is in the past so it advances.
        session.add(
            TenantBillingCycle(
                id="cycle_engine",
                tenant_id=ENGINE_TENANT,
                billing_anchor_at=datetime.now(UTC) - timedelta(days=30),
                next_payment_due_at=datetime.now(UTC) - timedelta(days=1),
                currency="RUB",
                status="active",
            )
        )
        session.commit()
        before = _eds_state_snapshot(session)

        telegram_client = _FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)
        queued = service.enqueue_action(
            ActionEnvelope(
                action_type="subscription.renew_cycle",
                tenant_id=ENGINE_TENANT,
                workspace_id=ENGINE_WORKSPACE,
                assistant_id=ENGINE_ASSISTANT,
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id=ENGINE_CHAT,
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_callback",
                source_value="billing:renew",
                params={},
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))

        session.expire_all()
        after = _eds_state_snapshot(session)
        run_status = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one().status
        job_status = session.query(Job).filter(Job.id == queued.job_id).one().status
        voice_after = _snapshot(session.get(TenantSubscription, "sub_voice"))
    finally:
        session.close()

    assert result == "completed"
    assert run_status == "completed"
    assert job_status == "completed"

    # Voice renewed (active in the future).
    assert voice_after["status"] == "active"
    assert _coerce_naive(voice_after["active_until"]) > _coerce_naive(datetime.now(UTC))

    # User-facing reply: voice amount, no EDS leak.
    assert len(telegram_client.sent_messages) == 1
    sent_text = telegram_client.sent_messages[0]["text"]
    assert "Подписка продлена." in sent_text
    assert "299" in sent_text
    assert "2990" not in sent_text
    assert "5980" not in sent_text
    assert "EDS" not in sent_text

    # Stale EDS subscription / feature / job byte-for-byte unchanged.
    assert after == before
