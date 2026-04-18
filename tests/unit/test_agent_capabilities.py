"""Unit tests for agent_capabilities.has_voice_access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sreda.services.agent_capabilities import has_voice_access


def _mk_sub(
    *,
    feature_key: str,
    status: str = "active",
    active_until_offset_days: int = 30,
    quantity: int = 1,
):
    """Return a (TenantSubscription, SubscriptionPlan) pair as the join
    result would produce. We bypass ORM completely — the helper only
    touches attributes, not SQL behaviour."""
    sub = MagicMock()
    sub.status = status
    sub.quantity = quantity
    sub.active_until = datetime.now(UTC) + timedelta(days=active_until_offset_days)

    plan = MagicMock()
    plan.feature_key = feature_key
    return (sub, plan)


def _make_session(subs):
    """Session mock whose chained .query/.join/.filter/.all returns ``subs``."""
    session = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = subs
    session.query.return_value = q
    return session


def _make_registry(manifests_by_key):
    registry = MagicMock()
    def _get(key):
        return manifests_by_key.get(key)
    registry.get_manifest.side_effect = _get
    return registry


def _mk_manifest(includes_voice: bool):
    m = MagicMock()
    m.includes_voice = includes_voice
    return m


def test_no_tenant_returns_false():
    assert has_voice_access(MagicMock(), "") is False


def test_no_subscriptions_returns_false():
    session = _make_session([])
    with patch(
        "sreda.services.agent_capabilities.get_feature_registry",
        return_value=_make_registry({}),
    ):
        assert has_voice_access(session, "t1") is False


def test_active_agent_with_voice_returns_true():
    session = _make_session([_mk_sub(feature_key="housewife_assistant")])
    registry = _make_registry({"housewife_assistant": _mk_manifest(True)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is True


def test_active_agent_without_voice_returns_false():
    """EDS Monitor is active but manifest has includes_voice=False."""
    session = _make_session([_mk_sub(feature_key="eds_monitor")])
    registry = _make_registry({"eds_monitor": _mk_manifest(False)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is False


def test_legacy_voice_transcription_subscription_does_not_grant():
    """Old standalone voice_transcription subscriptions are dead weight —
    they must NOT grant voice access. Voice is only granted by agents
    whose manifest sets includes_voice=True."""
    session = _make_session([_mk_sub(feature_key="voice_transcription")])
    # voice_transcription manifest now declares includes_voice=False
    # (it's not an agent, it's the runtime package).
    registry = _make_registry({"voice_transcription": _mk_manifest(False)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is False


def test_expired_subscription_ignored():
    session = _make_session([
        _mk_sub(feature_key="housewife_assistant", active_until_offset_days=-1),
    ])
    registry = _make_registry({"housewife_assistant": _mk_manifest(True)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is False


def test_cancelled_subscription_ignored():
    session = _make_session([
        _mk_sub(feature_key="housewife_assistant", status="cancelled"),
    ])
    registry = _make_registry({"housewife_assistant": _mk_manifest(True)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is False


def test_scheduled_for_cancel_still_grants():
    """User cancelled mid-cycle — service still active until period end."""
    session = _make_session([
        _mk_sub(feature_key="housewife_assistant", status="scheduled_for_cancel"),
    ])
    registry = _make_registry({"housewife_assistant": _mk_manifest(True)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is True


def test_mixed_active_agents_one_has_voice():
    session = _make_session([
        _mk_sub(feature_key="eds_monitor"),
        _mk_sub(feature_key="housewife_assistant"),
    ])
    registry = _make_registry({
        "eds_monitor": _mk_manifest(False),
        "housewife_assistant": _mk_manifest(True),
    })
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is True


def test_zero_quantity_ignored():
    session = _make_session([
        _mk_sub(feature_key="housewife_assistant", quantity=0),
    ])
    registry = _make_registry({"housewife_assistant": _mk_manifest(True)})
    with patch("sreda.services.agent_capabilities.get_feature_registry", return_value=registry):
        assert has_voice_access(session, "t1") is False
