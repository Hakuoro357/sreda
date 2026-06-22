"""Phase 2 services unit tests: usage_ledger, signup_abuse,
entitlement_gate, audio_probe, upgrade_copy.

Strategy:
- SQLite in-memory engine + Base.metadata.create_all() для DB-backed
  services (usage_ledger, signup_abuse, entitlement_gate).
- audio_probe тестируется через mocked subprocess + real ffprobe
  если binary в PATH (skipif).
- upgrade_copy — pure constants module, smoke test.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import (  # noqa: F401 — registers all
    SignupAttempt,
    SubscriptionPlan,
    Tenant,
    TenantSubscription,
    UsageLedger,
)


_NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Required env-vars that production code grabs at runtime."""
    if not os.environ.get("SREDA_TG_ACCOUNT_SALT"):
        monkeypatch.setenv(
            "SREDA_TG_ACCOUNT_SALT",
            "test-salt-for-phase2-do-not-use-in-prod",
        )
    if not os.environ.get("SREDA_ENCRYPTION_KEY"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "0" * 64)
    if not os.environ.get("SREDA_ENCRYPTION_KEY_ID"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "primary")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _seed_tenant(session, tenant_id: str, *, approved: bool = True) -> None:
    session.add(Tenant(
        id=tenant_id, name=tenant_id,
        created_at=_NOW,
        approved_at=_NOW if approved else None,
    ))
    session.commit()


def _seed_plan(session, plan_key: str, **kwargs) -> SubscriptionPlan:
    defaults = dict(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=plan_key,
        feature_key="housewife_assistant",
        title=plan_key,
        description="",
        price_rub=0,
        billing_period_days=30,
        is_public=True,
        is_active=True,
        sort_order=100,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    plan = SubscriptionPlan(**defaults)
    session.add(plan)
    session.commit()
    return plan


def _seed_subscription(
    session, *, sub_id, tenant_id, plan, status="active",
    grandfathered=False,
) -> TenantSubscription:
    sub = TenantSubscription(
        id=sub_id, tenant_id=tenant_id, plan_id=plan.id,
        feature_key=plan.feature_key, status=status,
        starts_at=_NOW, active_until=None,
        grandfathered_at=_NOW if grandfathered else None,
        cancel_at_period_end=False, quantity=1, next_cycle_quantity=1,
        created_at=_NOW, updated_at=_NOW,
    )
    session.add(sub)
    session.commit()
    return sub


# ---------------------------------------------------------------- upgrade_copy


def test_upgrade_copy_keys_present():
    from sreda.services.upgrade_copy import UPGRADE_COPY, UPGRADE_CONTACT
    expected_keys = {
        "llm_daily_or_monthly", "voice_daily_or_monthly",
        "no_active_subscription", "suspended",
        "signups_closed", "free_tier_full", "rate_limited",
    }
    assert expected_keys.issubset(UPGRADE_COPY.keys())
    assert all(UPGRADE_CONTACT in v for v in [
        UPGRADE_COPY["llm_daily_or_monthly"],
        UPGRADE_COPY["voice_daily_or_monthly"],
        UPGRADE_COPY["no_active_subscription"],
    ])


# ---------------------------------------------------------------- audio_probe


def test_audio_probe_missing_ffprobe_raises():
    """No ffprobe в PATH → FfprobeError."""
    from sreda.services.audio_probe import FfprobeError, ffprobe_duration
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(FfprobeError, match="ffprobe binary not found"):
            ffprobe_duration(b"\x00\x01\x02")


def test_audio_probe_non_zero_exit_raises():
    """ffprobe returncode != 0 → FfprobeError."""
    from sreda.services.audio_probe import FfprobeError, ffprobe_duration

    class FakeProc:
        returncode = 1
        stdout = b""
        stderr = b"Invalid data found"

    with patch("subprocess.run", return_value=FakeProc):
        with pytest.raises(FfprobeError, match="exited 1"):
            ffprobe_duration(b"junk")


def test_audio_probe_missing_duration_uses_byte_estimate():
    """2026-05-08: ffprobe вывод без format.duration AND streams.duration
    → fallback на byte-estimate (16 kbps OGG/Opus voice = 2 KB/sec).

    Triggered by tenant_max_142322319 prod incident — MAX containers
    sometimes don't set duration. Раньше → FfprobeError → юзер видел
    «не получилось обработать голосовое». Теперь — оценка по размеру.
    """
    from sreda.services.audio_probe import ffprobe_duration

    class FakeProc:
        returncode = 0
        stdout = b'{"format": {}, "streams": []}'  # both missing
        stderr = b""

    # 4500 bytes / 1500 bytes-per-sec (12 kbps Opus) = 3.0s estimate
    with patch("subprocess.run", return_value=FakeProc):
        result = ffprobe_duration(b"x" * 4500)
        assert result == pytest.approx(3.0)


def test_audio_probe_returns_float_seconds():
    """Mocked ffprobe with duration='12.34' → 12.34 float."""
    from sreda.services.audio_probe import ffprobe_duration

    class FakeProc:
        returncode = 0
        stdout = b'{"format": {"duration": "12.34"}}'
        stderr = b""

    with patch("subprocess.run", return_value=FakeProc):
        result = ffprobe_duration(b"x")
    assert result == pytest.approx(12.34)


@pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="ffprobe binary not installed (skipped in CI without ffmpeg)",
)
def test_audio_probe_real_ffprobe_garbage_input_raises():
    """Real ffprobe on garbage bytes → FfprobeError (not crash)."""
    from sreda.services.audio_probe import FfprobeError, ffprobe_duration
    with pytest.raises(FfprobeError):
        ffprobe_duration(b"\x00" * 100)


# ---------------------------------------------------------------- usage_ledger


def test_usage_ledger_try_consume_basic(engine, session):
    """First consume creates row; subsequent increments."""
    _seed_tenant(session, "t_ll1")
    from sreda.services.usage_ledger import UsageLedgerService
    ledger = UsageLedgerService(engine)

    assert ledger.try_consume(
        "t_ll1", "llm_turns", 1,
        [("daily", "2026-05-08", 20), ("monthly", "2026-05", 200)],
    )
    assert ledger.try_consume(
        "t_ll1", "llm_turns", 5,
        [("daily", "2026-05-08", 20), ("monthly", "2026-05", 200)],
    )

    # Verify state
    rows = session.execute(
        UsageLedger.__table__.select().where(
            UsageLedger.tenant_id == "t_ll1"
        )
    ).fetchall()
    daily = next(r for r in rows if r.period_type == "daily")
    monthly = next(r for r in rows if r.period_type == "monthly")
    assert float(daily.used) == 6
    assert float(monthly.used) == 6


def test_usage_ledger_insert_path_quota_check(engine, session):
    """First request с delta > quota — INSERT path должен reject."""
    _seed_tenant(session, "t_ll2")
    from sreda.services.usage_ledger import UsageLedgerService
    ledger = UsageLedgerService(engine)

    # delta=25, quota=20 — должно reject не создавая row
    assert not ledger.try_consume(
        "t_ll2", "llm_turns", 25,
        [("daily", "2026-05-08", 20)],
    )
    # No row created
    rows = session.execute(
        UsageLedger.__table__.select().where(
            UsageLedger.tenant_id == "t_ll2"
        )
    ).fetchall()
    assert len(rows) == 0


def test_usage_ledger_all_or_nothing_across_periods(engine, session):
    """Если monthly exceeds но daily passes → both rolled back.

    Setup: directly seed monthly row близко к лимиту bypass'я
    try_consume's daily quota check (квота daily=20, нельзя pre-fill
    199 через try_consume).
    """
    _seed_tenant(session, "t_ll3")
    # Direct seed: monthly row at 199/200
    session.add(UsageLedger(
        tenant_id="t_ll3", metric="llm_turns",
        period_type="monthly", period_key="2026-05",
        used=199, quota_snapshot=200, updated_at=_NOW,
    ))
    session.commit()

    from sreda.services.usage_ledger import UsageLedgerService
    ledger = UsageLedgerService(engine)

    # Try to consume 5 — daily 2026-05-09 fits (5<=20), monthly fails
    # (199+5=204 > 200) → expect rollback both.
    assert not ledger.try_consume(
        "t_ll3", "llm_turns", 5,
        [("daily", "2026-05-09", 20), ("monthly", "2026-05", 200)],
    )
    # Verify daily 2026-05-09 NOT created (rolled back с monthly fail)
    daily_row = session.execute(
        UsageLedger.__table__.select().where(
            UsageLedger.tenant_id == "t_ll3",
            UsageLedger.period_key == "2026-05-09",
        )
    ).first()
    assert daily_row is None
    # Verify monthly used unchanged (199, не 204)
    session.expire_all()
    monthly_row = session.execute(
        UsageLedger.__table__.select().where(
            UsageLedger.tenant_id == "t_ll3",
            UsageLedger.period_key == "2026-05",
        )
    ).first()
    assert float(monthly_row.used) == 199


def test_usage_ledger_refund_decrements(engine, session):
    """refund() decrements counters; floors at 0."""
    _seed_tenant(session, "t_ll4")
    from sreda.services.usage_ledger import UsageLedgerService
    ledger = UsageLedgerService(engine)

    periods = [("daily", "2026-05-08", 20), ("monthly", "2026-05", 200)]
    ledger.try_consume("t_ll4", "voice_stt_seconds", 60, periods)
    ledger.refund("t_ll4", "voice_stt_seconds", 60, periods)

    rows = session.execute(
        UsageLedger.__table__.select().where(
            UsageLedger.tenant_id == "t_ll4"
        )
    ).fetchall()
    for r in rows:
        assert float(r.used) == 0


def test_msk_period_keys_helper():
    """msk_period_keys returns ('YYYY-MM-DD', 'YYYY-MM') in MSK."""
    from sreda.services.usage_ledger import msk_period_keys
    daily, monthly = msk_period_keys()
    assert len(daily) == 10  # YYYY-MM-DD
    assert len(monthly) == 7  # YYYY-MM


# ---------------------------------------------------------------- signup_abuse


def test_signup_abuse_hash_deterministic():
    """hmac_signup_source returns same hash для same input."""
    from sreda.services.signup_abuse import hmac_signup_source
    h1 = hmac_signup_source("352612382")
    h2 = hmac_signup_source(352612382)  # int → str normalized
    assert h1 == h2
    assert len(h1) == 64  # hex sha256


def test_signup_abuse_kill_switch(session):
    """signup_open=False → blocks с reason='signups_closed'."""
    from sreda.services.signup_abuse import SignupAbuseGuard
    guard = SignupAbuseGuard(signup_open=False)
    allowed, reason = guard.check_inside_tx(session, "telegram", "999")
    assert not allowed
    assert reason == "signups_closed"


def test_signup_abuse_free_tier_full(session):
    """Достижение free_tier_active_max → 'free_tier_full'."""
    from sreda.services.signup_abuse import SignupAbuseGuard

    plan = _seed_plan(session, "sreda_free", sort_order=0)
    _seed_tenant(session, "t_full_1")
    _seed_subscription(
        session, sub_id="sub_full_1", tenant_id="t_full_1", plan=plan,
    )

    guard = SignupAbuseGuard(free_tier_active_max=1)  # cap=1, 1 already active
    allowed, reason = guard.check_inside_tx(session, "telegram", "777")
    assert not allowed
    assert reason == "free_tier_full"


def test_signup_abuse_grandfathered_excluded_from_cap(session):
    """#200: grandfathered active sreda_free НЕ занимает free-место → cap не сработал."""
    from sreda.services.signup_abuse import SignupAbuseGuard

    plan = _seed_plan(session, "sreda_free", sort_order=0)
    _seed_tenant(session, "t_gf_1")
    _seed_subscription(
        session, sub_id="sub_gf_1", tenant_id="t_gf_1", plan=plan,
        grandfathered=True,  # безлимит штатно — не «бесплатная liability под потолком»
    )

    guard = SignupAbuseGuard(free_tier_active_max=1)  # cap=1, но единственная active — grandfathered
    allowed, reason = guard.check_inside_tx(session, "telegram", "778")
    assert allowed, f"grandfathered не должен занимать free-место, но reason={reason}"


def test_signup_abuse_rate_limited(session):
    """4-я попытка с одного source за 24h → 'rate_limited'."""
    from sreda.services.signup_abuse import SignupAbuseGuard, hmac_signup_source

    # Pre-seed 3 attempts (max=3 разрешает 3, 4-я reject'нет)
    h = hmac_signup_source("rate_test")
    for _ in range(3):
        session.add(SignupAttempt(
            channel="telegram", source_id_hash=h, attempted_at=_NOW,
        ))
    session.commit()

    guard = SignupAbuseGuard(free_tier_active_max=100)
    allowed, reason = guard.check_inside_tx(session, "telegram", "rate_test")
    assert not allowed
    assert reason == "rate_limited"


def test_signup_abuse_ok_path_records_attempt(session):
    """Allowed path inserts signup_attempts row."""
    from sreda.services.signup_abuse import SignupAbuseGuard

    guard = SignupAbuseGuard(free_tier_active_max=100)
    allowed, reason = guard.check_inside_tx(session, "telegram", "fresh_user")
    assert allowed
    assert reason == "ok"

    rows = session.execute(
        SignupAttempt.__table__.select().where(
            SignupAttempt.channel == "telegram"
        )
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------- entitlement_gate


def test_entitlement_gate_active_subscription_passes(session):
    """Active sub → allowed=True, reason='ok', plan_key returned."""
    from sreda.services.entitlement_gate import EntitlementGate

    plan = _seed_plan(session, "sreda_free", sort_order=0)
    _seed_tenant(session, "t_gate1")
    _seed_subscription(
        session, sub_id="sub_g1", tenant_id="t_gate1", plan=plan,
    )

    gate = EntitlementGate(session)
    result = gate.check("t_gate1")
    assert result.allowed
    assert result.plan_key == "sreda_free"
    assert not result.is_grandfathered


def test_entitlement_gate_grandfathered_detected(session):
    """grandfathered_at IS NOT NULL → is_grandfathered=True."""
    from sreda.services.entitlement_gate import EntitlementGate

    plan = _seed_plan(session, "housewife_grandfathered", sort_order=999)
    _seed_tenant(session, "t_gate2")
    _seed_subscription(
        session, sub_id="sub_g2", tenant_id="t_gate2", plan=plan,
        grandfathered=True,
    )

    gate = EntitlementGate(session)
    result = gate.check("t_gate2")
    assert result.allowed
    assert result.is_grandfathered


def test_entitlement_gate_suspended_blocks(session):
    """Status='suspended' → allowed=False, reason='suspended'."""
    from sreda.services.entitlement_gate import EntitlementGate

    plan = _seed_plan(session, "sreda_free", sort_order=0)
    _seed_tenant(session, "t_gate3")
    _seed_subscription(
        session, sub_id="sub_g3", tenant_id="t_gate3", plan=plan,
        status="suspended",
    )

    gate = EntitlementGate(session)
    result = gate.check("t_gate3")
    assert not result.allowed
    assert result.reason == "suspended"


def test_entitlement_gate_no_subscription_blocks(session):
    """Tenant без subscription → allowed=False, reason='no_active_subscription'."""
    from sreda.services.entitlement_gate import EntitlementGate

    _seed_tenant(session, "t_gate4")

    gate = EntitlementGate(session)
    result = gate.check("t_gate4")
    assert not result.allowed
    assert result.reason == "no_active_subscription"
