"""Phase 6 — per-bot onboarding matrix unit tests.

Matrix under test (plans/second-tg-bot-final.md § Phase 6):

| case                        | sreda (old)       | sreda_home (new/main)       |
|-----------------------------|-------------------|-----------------------------|
| tenant moderation           | auto-approve      | auto-approve (same today)   |
| signup_open                 | per-bot (can be   | per-bot (open by default)   |
|                             | closed)           |                             |
| capacity (free-tier cap)    | kept              | kept                        |
| rate-limit (anti-spam)      | kept              | kept                        |
| existing pending tenant     | auto-unlocked     | auto-unlocked (Model B      |
|                             |                   | shared tenant)              |
| existing approved           | unchanged         | unchanged                   |

GROUNDING NOTE: Both bots already auto-approve today (Phase 2C
`_auto_approve_and_grant_free_tier` called for all signups). The real
matrix delta introduced by Phase 6 is:
  - per-bot signup_open (global kill-switch still applies first)
  - bot_key threaded into onboarding so the guard reads BotConfig.signup_open

Strategy: SQLite in-memory + direct SignupAbuseGuard construction (no
RuntimeConfig DB dependency) for lower-level tests; full onboarding flow
for higher-level matrix cells.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import (  # noqa: F401 — registers core tables
    SignupAttempt,
    SubscriptionPlan,
    Tenant,
    TenantSubscription,
)
import sreda.db.models.audit  # noqa: F401
import sreda.db.models.checklists  # noqa: F401
import sreda.db.models.free_tier  # noqa: F401
import sreda.db.models.reply_buttons  # noqa: F401


_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Required env-vars pulled at runtime by onboarding / hash services."""
    for key, val in [
        ("SREDA_TG_ACCOUNT_SALT", "phase6-test-salt-do-not-use-in-prod"),
        ("SREDA_ENCRYPTION_KEY", "0" * 64),
        ("SREDA_ENCRYPTION_KEY_ID", "primary"),
        ("SREDA_TELEGRAM_BOT_TOKEN", "111:AAA"),
        ("SREDA_TELEGRAM_BOT_USERNAME", "SredaBot"),
    ]:
        if not os.environ.get(key):
            monkeypatch.setenv(key, val)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    Sess = sessionmaker(bind=engine)
    s = Sess()
    try:
        yield s
    finally:
        s.close()


def _seed_plan(session) -> SubscriptionPlan:
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key="sreda_free",
        feature_key="housewife_assistant",
        title="Sreda Free",
        description="",
        price_rub=0,
        billing_period_days=30,
        is_public=True,
        is_active=True,
        sort_order=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(plan)
    session.commit()
    return plan


def _seed_active_sub(session, plan, tenant_id: str) -> TenantSubscription:
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        plan_id=plan.id,
        feature_key=plan.feature_key,
        status="active",
        starts_at=_NOW,
        active_until=None,
        cancel_at_period_end=False,
        quantity=1,
        next_cycle_quantity=1,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(sub)
    session.commit()
    return sub


def _make_registry(
    *,
    sreda_signup_open: bool = True,
    sreda_home_signup_open: bool = True,
):
    """Build a minimal TelegramBotRegistry for testing without env quirks."""
    from sreda.config.bot_registry import BotConfig, TelegramBotRegistry

    bots = [
        BotConfig(
            key="sreda",
            token="111:AAA",
            username="SredaBot",
            signup_open=sreda_signup_open,
        ),
        BotConfig(
            key="sreda_home",
            token="222:BBB",
            username="SredaHomeBot",
            signup_open=sreda_home_signup_open,
        ),
    ]
    return TelegramBotRegistry(bots)


# ---------------------------------------------------------------------------
# SignupAbuseGuard — per-bot signup_open unit tests
# ---------------------------------------------------------------------------


class TestSignupAbuseGuardPerBot:
    """Direct guard tests — no onboarding stack, no runtime_config."""

    def test_global_open_bot_open_passes(self, session):
        """Global=True, per-bot=True → passes abuse checks."""
        from sreda.services.signup_abuse import SignupAbuseGuard

        guard = SignupAbuseGuard(
            signup_open=True,
            bot_signup_open=True,
            free_tier_active_max=100,
        )
        allowed, reason = guard.check_inside_tx(session, "telegram", "user_1")
        assert allowed
        assert reason == "ok"

    def test_global_kill_switch_blocks_regardless_of_bot(self, session):
        """Global kill-switch=False → blocks even if bot signup_open=True."""
        from sreda.services.signup_abuse import SignupAbuseGuard

        guard = SignupAbuseGuard(
            signup_open=False,      # global kill-switch
            bot_signup_open=True,   # per-bot open
            free_tier_active_max=100,
        )
        allowed, reason = guard.check_inside_tx(session, "telegram", "user_2")
        assert not allowed
        assert reason == "signups_closed"

    def test_per_bot_closed_blocks_that_bot(self, session):
        """Per-bot=False → blocks while global is open."""
        from sreda.services.signup_abuse import SignupAbuseGuard

        guard = SignupAbuseGuard(
            signup_open=True,        # global open
            bot_signup_open=False,   # this bot closed
            free_tier_active_max=100,
        )
        allowed, reason = guard.check_inside_tx(session, "telegram", "user_3")
        assert not allowed
        assert reason == "signups_closed"

    def test_global_closed_bot_open_still_blocked(self, session):
        """Confirm: global closed + bot open → still blocked (kill-switch wins)."""
        from sreda.services.signup_abuse import SignupAbuseGuard

        guard = SignupAbuseGuard(
            signup_open=False,
            bot_signup_open=True,
            free_tier_active_max=100,
        )
        allowed, reason = guard.check_inside_tx(session, "telegram", "user_4")
        assert not allowed
        assert reason == "signups_closed"

    def test_capacity_hit_blocks_regardless_of_bot(self, session):
        """Capacity check is bot-agnostic — cap hit blocks sreda_home too."""
        from sreda.services.signup_abuse import SignupAbuseGuard

        plan = _seed_plan(session)
        t = Tenant(id="t_cap", name="t_cap", created_at=_NOW, approved_at=_NOW)
        session.add(t)
        session.commit()
        _seed_active_sub(session, plan, "t_cap")

        guard = SignupAbuseGuard(
            signup_open=True,
            bot_signup_open=True,
            free_tier_active_max=1,  # cap=1, already 1 active
        )
        allowed, reason = guard.check_inside_tx(session, "telegram", "new_user")
        assert not allowed
        assert reason == "free_tier_full"

    def test_rate_limit_hit_blocks_regardless_of_bot(self, session):
        """Rate-limit check is bot-agnostic — hit blocks sreda_home too."""
        from sreda.services.signup_abuse import SignupAbuseGuard, hmac_signup_source

        h = hmac_signup_source("spammer_99")
        for _ in range(3):
            session.add(SignupAttempt(
                channel="telegram",
                source_id_hash=h,
                attempted_at=_NOW,
            ))
        session.commit()

        guard = SignupAbuseGuard(
            signup_open=True,
            bot_signup_open=True,
            free_tier_active_max=100,
        )
        allowed, reason = guard.check_inside_tx(session, "telegram", "spammer_99")
        assert not allowed
        assert reason == "rate_limited"


# ---------------------------------------------------------------------------
# Onboarding matrix — full stack via ensure_telegram_user_bundle_by_id
# ---------------------------------------------------------------------------


class TestOnboardingMatrix:
    """Full-stack tests for each matrix cell.

    We patch TelegramBotRegistry.from_settings so tests are independent
    of real env-var loading for the second bot token.
    """

    def _patch_registry(self, **kwargs):
        """Return a context manager patching registry resolution."""
        registry = _make_registry(**kwargs)
        return patch(
            "sreda.config.bot_registry.TelegramBotRegistry.from_settings",
            return_value=registry,
        )

    def _patch_runtime_config(self, session, signup_open_global: bool = True):
        """Stub RuntimeConfig so from_runtime_config reads a known value."""
        raw_value = "true" if signup_open_global else "false"

        def _fake_get_config(sess, key):
            if key == "sreda_signup_open":
                return raw_value
            if key == "sreda_free_tier_active_max":
                return "100"
            return None

        return patch(
            "sreda.services.runtime_config.get_config",
            side_effect=_fake_get_config,
        )

    def test_new_user_sreda_home_signup_open_true_onboarded(self, session):
        """New user via sreda_home with signup_open=True → onboarded + approved."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id

        with self._patch_registry(sreda_home_signup_open=True), \
             self._patch_runtime_config(session):
            result = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id="111111",
                display_name="New Home User",
                bot_key="sreda_home",
            )

        assert result.is_new_user is True
        assert result.tenant_id is not None

        # Tenant must be auto-approved (approved_at set)
        tenant = session.get(Tenant, result.tenant_id)
        assert tenant is not None
        assert tenant.approved_at is not None

    def test_new_user_sreda_home_signup_open_false_blocked(self, session):
        """Per-bot signup_open=False for sreda_home → blocked."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.signup_abuse import SignupBlocked

        with self._patch_registry(sreda_home_signup_open=False), \
             self._patch_runtime_config(session):
            with pytest.raises(SignupBlocked) as exc_info:
                ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id="222222",
                    bot_key="sreda_home",
                )

        assert exc_info.value.reason == "signups_closed"

    def test_sreda_bot_signup_open_unaffected_when_sreda_home_closed(self, session):
        """sreda bot can still onboard when sreda_home is closed."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id

        with self._patch_registry(sreda_signup_open=True, sreda_home_signup_open=False), \
             self._patch_runtime_config(session):
            result = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id="333333",
                display_name="Sreda User",
                bot_key="sreda",
            )

        assert result.is_new_user is True

    def test_global_kill_switch_blocks_sreda_home(self, session):
        """Global sreda_signup_open=False → blocks sreda_home regardless of per-bot."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.signup_abuse import SignupBlocked

        with self._patch_registry(sreda_home_signup_open=True), \
             self._patch_runtime_config(session, signup_open_global=False):
            with pytest.raises(SignupBlocked) as exc_info:
                ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id="444444",
                    bot_key="sreda_home",
                )

        assert exc_info.value.reason == "signups_closed"

    def test_global_kill_switch_blocks_sreda_too(self, session):
        """Global sreda_signup_open=False → blocks old sreda bot too."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.signup_abuse import SignupBlocked

        with self._patch_registry(sreda_signup_open=True), \
             self._patch_runtime_config(session, signup_open_global=False):
            with pytest.raises(SignupBlocked) as exc_info:
                ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id="555555",
                    bot_key="sreda",
                )

        assert exc_info.value.reason == "signups_closed"

    def test_capacity_hit_blocks_sreda_home(self, session):
        """Capacity cap blocks sreda_home (anti-abuse kept, bot-agnostic)."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.signup_abuse import SignupBlocked

        # Seed capacity cap at 1, with 1 already active
        plan = _seed_plan(session)
        t = Tenant(id="t_cap2", name="cap", created_at=_NOW, approved_at=_NOW)
        session.add(t)
        session.commit()
        _seed_active_sub(session, plan, "t_cap2")

        def _fake_get_config(sess, key):
            if key == "sreda_signup_open":
                return "true"
            if key == "sreda_free_tier_active_max":
                return "1"  # cap=1, already 1 active
            return None

        with self._patch_registry(sreda_home_signup_open=True), \
             patch("sreda.services.runtime_config.get_config", side_effect=_fake_get_config):
            with pytest.raises(SignupBlocked) as exc_info:
                ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id="666666",
                    bot_key="sreda_home",
                )

        assert exc_info.value.reason == "free_tier_full"

    def test_rate_limit_blocks_sreda_home(self, session):
        """Rate-limit blocks sreda_home (anti-abuse kept, bot-agnostic)."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.signup_abuse import SignupBlocked, hmac_signup_source

        # Pre-seed 3 attempts for this source
        h = hmac_signup_source("777777")
        for _ in range(3):
            session.add(SignupAttempt(
                channel="telegram",
                source_id_hash=h,
                attempted_at=_NOW,
            ))
        session.commit()

        with self._patch_registry(sreda_home_signup_open=True), \
             self._patch_runtime_config(session):
            with pytest.raises(SignupBlocked) as exc_info:
                ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id="777777",
                    bot_key="sreda_home",
                )

        assert exc_info.value.reason == "rate_limited"

    def test_existing_pending_tenant_unlocked_via_sreda_home(self, session):
        """Existing pending tenant → auto-unlocked on contact via sreda_home.

        Model B shared tenant: the same tenant record is used regardless of
        which bot the user contacts. ensure_telegram_user_bundle_by_id finds
        the existing user (pending) and calls _auto_approve_and_grant_free_tier,
        which sets approved_at. signup_open / abuse guard NOT called for existing
        users — they bypass the guard entirely.
        """
        from sreda.db.models.core import User, Workspace, Assistant
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.tg_account_hash import hash_tg_account

        # Seed a pending tenant (approved_at=None)
        tenant_id = "tenant_pending_888"
        workspace_id = "workspace_pending_888"
        user_id = "user_pending_888"
        assistant_id = "assistant_pending_888"
        chat_id = "888888"
        tg_hash = hash_tg_account(chat_id)

        session.add(Tenant(id=tenant_id, name="Pending", created_at=_NOW, approved_at=None))
        session.add(Workspace(id=workspace_id, tenant_id=tenant_id, name="W"))
        session.add(User(
            id=user_id, tenant_id=tenant_id,
            tg_account_hash=tg_hash,
        ))
        session.add(Assistant(
            id=assistant_id, tenant_id=tenant_id, workspace_id=workspace_id,
            name="Среда",
        ))
        session.commit()

        # Seed sreda_free plan so auto_approve can grant it
        _seed_plan(session)

        with self._patch_registry(sreda_home_signup_open=True):
            # sreda_home contact — existing user found, pending unlocked
            result = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id=chat_id,
                bot_key="sreda_home",
            )

        # Not a new user — existing tenant found
        assert result.is_new_user is False
        assert result.tenant_id == tenant_id

        # Tenant must now be approved (pending → unlocked)
        session.expire_all()
        tenant = session.get(Tenant, tenant_id)
        assert tenant.approved_at is not None

    def test_existing_approved_tenant_returned_unchanged(self, session):
        """Existing approved tenant → returned as-is, no guard called."""
        from sreda.db.models.core import User, Workspace, Assistant
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.tg_account_hash import hash_tg_account

        tenant_id = "tenant_approved_999"
        workspace_id = "workspace_approved_999"
        user_id = "user_approved_999"
        assistant_id = "assistant_approved_999"
        chat_id = "999999"
        tg_hash = hash_tg_account(chat_id)

        session.add(Tenant(id=tenant_id, name="Approved", created_at=_NOW, approved_at=_NOW))
        session.add(Workspace(id=workspace_id, tenant_id=tenant_id, name="W"))
        session.add(User(
            id=user_id, tenant_id=tenant_id,
            tg_account_hash=tg_hash,
        ))
        session.add(Assistant(
            id=assistant_id, tenant_id=tenant_id, workspace_id=workspace_id,
            name="Среда",
        ))
        session.commit()
        _seed_plan(session)

        # Even with sreda_home signup_open=False — existing users bypass guard
        with self._patch_registry(sreda_home_signup_open=False):
            result = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id=chat_id,
                bot_key="sreda_home",
            )

        assert result.is_new_user is False
        assert result.tenant_id == tenant_id

    def test_inbound_stamps_last_bot_key_on_existing_user(self, session):
        """#109: existing migrated user contacting sreda_home gets
        user.last_bot_key='sreda_home' so async producers route to it."""
        from sreda.db.models.core import User, Workspace, Assistant
        from sreda.services.onboarding import ensure_telegram_user_bundle
        from sreda.services.tg_account_hash import hash_tg_account

        tenant_id = "tenant_mig_109"
        chat_id = "755682022"
        tg_hash = hash_tg_account(chat_id)
        session.add(Tenant(id=tenant_id, name="Migrated", created_at=_NOW, approved_at=_NOW))
        session.add(Workspace(id="ws_mig_109", tenant_id=tenant_id, name="W"))
        session.add(User(
            id="user_mig_109", tenant_id=tenant_id,
            tg_account_hash=tg_hash,
            last_bot_key="sreda",  # was last on the OLD bot
        ))
        session.add(Assistant(
            id="ast_mig_109", tenant_id=tenant_id, workspace_id="ws_mig_109",
            name="Среда",
        ))
        session.commit()

        payload = {"message": {"chat": {"id": int(chat_id)}, "text": "привет"}}
        with self._patch_registry():
            result = ensure_telegram_user_bundle(
                session, payload, bot_key="sreda_home",
            )

        assert result.is_new_user is False
        session.expire_all()
        user = session.get(User, "user_mig_109")
        assert user.last_bot_key == "sreda_home"

    def test_inbound_stamps_last_bot_key_on_new_user(self, session):
        """#109: a brand-new user's first inbound stamps last_bot_key."""
        from sreda.db.models.core import User
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id

        _seed_plan(session)
        with self._patch_registry(sreda_home_signup_open=True), \
             self._patch_runtime_config(session):
            result = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id="709109109",
                display_name="Fresh User",
                bot_key="sreda_home",
            )

        assert result.is_new_user is True
        user = session.get(User, result.user_id)
        assert user.last_bot_key == "sreda_home"

    def test_unknown_bot_key_fails_closed(self, session):
        """Unknown bot_key → SignupBlocked (fail-closed, not KeyError bubble)."""
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
        from sreda.services.signup_abuse import SignupBlocked

        with self._patch_registry():
            with pytest.raises(SignupBlocked) as exc_info:
                ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id="123456",
                    bot_key="unknown_bot",
                )

        assert exc_info.value.reason == "signups_closed"

    def test_old_sreda_bot_behavior_unchanged(self, session):
        """Old bot sreda: new user onboarded with default signup_open=True.

        Regression: Phase 6 must not change the sreda bot's existing
        auto-approve + free-tier-grant behavior.
        """
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id

        with self._patch_registry(sreda_signup_open=True), \
             self._patch_runtime_config(session):
            result = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id="100200300",
                display_name="Legacy User",
                bot_key="sreda",
            )

        assert result.is_new_user is True
        assert result.tenant_id is not None

        tenant = session.get(Tenant, result.tenant_id)
        assert tenant is not None
        assert tenant.approved_at is not None

    def test_model_b_shared_tenant_sreda_home_sees_same_tenant(self, session):
        """Model B: user registered via sreda sees same tenant via sreda_home.

        After first contact via sreda, contact via sreda_home returns the
        SAME tenant_id (shared tenant model). Abuse guard is NOT called for
        existing users — no double-counting of capacity.
        """
        from sreda.services.onboarding import ensure_telegram_user_bundle_by_id

        chat_id = "555666777"

        # First contact via sreda
        with self._patch_registry(), self._patch_runtime_config(session):
            result_a = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id=chat_id,
                display_name="Shared User",
                bot_key="sreda",
            )

        assert result_a.is_new_user is True
        tenant_id_a = result_a.tenant_id

        # Second contact via sreda_home — same tenant, no new signup
        with self._patch_registry(sreda_home_signup_open=True):
            result_b = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id=chat_id,
                bot_key="sreda_home",
            )

        assert result_b.is_new_user is False
        assert result_b.tenant_id == tenant_id_a
