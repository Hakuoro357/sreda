"""R-34 (2026-05-16): schedule_reminder WARN log on non-ok paths.

Prod incident — bot reply «12:00 прошло» but без логов tool args
невозможно подтвердить root cause (LLM передала вчерашнюю дату).
R-34 adds WARN log per non-ok return branch с forensic fields:
tenant_id, user_id, trigger_iso_raw, parsed_trigger_utc, now_utc,
tz_label, branch-specific (late_by_min | exc), title_len.

Tests (4 cases per Codex R1 R2 MAJOR 2):
1. parse error (invalid trigger_iso format) → WARN
2. skipped:past (one-shot late) → WARN с late_by_min
3. service ValueError → WARN с exc
4. service generic Exception → WARN (logger.exception path)

Privacy: title content НЕ в log, только title_len.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.housewife_chat_tools import build_housewife_tools


@pytest.fixture
def _session(monkeypatch, tmp_path):
    """Test-isolated session with seeded tenant/user."""
    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "a" * 64)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_SALT", "c" * 32)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(Tenant(id="t1", name="Test"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    s.commit()
    yield s
    s.close()
    get_settings.cache_clear()


def _get_schedule_tool(session):
    tools = build_housewife_tools(session=session, tenant_id="t1", user_id="u1")
    return {t.name: t for t in tools}["schedule_reminder"]


# ── 1. Parse error ─────────────────────────────────────────────────


def test_parse_error_emits_warn(_session, caplog) -> None:
    """Invalid trigger_iso → returns error + WARN log с exc field."""
    schedule = _get_schedule_tool(_session)
    with caplog.at_level(logging.WARNING, logger="sreda.services.housewife_chat_tools"):
        result = schedule.invoke({
            "title": "test reminder",
            "trigger_iso": "not-a-valid-iso-string",
        })
    assert result.startswith("error: cannot parse trigger_iso=")
    parse_warns = [
        r for r in caplog.records
        if "parse trigger_iso" in r.message and "trigger_iso_raw" in r.message
    ]
    assert len(parse_warns) >= 1, (
        "Expected parse-error WARN log с trigger_iso_raw — for forensic"
    )
    msg = parse_warns[0].getMessage()
    # No raw title в log (only title_len)
    assert "test reminder" not in msg, "Raw title leaked — privacy fail"
    assert "title_len=" in msg
    assert "server_now_utc=" in msg


# ── 2. skipped:past ─────────────────────────────────────────────────


def test_skipped_past_emits_warn(_session, caplog) -> None:
    """Past trigger_iso → returns skipped:past + WARN log с late_by_min."""
    schedule = _get_schedule_tool(_session)
    # Way past — 2024 — guaranteed skip независимо от текущего clock
    with caplog.at_level(logging.WARNING, logger="sreda.services.housewife_chat_tools"):
        result = schedule.invoke({
            "title": "secret content",
            "trigger_iso": "2024-01-01T12:00:00",
        })
    assert result.startswith("skipped:past:2024-01-01T12:00:00"), (
        f"Expected skipped:past prefix, got: {result}"
    )
    skip_warns = [
        r for r in caplog.records
        if "schedule_reminder skipped" in r.message and "past trigger" in r.message
    ]
    assert len(skip_warns) >= 1, (
        "Expected skipped:past WARN log с late_by_min — for forensic"
    )
    msg = skip_warns[0].getMessage()
    assert "secret content" not in msg, "Raw title leaked — privacy fail"
    assert "late_by_min=" in msg
    assert "parsed_trigger_utc=" in msg
    assert "trigger_iso_raw=" in msg
    assert "server_now_utc=" in msg


# ── 3. service ValueError ──────────────────────────────────────────


def test_service_value_error_emits_warn(_session, caplog) -> None:
    """service.schedule raises ValueError → WARN с exc field."""
    schedule = _get_schedule_tool(_session)
    future = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    # Replace по offset = +1 hour — future trigger
    from datetime import timedelta
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        microsecond=0
    ).isoformat()

    with patch(
        "sreda.services.housewife_reminders.HousewifeReminderService.schedule",
        side_effect=ValueError("test-induced value error"),
    ):
        with caplog.at_level(logging.WARNING, logger="sreda.services.housewife_chat_tools"):
            result = schedule.invoke({
                "title": "test title private",
                "trigger_iso": future,
            })
    assert result.startswith("error: test-induced value error"), (
        f"Expected error: prefix with exc, got: {result}"
    )
    err_warns = [
        r for r in caplog.records
        if "service ValueError" in r.message and "exc_type=" in r.message
    ]
    assert len(err_warns) >= 1, (
        "Expected service ValueError WARN log с exc_type field"
    )
    msg = err_warns[0].getMessage()
    assert "test title private" not in msg, "Raw title leaked"
    assert "title_len=" in msg


# ── 4. service generic Exception ───────────────────────────────────


def test_service_generic_exception_emits_warn(_session, caplog) -> None:
    """service.schedule raises non-ValueError → WARN с exc_type+exc_str.
    R-34 R2: logger.warning (NOT logger.exception) — privacy fix drops
    exc_info=True чтобы избежать leak SQLAlchemy params в traceback."""
    schedule = _get_schedule_tool(_session)
    from datetime import timedelta
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        microsecond=0
    ).isoformat()

    with patch(
        "sreda.services.housewife_reminders.HousewifeReminderService.schedule",
        side_effect=RuntimeError("test-induced runtime error"),
    ):
        with caplog.at_level(logging.WARNING, logger="sreda.services.housewife_chat_tools"):
            result = schedule.invoke({
                "title": "private content",
                "trigger_iso": future,
            })
    assert result == "error: internal", f"Expected error: internal, got: {result}"
    # R-34 R2: WARN level (privacy: dropped exc_info=True).
    generic_warns = [
        r for r in caplog.records
        if "generic exception" in r.message
    ]
    assert len(generic_warns) >= 1, (
        "Expected generic-exception WARN/ERROR log"
    )
    msg = generic_warns[0].getMessage()
    assert "private content" not in msg, "Raw title leaked"


# ── 5. Happy path: no WARN (Qwen R2 nit) ───────────────────────────


def test_ok_path_emits_no_warn(_session, caplog) -> None:
    """Successful schedule → no WARN log fires (Qwen R2 MINOR: confirm
    happy path silent)."""
    schedule = _get_schedule_tool(_session)
    from datetime import timedelta
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        microsecond=0
    ).isoformat()
    with caplog.at_level(logging.WARNING, logger="sreda.services.housewife_chat_tools"):
        result = schedule.invoke({
            "title": "happy path",
            "trigger_iso": future,
        })
    assert result.startswith("ok:scheduled:"), (
        f"Expected ok:scheduled prefix, got: {result}"
    )
    schedule_warns = [
        r for r in caplog.records
        if r.name == "sreda.services.housewife_chat_tools"
        and ("skipped" in r.message or "error" in r.message)
    ]
    assert len(schedule_warns) == 0, (
        f"Happy path должна быть silent, но получили WARN: "
        f"{[r.message for r in schedule_warns]}"
    )
