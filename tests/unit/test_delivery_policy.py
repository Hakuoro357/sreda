"""Phase 2d: delivery policy decision tree + quiet-window math."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sreda.runtime.delivery_policy import (
    DeliveryKind,
    _in_window,
    _next_exit,
    decide_delivery,
)


MSK = ZoneInfo("Europe/Moscow")


def _utc(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Decision tree
# ---------------------------------------------------------------------------


def test_interactive_always_sends_even_when_muted():
    profile = {"timezone": "UTC", "quiet_hours": []}
    skill_config = {"notification_priority": "mute"}
    decision = decide_delivery(
        profile=profile,
        skill_config=skill_config,
        feature_key="eds_monitor",
        is_interactive=True,
        now_utc=_utc(2026, 4, 15, 23, 0),
    )
    assert decision.kind == DeliveryKind.send
    assert decision.reason == "interactive"


def test_mute_drops_proactive_reply():
    decision = decide_delivery(
        profile=None,
        skill_config={"notification_priority": "mute"},
        feature_key="eds_monitor",
        is_interactive=False,
        now_utc=_utc(2026, 4, 15, 12, 0),
    )
    assert decision.kind == DeliveryKind.drop
    assert decision.reason == "muted_by_user"


def test_urgent_bypasses_quiet_hours():
    profile = {
        "timezone": "Europe/Moscow",
        "quiet_hours": [{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    }
    decision = decide_delivery(
        profile=profile,
        skill_config={"notification_priority": "urgent"},
        feature_key="eds_monitor",
        is_interactive=False,
        now_utc=_utc(2026, 4, 15, 23, 0),  # 02:00 MSK — inside quiet
    )
    assert decision.kind == DeliveryKind.send
    assert decision.reason == "urgent"


def test_normal_priority_defers_inside_quiet_window():
    # 22:00..08:00 MSK, now is 2026-04-15 20:00 UTC == 23:00 MSK (inside window).
    profile = {
        "timezone": "Europe/Moscow",
        "quiet_hours": [{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    }
    now = _utc(2026, 4, 15, 20, 0)
    decision = decide_delivery(
        profile=profile,
        skill_config={"notification_priority": "normal"},
        feature_key="eds_monitor",
        is_interactive=False,
        now_utc=now,
    )
    assert decision.kind == DeliveryKind.defer
    # Exit = 08:00 MSK next day == 05:00 UTC 2026-04-16
    expected_exit_utc = datetime(2026, 4, 16, 5, 0, tzinfo=timezone.utc)
    assert decision.defer_until_utc == expected_exit_utc


def test_outside_quiet_window_sends_normal():
    profile = {
        "timezone": "Europe/Moscow",
        "quiet_hours": [{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    }
    # 12:00 UTC == 15:00 MSK — outside quiet
    decision = decide_delivery(
        profile=profile,
        skill_config={"notification_priority": "normal"},
        feature_key="eds_monitor",
        is_interactive=False,
        now_utc=_utc(2026, 4, 15, 12, 0),
    )
    assert decision.kind == DeliveryKind.send


def test_no_profile_falls_through_to_send():
    decision = decide_delivery(
        profile=None,
        skill_config=None,
        feature_key=None,
        is_interactive=False,
        now_utc=_utc(2026, 4, 15, 23, 0),
    )
    assert decision.kind == DeliveryKind.send


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------


def test_in_window_same_day_window():
    w = {"from_hour": 10, "to_hour": 14, "weekdays": list(range(7))}
    # Wed 2026-04-15
    assert _in_window(datetime(2026, 4, 15, 11, 0, tzinfo=MSK), w)
    assert not _in_window(datetime(2026, 4, 15, 14, 0, tzinfo=MSK), w)
    assert not _in_window(datetime(2026, 4, 15, 9, 59, tzinfo=MSK), w)


def test_in_window_midnight_crossing():
    w = {"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}
    assert _in_window(datetime(2026, 4, 15, 23, 0, tzinfo=MSK), w)
    assert _in_window(datetime(2026, 4, 16, 3, 0, tzinfo=MSK), w)
    assert not _in_window(datetime(2026, 4, 15, 9, 0, tzinfo=MSK), w)


def test_in_window_weekdays_filter():
    # Only weekdays 0..4 (Mon..Fri)
    w = {"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4]}
    # 2026-04-18 is a Saturday (weekday 5)
    assert not _in_window(datetime(2026, 4, 18, 23, 0, tzinfo=MSK), w)
    # 2026-04-15 is a Wednesday (weekday 2)
    assert _in_window(datetime(2026, 4, 15, 23, 0, tzinfo=MSK), w)


def test_in_window_zero_length_is_inactive():
    w = {"from_hour": 10, "to_hour": 10, "weekdays": list(range(7))}
    assert not _in_window(datetime(2026, 4, 15, 10, 0, tzinfo=MSK), w)


def test_next_exit_today_vs_tomorrow():
    w = {"from_hour": 22, "to_hour": 8}
    # 23:00 Wed → exit at 08:00 Thu
    now = datetime(2026, 4, 15, 23, 0, tzinfo=MSK)
    assert _next_exit(now, w) == datetime(2026, 4, 16, 8, 0, tzinfo=MSK)
    # 03:00 Thu → exit at 08:00 Thu (same day)
    now = datetime(2026, 4, 16, 3, 0, tzinfo=MSK)
    assert _next_exit(now, w) == datetime(2026, 4, 16, 8, 0, tzinfo=MSK)


def test_multiple_windows_take_latest_exit():
    # Two overlapping windows, exit of the "longer" one wins
    profile = {
        "timezone": "Europe/Moscow",
        "quiet_hours": [
            {"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))},
            {"from_hour": 2, "to_hour": 10, "weekdays": list(range(7))},
        ],
    }
    # 03:00 MSK == 00:00 UTC. In both windows; exits are 08:00 and 10:00 MSK.
    now = _utc(2026, 4, 16, 0, 0)
    decision = decide_delivery(
        profile=profile,
        skill_config={"notification_priority": "normal"},
        feature_key="eds_monitor",
        is_interactive=False,
        now_utc=now,
    )
    assert decision.kind == DeliveryKind.defer
    expected = datetime(2026, 4, 16, 10, 0, tzinfo=MSK).astimezone(timezone.utc)
    assert decision.defer_until_utc == expected


def test_invalid_timezone_falls_back_to_utc():
    profile = {
        "timezone": "Mars/Olympus_Mons",
        "quiet_hours": [{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    }
    # 23:00 UTC — inside the window interpreted as UTC hours
    decision = decide_delivery(
        profile=profile,
        skill_config={"notification_priority": "normal"},
        feature_key="eds_monitor",
        is_interactive=False,
        now_utc=_utc(2026, 4, 15, 23, 0),
    )
    assert decision.kind == DeliveryKind.defer
