"""Unit tests for housewife LLM chat tools."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyReminder
from sreda.services.housewife_chat_tools import build_housewife_tools


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    session.commit()
    return session


def _tools_by_name(tools):
    return {t.name: t for t in tools}


def test_schedule_reminder_creates_row() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )

    result = tools["schedule_reminder"].invoke(
        {
            "title": "Купить молоко",
            "trigger_iso": "2026-05-01T12:00:00+00:00",
        }
    )

    assert result.startswith("ok:scheduled:rem_")
    assert session.query(FamilyReminder).count() == 1


def test_schedule_reminder_rejects_bad_datetime() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )

    result = tools["schedule_reminder"].invoke(
        {"title": "Bad", "trigger_iso": "not-a-date"}
    )

    assert result.startswith("error:")
    assert session.query(FamilyReminder).count() == 0


def test_schedule_reminder_rejects_bad_rrule() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )

    result = tools["schedule_reminder"].invoke(
        {
            "title": "X",
            "trigger_iso": "2026-05-01T12:00:00+00:00",
            "recurrence_rule": "TOTALLY_INVALID",
        }
    )

    assert result.startswith("error:")


def test_list_reminders_empty() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )

    result = tools["list_reminders"].invoke({})

    assert "no active reminders" in result


def test_list_reminders_returns_formatted_lines() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )
    tools["schedule_reminder"].invoke(
        {"title": "First", "trigger_iso": "2026-05-01T12:00:00+00:00"}
    )
    tools["schedule_reminder"].invoke(
        {"title": "Second", "trigger_iso": "2026-05-02T12:00:00+00:00"}
    )

    result = tools["list_reminders"].invoke({})

    assert "active reminders:" in result
    assert "First" in result
    assert "Second" in result


def test_cancel_reminder_works_then_denies_second_time() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )
    ok_result = tools["schedule_reminder"].invoke(
        {"title": "X", "trigger_iso": "2026-05-01T12:00:00+00:00"}
    )
    # Extract id from "ok:scheduled:rem_XXXXX:TIMESTAMP"
    rid = ok_result.split(":")[2]

    r1 = tools["cancel_reminder"].invoke({"reminder_id": rid})
    r2 = tools["cancel_reminder"].invoke({"reminder_id": rid})

    assert r1 == "ok:cancelled"
    assert r2.startswith("error:")


def test_cancel_reminder_unknown_id() -> None:
    session = _fresh_session()
    tools = _tools_by_name(
        build_housewife_tools(session=session, tenant_id="tenant_1", user_id="user_1")
    )

    result = tools["cancel_reminder"].invoke({"reminder_id": "rem_does_not_exist"})

    assert result.startswith("error:")
