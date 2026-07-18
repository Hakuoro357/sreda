"""#204 Фаза 3 — снять 2 легаси voice-подписки (DATA, форвард-онли).

Прод-состояние (после Ф1+Ф2): план ``voice_transcription_base`` —
tombstone (``is_active=False``, миграция 0018); ровно 2 тенанта всё ещё
держат active voice-подписку — ``tenant_tg_1089832184`` и
``tenant_tg_755682022`` (у обоих параллельно active ``housewife_assistant``,
который теперь и несёт голос — атрибуция Ф1). Ф3 снимает 2 легаси voice-
подписки ИДЕМПОТЕНТНО, зеркаля ``cancel_voice_subscription`` (billing.py),
с жёсткими preconditions: любое расхождение прод-состояния (дрейф) → STOP,
НИЧЕГО не мутируется.

🔴 Тест целиком на СИНТЕТИЧЕСКИХ данных (in-memory SQLite) — НЕ зависит от
прод-env. Зовёт чистую функцию ``cancel_legacy_voice_subs(session, *,
dry_run=False)``, чтобы биллинг-мутацию можно было прогнать без
``/etc/sreda/.env``.

Покрытие (цитаты чеклиста V3/V8 + plan Фаза 3):
- precondition-happy: 2 active voice + housewife у обоих → обе сняты
  (полные поля: status/quantity/next_cycle_quantity/cancel_at_period_end/
  feature-flag), postcondition active voice==0, housewife цел.
- precondition-drift: 3 active voice / не тот tenant-set / нет housewife →
  STOP, НИЧЕГО не снято (ассерт неизменности).
- идемпотентность: повторный прогон на уже-снятых → no-op (precondition
  «ровно 2 active» не прошёл, без исключения).
- 🔴 анти-over-reach (c-010): cancel НЕ трогает housewife/чужие подписки.
- dry-run: считает мутации, но НЕ коммитит (rollback) — для прод-проверки.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sreda.services.agent_capabilities as agent_capabilities
from sreda.db.base import Base
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.core import Tenant, TenantFeature

# Load the script under test by file path. The module filename starts with a
# digit (``204_cancel_legacy_voice_subs.py``) so it can't be ``import``-ed by
# name — leading-digit identifiers are not valid Python module names. Standalone
# prod-data scripts in ``scripts/`` are run as ``python scripts/<file>.py``, not
# imported, so this is the right way to reach the pure function for a unit test.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "204_cancel_legacy_voice_subs.py"
)
_MOD_NAME = "script_204_cancel_legacy_voice_subs"
# Audit 2026-07-18: скрипт больше не хранит прод-tenant-id'ы в коде — читает
# их из env. Тест подставляет те же синтетические-для-теста id, что сидит
# ниже (TENANT_A/TENANT_B), ДО exec_module (константа считается при импорте).
os.environ.setdefault(
    "SREDA_204_EXPECTED_TENANTS",
    "tenant_tg_1089832184,tenant_tg_755682022",
)
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass(field(default_factory=...)) can resolve
# the module's own namespace (dataclasses looks it up via sys.modules).
sys.modules[_MOD_NAME] = mod
_spec.loader.exec_module(mod)

# Canonical production tenant ids. The plan/checklist write the set as
# shorthand ``{tg_1089832184, tg_755682022}``, but the real ``Tenant.id``
# is ``tenant_tg_<digits>`` (see onboarding.py:292
# ``tenant_id = f"tenant_tg_{telegram_id}"``). The script defines the
# expected set the same way — assert the two agree so a future edit can't
# silently drift the canonical id format.
TENANT_A = "tenant_tg_1089832184"
TENANT_B = "tenant_tg_755682022"

VOICE_FEATURE = "voice_transcription"
HOUSEWIFE_FEATURE = "housewife_assistant"
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / builders
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


@pytest.fixture(autouse=True)
def _voice_registry(monkeypatch):
    """Patch the feature registry so ``resolve_voice_access`` resolves voice
    through the ``housewife_assistant`` carrier (manifest
    ``includes_voice=True``) and NOT through the legacy ``voice_transcription``
    plan (``includes_voice=False``) — exactly the prod semantics the script
    relies on. resolve_voice_access calls ``get_feature_registry()`` at module
    scope in ``agent_capabilities``, so we patch it there."""

    def _manifest(includes_voice: bool):
        m = MagicMock()
        m.includes_voice = includes_voice
        return m

    manifests = {
        HOUSEWIFE_FEATURE: _manifest(True),
        VOICE_FEATURE: _manifest(False),  # legacy plan never grants voice
    }

    registry = MagicMock()
    registry.get_manifest.side_effect = lambda key: manifests.get(key)
    monkeypatch.setattr(
        agent_capabilities, "get_feature_registry", lambda: registry
    )
    return registry


def _plan(
    *,
    id: str,
    plan_key: str,
    feature_key: str,
    title: str,
    is_active: bool = True,
) -> SubscriptionPlan:
    return SubscriptionPlan(
        id=id,
        plan_key=plan_key,
        feature_key=feature_key,
        title=title,
        description=f"{title} plan",
        price_rub=0,
        billing_period_days=30,
        is_public=True,
        is_active=is_active,
        sort_order=10,
    )


def _seed_plans(session) -> None:
    """Voice tombstone plan + the active housewife (sreda_free) plan."""
    session.add(
        _plan(
            id="plan_voice",
            plan_key="voice_transcription_base",
            feature_key=VOICE_FEATURE,
            title="Распознавание голоса",
            is_active=False,  # prod tombstone (migration 0018)
        )
    )
    session.add(
        _plan(
            id="plan_free",
            plan_key="sreda_free",
            feature_key=HOUSEWIFE_FEATURE,
            title="Среда Free",
            is_active=True,
        )
    )


def _add_tenant(session, tenant_id: str) -> None:
    session.add(Tenant(id=tenant_id, name=f"T-{tenant_id}", approved_at=NOW - timedelta(days=30)))


def _add_voice_sub(
    session, tenant_id: str, *, status: str = "active", sub_id: str | None = None
) -> TenantSubscription:
    sub = TenantSubscription(
        id=sub_id or f"sub_voice_{tenant_id}",
        tenant_id=tenant_id,
        plan_id="plan_voice",
        feature_key=VOICE_FEATURE,
        status=status,
        starts_at=NOW - timedelta(days=40),
        active_until=NOW + timedelta(days=36500),
        cancel_at_period_end=False,
        quantity=1,
        next_cycle_quantity=1,
    )
    session.add(sub)
    return sub


def _add_housewife_sub(
    session, tenant_id: str, *, status: str = "active"
) -> TenantSubscription:
    sub = TenantSubscription(
        id=f"sub_hw_{tenant_id}",
        tenant_id=tenant_id,
        plan_id="plan_free",
        feature_key=HOUSEWIFE_FEATURE,
        status=status,
        starts_at=NOW - timedelta(days=40),
        active_until=NOW + timedelta(days=36500),
        cancel_at_period_end=False,
        quantity=1,
        next_cycle_quantity=1,
    )
    session.add(sub)
    return sub


def _active_voice_count(session) -> int:
    return (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.feature_key == VOICE_FEATURE,
            TenantSubscription.status == "active",
        )
        .count()
    )


def _active_housewife_count(session, tenant_id: str) -> int:
    return (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == tenant_id,
            TenantSubscription.feature_key == HOUSEWIFE_FEATURE,
            TenantSubscription.status == "active",
        )
        .count()
    )


def _seed_happy(session) -> None:
    """Exactly the prod precondition: 2 active voice subs on {A,B}, each with
    an active housewife sub."""
    _seed_plans(session)
    for t in (TENANT_A, TENANT_B):
        _add_tenant(session, t)
        _add_voice_sub(session, t)
        _add_housewife_sub(session, t)
    session.commit()


# ---------------------------------------------------------------------------
# Sanity: the script's expected tenant set matches the canonical ids
# ---------------------------------------------------------------------------


def test_expected_tenant_set_is_canonical() -> None:
    """The script's EXPECTED_TENANT_IDS must be the canonical ``tenant_tg_*``
    ids (not the ``tg_*`` shorthand from the plan)."""
    assert mod.EXPECTED_TENANT_IDS == frozenset({TENANT_A, TENANT_B})


# ---------------------------------------------------------------------------
# Happy path — both voice subs cancelled with full fields
# ---------------------------------------------------------------------------


def test_happy_cancels_both_voice_subs_full_fields(session) -> None:
    """Given 2 active voice + active housewife on {A,B}, when the script runs,
    then both voice subs are cancelled with the FULL mirror of
    cancel_voice_subscription (status/quantity/next_cycle_quantity/
    cancel_at_period_end + feature-flag disabled), housewife untouched."""
    _seed_happy(session)

    result = mod.cancel_legacy_voice_subs(session, now=NOW)

    assert result.applied is True
    assert result.cancelled_count == 2
    assert set(result.cancelled_tenant_ids) == {TENANT_A, TENANT_B}

    session.expire_all()
    # Postcondition: active voice == 0 (no rows surface in /admin/budget).
    assert _active_voice_count(session) == 0
    for t in (TENANT_A, TENANT_B):
        sub = session.query(TenantSubscription).filter_by(
            tenant_id=t, feature_key=VOICE_FEATURE
        ).one()
        assert sub.status == "cancelled"
        assert sub.quantity == 0
        assert sub.next_cycle_quantity == 0
        assert sub.cancel_at_period_end is True
        assert sub.updated_at is not None
        # Voice feature-flag flipped off (mirror _ensure_feature_enabled(...False)).
        feat = session.query(TenantFeature).filter_by(
            tenant_id=t, feature_key=VOICE_FEATURE
        ).one()
        assert feat.enabled is False
        # Postcondition: housewife still active for both.
        assert _active_housewife_count(session, t) == 1


def test_happy_does_not_touch_housewife_or_other_subs(session) -> None:
    """🔴 anti-over-reach (c-010): the script cancels ONLY the 2 voice subs —
    housewife subs and an unrelated tenant's subs are byte-for-byte untouched."""
    _seed_happy(session)
    # An unrelated tenant with its own active voice-feature sub on a DIFFERENT
    # (live) plan — must NOT be touched (proves the script scopes to the exact
    # tombstone-plan voice subs of {A,B}, not "any voice sub anywhere").
    _add_tenant(session, "tenant_tg_999")
    session.add(
        _plan(
            id="plan_voice_live",
            plan_key="voice_live",
            feature_key="voice_live",
            title="Голос (живой)",
            is_active=True,
        )
    )
    session.add(
        TenantSubscription(
            id="sub_other_voice",
            tenant_id="tenant_tg_999",
            plan_id="plan_voice_live",
            feature_key="voice_live",
            status="active",
            starts_at=NOW - timedelta(days=5),
            active_until=NOW + timedelta(days=30),
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    session.commit()

    # Snapshot housewife state before.
    hw_before = {
        t: session.query(TenantSubscription).filter_by(
            tenant_id=t, feature_key=HOUSEWIFE_FEATURE
        ).one().status
        for t in (TENANT_A, TENANT_B)
    }

    mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    for t in (TENANT_A, TENANT_B):
        hw = session.query(TenantSubscription).filter_by(
            tenant_id=t, feature_key=HOUSEWIFE_FEATURE
        ).one()
        assert hw.status == hw_before[t] == "active"
        assert hw.quantity == 1
        assert hw.cancel_at_period_end is False

    other = session.get(TenantSubscription, "sub_other_voice")
    assert other.status == "active"
    assert other.quantity == 1
    assert other.cancel_at_period_end is False


# ---------------------------------------------------------------------------
# Drift — precondition fails, NOTHING mutated
# ---------------------------------------------------------------------------


def test_drift_three_active_voice_stops_no_mutation(session) -> None:
    """Given 3 active voice subs (drift), when the script runs, then it STOPS —
    NOTHING is cancelled (active voice count unchanged, no feature flipped)."""
    _seed_happy(session)
    # A 3rd unexpected active voice sub on the SAME tombstone plan.
    _add_tenant(session, "tenant_tg_333")
    _add_voice_sub(session, "tenant_tg_333", sub_id="sub_voice_extra")
    session.commit()

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    assert result.applied is False
    assert result.cancelled_count == 0
    assert _active_voice_count(session) == 3
    # No voice feature flipped off anywhere.
    assert (
        session.query(TenantFeature)
        .filter_by(feature_key=VOICE_FEATURE, enabled=False)
        .count()
        == 0
    )


def test_drift_wrong_tenant_set_stops_no_mutation(session) -> None:
    """Given exactly 2 active voice subs but on the WRONG tenant-set, when the
    script runs, then it STOPS (tenant-set guard) — nothing cancelled."""
    _seed_plans(session)
    for t in (TENANT_A, "tenant_tg_777"):  # B replaced by an impostor
        _add_tenant(session, t)
        _add_voice_sub(session, t)
        _add_housewife_sub(session, t)
    session.commit()

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    assert result.applied is False
    assert _active_voice_count(session) == 2


def test_drift_missing_housewife_stops_no_mutation(session) -> None:
    """Given 2 active voice subs on {A,B} but one tenant LACKS an active
    housewife sub (taking voice away would strand them), when the script runs,
    then it STOPS — nothing cancelled."""
    _seed_plans(session)
    for t in (TENANT_A, TENANT_B):
        _add_tenant(session, t)
        _add_voice_sub(session, t)
    _add_housewife_sub(session, TENANT_A)  # B has NO housewife
    session.commit()

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    assert result.applied is False
    assert _active_voice_count(session) == 2


# ---------------------------------------------------------------------------
# Idempotency — second run on already-cancelled state is a clean no-op
# ---------------------------------------------------------------------------


def test_idempotent_second_run_is_noop(session) -> None:
    """Given the script already ran (0 active voice), when it runs AGAIN, then
    the precondition «exactly 2 active» fails → clean no-op (no exception,
    nothing mutated, housewife intact)."""
    _seed_happy(session)
    first = mod.cancel_legacy_voice_subs(session, now=NOW)
    assert first.applied is True
    session.expire_all()
    assert _active_voice_count(session) == 0

    # Second run — must not raise, must be a no-op.
    second = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    assert second.applied is False
    assert second.cancelled_count == 0
    assert _active_voice_count(session) == 0
    # Housewife still active for both.
    for t in (TENANT_A, TENANT_B):
        assert _active_housewife_count(session, t) == 1


# ---------------------------------------------------------------------------
# Dry-run — computes the plan but commits NOTHING
# ---------------------------------------------------------------------------


def test_dry_run_does_not_commit(session) -> None:
    """Given the happy precondition, when run with dry_run=True, then it reports
    what it WOULD cancel (2) but commits nothing AND is self-contained: WITHOUT
    any external rollback, a fresh read still sees 2 active voice subs (the
    function rolls back its own in-flight changes — FIX 3)."""
    _seed_happy(session)

    result = mod.cancel_legacy_voice_subs(session, now=NOW, dry_run=True)
    assert result.applied is False
    assert result.would_cancel_count == 2
    assert set(result.cancelled_tenant_ids) == {TENANT_A, TENANT_B}

    # NO external rollback here — the dry-run must be durable-mutation-free on
    # its own. Re-read: still 2 active, nothing cancelled, no feature flipped.
    session.expire_all()
    assert _active_voice_count(session) == 2
    for t in (TENANT_A, TENANT_B):
        sub = session.query(TenantSubscription).filter_by(
            tenant_id=t, feature_key=VOICE_FEATURE
        ).one()
        assert sub.status == "active"
        assert sub.quantity == 1
    assert (
        session.query(TenantFeature)
        .filter_by(feature_key=VOICE_FEATURE, enabled=False)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# FIX 1 — selection is by plan_key, NOT feature_key
# ---------------------------------------------------------------------------


def test_different_plan_key_same_feature_key_not_matched(session) -> None:
    """🔴 FIX 1: a subscription on a DIFFERENT plan_key but the SAME
    feature_key=='voice_transcription', held by the expected tenants, must NOT
    be selected — the script mirrors cancel_voice_subscription, which targets
    the ``voice_transcription_base`` PLAN_KEY (billing.py:513), not the
    feature_key. So with only such off-plan subs present, the precondition
    «exactly 2 active voice subs on voice_transcription_base» sees 0 → STOP, no
    mutation, the off-plan sub left untouched (a feature_key filter would have
    wrongly cancelled it)."""
    _seed_plans(session)
    # A SECOND voice-feature plan with a DIFFERENT plan_key (e.g. a future
    # voice add-on) — same feature_key, must be out of scope.
    session.add(
        _plan(
            id="plan_voice_other",
            plan_key="voice_transcription_pro",  # different plan_key
            feature_key=VOICE_FEATURE,  # SAME feature_key
            title="Голос Pro",
            is_active=True,
        )
    )
    for t in (TENANT_A, TENANT_B):
        _add_tenant(session, t)
        _add_housewife_sub(session, t)
        # Active sub on the OTHER plan (NOT voice_transcription_base).
        session.add(
            TenantSubscription(
                id=f"sub_voice_other_{t}",
                tenant_id=t,
                plan_id="plan_voice_other",
                feature_key=VOICE_FEATURE,
                status="active",
                starts_at=NOW - timedelta(days=5),
                active_until=NOW + timedelta(days=30),
                cancel_at_period_end=False,
                quantity=1,
                next_cycle_quantity=1,
            )
        )
    session.commit()

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    # Precondition (1) counts subs on voice_transcription_base only → 0 → STOP.
    assert result.applied is False
    assert result.cancelled_count == 0
    assert "expected exactly 2 active voice subs" in result.reason
    # The off-plan voice subs are byte-for-byte untouched (not over-reached).
    for t in (TENANT_A, TENANT_B):
        other = session.get(TenantSubscription, f"sub_voice_other_{t}")
        assert other.status == "active"
        assert other.quantity == 1
        assert other.cancel_at_period_end is False
    # No voice feature-flag flipped off anywhere.
    assert (
        session.query(TenantFeature)
        .filter_by(feature_key=VOICE_FEATURE, enabled=False)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# FIX 2 — gate on resolve_voice_access().allowed, not status-only housewife
# ---------------------------------------------------------------------------


def test_degraded_housewife_zero_quantity_stops_no_mutation(session) -> None:
    """🔴 FIX 2: housewife is status='active' but DEGRADED (quantity=0) →
    resolve_voice_access().allowed is False (voice does NOT actually work
    through the carrier) → STOP, the legacy voice sub is NOT cancelled (so the
    tenant is not stranded WITHOUT voice). A status-only check would have
    wrongly proceeded."""
    _seed_plans(session)
    for t in (TENANT_A, TENANT_B):
        _add_tenant(session, t)
        _add_voice_sub(session, t)
    # Both housewife subs are 'active' but quantity=0 → not a working carrier.
    for t in (TENANT_A, TENANT_B):
        hw = _add_housewife_sub(session, t)
        hw.quantity = 0
    session.commit()

    # Sanity: voice does NOT resolve (degraded carrier) for either tenant.
    for t in (TENANT_A, TENANT_B):
        assert agent_capabilities.resolve_voice_access(session, t).allowed is False

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    assert result.applied is False
    assert result.cancelled_count == 0
    assert "resolve_voice_access" in result.reason
    # Legacy voice subs untouched — tenant keeps the only voice they had.
    assert _active_voice_count(session) == 2
    for t in (TENANT_A, TENANT_B):
        sub = session.query(TenantSubscription).filter_by(
            tenant_id=t, feature_key=VOICE_FEATURE
        ).one()
        assert sub.status == "active"
    assert (
        session.query(TenantFeature)
        .filter_by(feature_key=VOICE_FEATURE, enabled=False)
        .count()
        == 0
    )


def test_degraded_housewife_expired_stops_no_mutation(session) -> None:
    """🔴 FIX 2 (variant): housewife is status='active' but its
    ``active_until`` is in the PAST → resolve_voice_access().allowed is False
    → STOP, legacy voice sub NOT cancelled."""
    _seed_plans(session)
    for t in (TENANT_A, TENANT_B):
        _add_tenant(session, t)
        _add_voice_sub(session, t)
    for t in (TENANT_A, TENANT_B):
        hw = _add_housewife_sub(session, t)
        hw.active_until = NOW - timedelta(days=1)  # expired
    session.commit()

    for t in (TENANT_A, TENANT_B):
        assert agent_capabilities.resolve_voice_access(session, t).allowed is False

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    session.expire_all()

    assert result.applied is False
    assert result.cancelled_count == 0
    assert _active_voice_count(session) == 2


def test_happy_postcondition_voice_still_resolves(session) -> None:
    """🔴 FIX 2 (postcondition): on the happy path, cancelling the legacy voice
    sub must NOT change resolve_voice_access().allowed (resolve ignores the
    legacy plan) — it stays True via the housewife carrier. The function
    commits (applied=True) precisely because the postcondition holds."""
    _seed_happy(session)
    for t in (TENANT_A, TENANT_B):
        assert agent_capabilities.resolve_voice_access(session, t).allowed is True

    result = mod.cancel_legacy_voice_subs(session, now=NOW)
    assert result.applied is True

    session.expire_all()
    assert _active_voice_count(session) == 0
    # Voice STILL resolves through housewife after the legacy sub is gone.
    for t in (TENANT_A, TENANT_B):
        assert agent_capabilities.resolve_voice_access(session, t).allowed is True
