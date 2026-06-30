"""#181 Phase B — EDS (eds_monitor) FINAL-removal regression suite.

EDS Monitor is fully retired: the engine (Phase 1/2), the eds_monitor DB
tables (Phase 4-A) and now the connect-layer + billing read-path + connect
tables (Phase B). This suite locks in the post-removal contract:

1. **INVARIANT (critical):** a tenant that migrated EDS→Среда with an active
   ``housewife_assistant`` subscription + a paid ``voice_transcription``
   subscription keeps working AND renewing after EDS is gone:
     - ``renew_cycle`` renews the voice subscription and shows the VOICE
       amount (never an EDS amount — there are no EDS rows left);
     - the runtime ``node_load_context`` (called every turn) does not crash
       and no longer loads any EDS billing summary / feature flag;
     - the generic simple-subscription path (voice/housewife) is unaffected.

2. **Tombstones:** the legacy EDS surfaces still answer cleanly (no 404/500)
   instead of routing to removed code:
     - ``GET /api/v1/eds/accounts`` → empty; the EDS POST routes →
       ``{"ok": False, ...}`` disabled message;
     - ``GET/POST /connect/eds/{token}`` → disabled HTML page (HTTP 200);
     - ``runtime.policy`` lets the legacy EDS action types through to their
       tombstoned handlers ("Это умение отключено.");
     - the runtime executor lands those actions as COMPLETED runs (not
       failed) with a disabled tombstone reply.

The EDS subscriptions/plans/accounts no longer exist (dropped by migration
20260622_0060), so there is nothing EDS-shaped left to mutate or leak — the
suite seeds only a stale ``TenantFeature(eds_monitor, enabled=False)`` (feature
history is intentionally kept) and asserts the surviving non-EDS behaviour.
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

from sreda.db.base import Base
from sreda.db.models.billing import (
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.db.models.core import (
    Assistant,
    Tenant,
    TenantFeature,
    User,
    Workspace,
)
from sreda.services.billing import (
    DISABLED_FEATURE_MESSAGE,
    PLAN_VOICE_TRANSCRIPTION,
    BillingService,
)

TENANT = "tenant_mixed"
WORKSPACE = "ws_mixed"
ASSISTANT = "asst_mixed"
NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)

DISABLED_TOMBSTONE_TEXT = "Это умение отключено."
DISABLED_TEXT = "Это умение больше не поддерживается."


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
    """Seed the surviving non-EDS plans (voice + housewife). #181 Phase B: the
    EDS plans are dropped — no EDS plan row is seeded anywhere anymore."""
    session.add_all(
        [
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
    """Build the migrated-user fixture: active housewife + paid voice + a stale
    disabled ``TenantFeature(eds_monitor)`` (feature history kept). #181 Phase B:
    NO EDS subscription / account / plan rows — those are gone."""
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

    # Feature flags. eds_monitor is kept as a DISABLED row (feature history is
    # intentionally not cleaned — see migration 20260622_0060 docstring).
    session.add(
        TenantFeature(
            id=f"{TENANT}:eds_monitor",
            tenant_id=TENANT,
            feature_key="eds_monitor",
            enabled=False,
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
    session.commit()


def _coerce_naive(dt: datetime) -> datetime:
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


def _count_mutations(session) -> tuple[dict[str, int], object]:
    counts: dict[str, int] = {}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lstrip().lower()
        for verb in ("insert", "update", "delete"):
            if lowered.startswith(verb):
                counts[verb] = counts.get(verb, 0) + 1

    return counts, _listener


# ---------------------------------------------------------------------------
# INVARIANT — the migrated tenant keeps working/renewing after EDS removal
# ---------------------------------------------------------------------------


def test_renew_cycle_renews_voice_and_shows_voice_amount(session) -> None:
    """The critical invariant: a migrated EDS→Среда tenant renews voice and the
    success message carries the VOICE amount (299 ₽) — never an EDS amount
    (there are no EDS rows left to sum)."""
    _seed_mixed_tenant(session)
    voice_before = _snapshot(session.get(TenantSubscription, "sub_voice"))

    result = BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()

    voice_after = _snapshot(session.get(TenantSubscription, "sub_voice"))
    # Voice renewed: active_until advanced, still active.
    assert voice_after["active_until"] > voice_before["active_until"]
    assert voice_after["status"] == "active"

    # Success message shows the voice amount, no EDS amount/word.
    assert "Подписка продлена." in result.message_text
    assert "Сумма следующего платежа: 299 ₽" in result.message_text
    assert "2990" not in result.message_text
    assert "5980" not in result.message_text
    assert "EDS" not in result.message_text


def test_renew_skips_stale_eds_subscription_keeps_voice(session) -> None:
    """ПРАВИЛО #7 regression-guard для CRITICAL-2 (is_feature_disabled retired-set).

    Миграция оставляет EDS-планы инертными (FK/история), значит СТЕЙЛ
    TenantSubscription(feature_key="eds_monitor") теоретически может лежать в проде.
    renew_cycle ДОЛЖЕН пропустить её (не продлить И не force-expire) благодаря
    is_feature_disabled("eds_monitor")=True; voice при этом продлевается. Регресс
    is_feature_disabled→always-False продлил/заэкспайрил бы EDS-строку и/или вернул
    EDS-сумму (2990) в сообщение — этот тест бы упал."""
    _seed_mixed_tenant(session)
    # Инертный EDS-план (как в проде — не дропнут) + стейл EDS-подписка, выглядящая renewable
    # (due в прошлом, next_cycle_quantity=1).
    session.add(
        _plan(
            id="plan_eds",
            plan_key="eds_monitor_base",
            feature_key="eds_monitor",
            title="EDS",
            price_rub=2990,
        )
    )
    session.add(
        _subscription(
            id="sub_eds",
            plan_id="plan_eds",
            feature_key="eds_monitor",
            active_until=NOW - timedelta(days=1),
        )
    )
    session.commit()
    eds_before = _snapshot(session.get(TenantSubscription, "sub_eds"))
    voice_before = _snapshot(session.get(TenantSubscription, "sub_voice"))

    result = BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()

    # EDS-подписка byte-for-byte не тронута (пропущена: не продлена, не force-expired).
    assert _snapshot(session.get(TenantSubscription, "sub_eds")) == eds_before
    # Voice продлён.
    assert session.get(TenantSubscription, "sub_voice").active_until > voice_before["active_until"]
    # Сообщение: только voice-сумма, без EDS (2990) и суммы 2990+299.
    assert "299 ₽" in result.message_text
    assert "2990" not in result.message_text
    assert "3289" not in result.message_text


def test_node_load_context_does_not_crash_and_carries_identity_only(session) -> None:
    """``node_load_context`` runs every turn via graph.node_load_context. After
    Phase B it must NOT query any EDS billing summary / feature flag — it only
    carries the routing identity keys, and it must not raise even with a stale
    disabled eds_monitor feature present."""
    _seed_mixed_tenant(session)

    from sreda.runtime import graph as graph_mod
    from sreda.runtime.dispatcher import ActionEnvelope

    envelope = ActionEnvelope(
        action_type="conversation.chat",
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        assistant_id=ASSISTANT,
        user_id="user_mixed",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value="привет",
        params={"text": "привет"},
    )
    config = {"configurable": {"session": session}}
    out = graph_mod.node_load_context({"action": envelope.as_dict()}, config)

    ctx = out["context"]
    assert ctx["tenant_id"] == TENANT
    assert ctx["workspace_id"] == WORKSPACE
    assert ctx["assistant_id"] == ASSISTANT
    # No EDS-era keys leak into the context.
    assert "billing_summary" not in ctx
    assert "eds_monitor_enabled" not in ctx


def test_generic_simple_subscription_voice_still_works(session) -> None:
    """The simple-subscription path (voice/housewife) is unaffected by the EDS
    removal — cancelling voice flows through and mutates the row."""
    _seed_mixed_tenant(session)
    result = BillingService(session).cancel_simple_subscription(
        TENANT, PLAN_VOICE_TRANSCRIPTION
    )
    assert result.message_text != DISABLED_TEXT
    session.expire_all()
    voice = session.get(TenantSubscription, "sub_voice")
    assert voice.status == "cancelled"


# ---------------------------------------------------------------------------
# Display — status / subscriptions render no EDS, keep voice
# ---------------------------------------------------------------------------


def test_build_status_message_keeps_voice_amount_no_eds(session) -> None:
    """A migrated tenant with a renewable voice sub sees the voice amount
    (299 ₽) and no EDS content."""
    _seed_mixed_tenant(session)
    text, _markup = BillingService(session).build_status_message(TENANT, now=NOW)
    assert "Сумма к оплате: 299 ₽" in text
    assert "2990" not in text
    assert "EDS" not in text


def test_build_status_message_suppresses_payment_when_nothing_renews(session) -> None:
    """With no renewable charge (voice cancelled, housewife free) the payment
    block is dropped entirely — no "Сумма к оплате: 0 ₽" line."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)
    billing.cancel_simple_subscription(TENANT, PLAN_VOICE_TRANSCRIPTION)
    session.commit()

    text, _markup = billing.build_status_message(TENANT, now=NOW)
    assert "Сумма к оплате" not in text
    assert "Следующий платеж" not in text
    assert "EDS" not in text


def test_subscriptions_message_has_no_eds_and_no_mutation(session) -> None:
    """``build_subscriptions_message`` renders voice + nav only — no EDS lines,
    no EDS buttons, no ``onboarding:connect_eds`` — and mutates nothing beyond
    the (separately drained) plan self-heal."""
    _seed_mixed_tenant(session)
    billing = BillingService(session)
    billing.ensure_default_plans()
    session.commit()

    engine = session.get_bind()
    counts, listener = _count_mutations(session)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        text, markup = billing.build_subscriptions_message(TENANT, now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert "EDS" not in text
    assert "ЛК" not in text
    assert "Распознавание голоса" in text

    flat = [btn for row in markup.get("inline_keyboard", []) for btn in row]
    callbacks = [b.get("callback_data", "") for b in flat]
    labels = [b.get("text", "") for b in flat]
    assert not any("eds" in (cb or "").lower() for cb in callbacks)
    assert not any("EDS" in lbl for lbl in labels)
    assert "onboarding:connect_eds" not in callbacks
    assert any("Мой статус" in lbl for lbl in labels)

    assert counts == {}, f"build_subscriptions_message mutated DB: {counts}"


def test_subscriptions_show_handler_fallback_no_eds(session, monkeypatch) -> None:
    """End-to-end through the runtime handler: with connect_public_base_url
    unset the handler hits the legacy fallback, which must carry no EDS content."""
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


# ---------------------------------------------------------------------------
# Policy — legacy EDS actions pass through to the tombstoned handler
# ---------------------------------------------------------------------------

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
def test_policy_lets_legacy_eds_action_through(action_type) -> None:
    """The policy no longer emits any legacy "подключи EDS" prompt — every
    legacy EDS action type passes through (returns None) so its tombstoned
    handler answers. Context carries only the identity keys (Phase B
    node_load_context shape)."""
    from sreda.runtime.policy import evaluate_policy

    context = {
        "tenant_id": TENANT,
        "workspace_id": WORKSPACE,
        "assistant_id": ASSISTANT,
    }
    assert evaluate_policy(_policy_envelope(action_type), context) is None


def test_policy_claim_lookup_reaches_handler_tombstone(session) -> None:
    """End-to-end: policy passthrough + handler tombstone → the disabled
    notice, not a claim card / connect prompt."""
    from sreda.runtime import handlers as handlers_mod
    from sreda.runtime.policy import evaluate_policy

    _seed_mixed_tenant(session)
    envelope = _policy_envelope("claim.lookup")
    context = {"tenant_id": TENANT, "workspace_id": WORKSPACE, "assistant_id": ASSISTANT}
    assert evaluate_policy(envelope, context) is None
    replies = handlers_mod.execute_claim_lookup(session, envelope, context)
    assert len(replies) == 1
    assert replies[0].text == DISABLED_TOMBSTONE_TEXT
    assert "EDS" not in replies[0].text


def test_policy_allows_non_eds_action() -> None:
    from sreda.runtime.policy import evaluate_policy

    assert evaluate_policy(_policy_envelope("help.show"), {}) is None
    # A normal authenticated action passes when context is present.
    context = {"tenant_id": TENANT, "workspace_id": WORKSPACE, "assistant_id": ASSISTANT}
    assert evaluate_policy(_policy_envelope("conversation.chat"), context) is None


def test_policy_blocks_when_runtime_context_missing() -> None:
    from sreda.runtime.policy import evaluate_policy

    error = evaluate_policy(_policy_envelope("conversation.chat"), {})
    assert error is not None
    assert error.code == "runtime_context_missing"


# ---------------------------------------------------------------------------
# Registry — generic guard still drops a retired feature module
# ---------------------------------------------------------------------------


def test_registry_registers_non_eds_feature() -> None:
    from sreda.features.registry import FeatureRegistry

    registry = FeatureRegistry()

    async def _handler(*a, **k):
        return None

    registry.register_skill_job_handler(
        feature_key="housewife_assistant", job_type="housewife.reminder", handler=_handler
    )
    assert registry.skill_job_types() == ["housewife.reminder"]


# ---------------------------------------------------------------------------
# Route tombstones (Mini App + /connect/eds)
# ---------------------------------------------------------------------------


@pytest.fixture()
def miniapp_client(monkeypatch, tmp_path):
    """TestClient with in-memory SQLite + a migrated tenant (stale disabled
    eds_monitor feature, NO EDS account/sub rows — those tables are gone)."""
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
    from sreda.db.repositories.seed import SeedRepository
    from sreda.db.session import get_engine, get_session_factory
    from sreda.main import create_app

    _get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()
    _Base.metadata.create_all(get_engine())

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


def test_route_eds_accounts_returns_empty(miniapp_client) -> None:
    resp = miniapp_client.get(
        "/miniapp/api/v1/eds/accounts", headers=miniapp_client._eds_auth_header
    )
    assert resp.status_code == 200
    assert resp.json() == {"accounts": []}


def test_route_eds_connect_tombstone(miniapp_client) -> None:
    resp = miniapp_client.post(
        "/miniapp/api/v1/eds/connect", headers=miniapp_client._eds_auth_header
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["connect_url"] is None
    assert data["message"] == DISABLED_FEATURE_MESSAGE


def test_route_eds_add_and_connect_tombstone(miniapp_client) -> None:
    resp = miniapp_client.post(
        "/miniapp/api/v1/eds/add-and-connect", headers=miniapp_client._eds_auth_header
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["connect_url"] is None


def test_route_subscribe_eds_plan_key_tombstone(miniapp_client) -> None:
    """An old client POSTing an EDS plan_key gets the disabled message (200),
    not a 400/500 and no mutation."""
    resp = miniapp_client.post(
        "/miniapp/api/v1/subscribe",
        headers=miniapp_client._eds_auth_header,
        json={"plan_key": "eds_monitor_base"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["message"] == DISABLED_FEATURE_MESSAGE


def test_route_resume_tombstone(miniapp_client) -> None:
    resp = miniapp_client.post(
        "/miniapp/api/v1/resume",
        headers=miniapp_client._eds_auth_header,
        json={"plan_key": "eds_monitor_base"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_route_connect_eds_token_get_tombstone(miniapp_client) -> None:
    resp = miniapp_client.get("/connect/eds/sometoken")
    assert resp.status_code == 200
    assert DISABLED_TOMBSTONE_TEXT in resp.text


def test_route_connect_eds_token_post_tombstone(miniapp_client) -> None:
    resp = miniapp_client.post(
        "/connect/eds/sometoken",
        data={"login": "u@example.com", "password": "pw"},
    )
    assert resp.status_code == 200
    assert DISABLED_TOMBSTONE_TEXT in resp.text


def test_api_summary_no_eds_card_and_voice_amount(miniapp_client) -> None:
    """``GET /api/v1/summary`` carries no EDS card and an empty
    eds_subscriptions list. With no billing cycle there is nothing to charge."""
    resp = miniapp_client.get(
        "/miniapp/api/v1/summary", headers=miniapp_client._eds_auth_header
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["eds_subscriptions"] == []
    assert data["next_amount_rub"] == 0
    assert data["next_payment_due_at"] is None
    # No EDS card sneaks into the skill lists.
    for card in data["active_skills"] + data["available_skills"]:
        assert card.get("feature_key") != "eds_monitor"


# ---------------------------------------------------------------------------
# Runtime executor — legacy EDS actions land as COMPLETED tombstone runs
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
    """Seed a migrated tenant on the env-backed engine: voice sub + stale
    disabled eds_monitor feature (NO EDS sub/account rows — tables are gone)."""
    from sreda.db.models.core import Assistant, Tenant, TenantFeature, User, Workspace

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
    session.commit()


_RUNTIME_TOMBSTONE_ACTIONS = [
    # (action_type, source_value, params, expected_tombstone_text)
    ("subscription.add_eds", "billing:add_eds_account", {}, DISABLED_TOMBSTONE_TEXT),
    ("subscription.connect_base", "billing:connect_plan:eds_monitor_base", {}, DISABLED_TOMBSTONE_TEXT),
    ("eds.connect.start", "onboarding:connect_eds", {"slot_type": "primary"}, DISABLED_TOMBSTONE_TEXT),
    ("eds.connect.retry", "eds:connect_retry", {"slot_type": "extra"}, DISABLED_TOMBSTONE_TEXT),
    ("claim.lookup", "/claim 6230173", {"claim_id": "6230173"}, DISABLED_TOMBSTONE_TEXT),
]


@pytest.mark.parametrize(
    "action_type,source_value,params,expected_text",
    _RUNTIME_TOMBSTONE_ACTIONS,
    ids=[a[0] for a in _RUNTIME_TOMBSTONE_ACTIONS],
)
def test_runtime_executor_eds_action_completed_tombstone(
    monkeypatch, tmp_path: Path, action_type, source_value, params, expected_text
) -> None:
    """The legacy EDS actions land as COMPLETED runs (not failed) with a
    disabled tombstone reply — and no EDS rows exist to mutate."""
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

        session.expire_all()
        run_status = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one().status
        job_status = session.query(Job).filter(Job.id == queued.job_id).one().status
        outbox_count = session.query(OutboxMessage).count()
    finally:
        session.close()

    assert result == "completed"
    assert run_status == "completed"
    assert job_status == "completed"

    assert len(telegram_client.sent_messages) == 1
    sent_text = telegram_client.sent_messages[0]["text"]
    assert expected_text in sent_text
    markup = telegram_client.sent_messages[0]["reply_markup"] or {}
    flat = [b for row in markup.get("inline_keyboard", []) for b in row]
    assert not any("EDS" in b.get("text", "") for b in flat)
    assert outbox_count == 1


def test_runtime_executor_renew_cycle_renews_voice_no_eds(
    monkeypatch, tmp_path: Path
) -> None:
    """End-to-end through the executor: a migrated tenant renewing voice lands
    as a COMPLETED run, the reply renews voice (299 ₽) and carries NO EDS amount."""
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
        run_status = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one().status
        job_status = session.query(Job).filter(Job.id == queued.job_id).one().status
        voice_after = _snapshot(session.get(TenantSubscription, "sub_voice"))
    finally:
        session.close()

    assert result == "completed"
    assert run_status == "completed"
    assert job_status == "completed"

    assert voice_after["status"] == "active"
    assert _coerce_naive(voice_after["active_until"]) > _coerce_naive(datetime.now(UTC))

    assert len(telegram_client.sent_messages) == 1
    sent_text = telegram_client.sent_messages[0]["text"]
    assert "Подписка продлена." in sent_text
    assert "299" in sent_text
    assert "2990" not in sent_text
    assert "EDS" not in sent_text
