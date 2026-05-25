"""Tests for the MSK day-window helper used by /admin/budget per-day view.

The window must:
- start at 00:00 Europe/Moscow on the given calendar date
- end at 00:00 Europe/Moscow on the next calendar date
- be expressed in UTC (so SQLAlchemy comparisons with tz-aware
  ``created_at`` work without further conversion)

Russia is UTC+3 year-round (no DST since 2011), so a single offset is
expected — but the test still uses ``ZoneInfo`` so any future TZ rule
changes flow through transparently rather than being baked into magic
offsets.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sreda.admin.queries import _msk_day_window_utc


def test_msk_day_window_starts_at_msk_midnight() -> None:
    start_utc, _ = _msk_day_window_utc(date(2026, 5, 25))
    # 00:00 MSK == 21:00 UTC previous day (UTC+3)
    assert start_utc == datetime(2026, 5, 24, 21, 0, 0, tzinfo=timezone.utc)


def test_msk_day_window_ends_at_next_msk_midnight() -> None:
    _, end_utc = _msk_day_window_utc(date(2026, 5, 25))
    # 00:00 MSK on 2026-05-26 == 21:00 UTC on 2026-05-25
    assert end_utc == datetime(2026, 5, 25, 21, 0, 0, tzinfo=timezone.utc)


def test_msk_day_window_is_exactly_24_hours() -> None:
    start_utc, end_utc = _msk_day_window_utc(date(2026, 5, 25))
    assert (end_utc - start_utc).total_seconds() == 24 * 3600


def test_msk_day_window_returns_utc_aware() -> None:
    start_utc, end_utc = _msk_day_window_utc(date(2026, 1, 1))
    assert start_utc.tzinfo is timezone.utc
    assert end_utc.tzinfo is timezone.utc


def test_msk_day_window_year_boundary() -> None:
    # 2025-12-31 in MSK → 2025-12-30 21:00 UTC … 2025-12-31 21:00 UTC
    start_utc, end_utc = _msk_day_window_utc(date(2025, 12, 31))
    assert start_utc == datetime(2025, 12, 30, 21, 0, 0, tzinfo=timezone.utc)
    assert end_utc == datetime(2025, 12, 31, 21, 0, 0, tzinfo=timezone.utc)
