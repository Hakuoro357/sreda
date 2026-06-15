"""#149: humanized reminder display.

Regression for the 2026-06-15 prod bug (trace, internal tenants): a
6-item reminder list rendered with raw ISO timestamps + RRULE strings
dropped the cyrillic ratio below the substitution-guard thresholds, so
``finalize_chat_reply`` replaced the whole list with a generic stub.

The fix humanizes ``_format_reminder_for_llm`` (MSK time + Russian
recurrence), keeping the ``[rem_...]`` id for the planner. The rot strips
the id per prompt; these tests assert the user-facing text is Russian and
passes both cyrillic-ratio guards.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sreda.runtime.handlers import (
    _is_predominantly_non_russian,
    _is_reasoning_leak_after_tool,
)
from sreda.services.housewife_chat_tools import _format_reminder_for_llm

MSK = ZoneInfo("Europe/Moscow")


def _rem(rid: str, title: str, trigger: datetime, rrule: str | None = None):
    return SimpleNamespace(
        id=rid, title=title, next_trigger_at=trigger, recurrence_rule=rrule
    )


def test_oneshot_humanized_to_msk_no_iso() -> None:
    rem = _rem("rem_" + "a" * 24, "разминка", datetime(2026, 6, 17, 6, 0, tzinfo=UTC))
    out = _format_reminder_for_llm(rem, MSK)
    assert "09:00" in out  # 06:00 UTC -> 09:00 MSK
    assert "17 июня" in out
    assert "2026-06-17" not in out and "T06:00" not in out


def test_recurring_weekly_byday_with_until() -> None:
    rem = _rem(
        "rem_" + "b" * 24,
        "разминка",
        datetime(2026, 6, 18, 6, 0, tzinfo=UTC),
        "FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261231T060000Z",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "по пн, ср и пт" in out
    assert "до 31 декабря" in out
    assert "09:00" in out
    assert "FREQ=" not in out and "BYDAY=" not in out and "UNTIL=" not in out


def test_daily_recurrence_humanized() -> None:
    rem = _rem(
        "rem_" + "c" * 24,
        "выпить воды",
        datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
        "FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "ежедневно" in out
    assert "FREQ=" not in out and "BYHOUR=" not in out


def test_id_retained_for_planner() -> None:
    # The planner reads reminder_id from list_reminders output to call
    # update_reminder; the id must stay in the LLM-facing line.
    rem = _rem("rem_" + "d" * 24, "разминка", datetime(2026, 6, 17, 6, 0, tzinfo=UTC))
    out = _format_reminder_for_llm(rem, MSK)
    assert "rem_" + "d" * 24 in out


def test_full_list_passes_substitution_guards() -> None:
    # The exact list shape that broke prod 2026-06-15. After humanizing,
    # the rot echoes Russian (ids stripped per prompt) -> both
    # cyrillic-ratio guards must pass.
    rems = [
        _rem("rem_" + "a" * 24, "разминка", datetime(2026, 6, 17, 6, 0, tzinfo=UTC)),
        _rem(
            "rem_" + "b" * 24,
            "заняться разминкой",
            datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
        ),
        _rem(
            "rem_" + "c" * 24,
            "разминка",
            datetime(2026, 6, 18, 6, 0, tzinfo=UTC),
            "FREQ=WEEKLY;BYDAY=MO,WE,FR",
        ),
        _rem("rem_" + "e" * 24, "разминка", datetime(2026, 6, 19, 6, 0, tzinfo=UTC)),
        _rem(
            "rem_" + "f" * 24,
            "заняться разминкой",
            datetime(2026, 6, 22, 6, 0, tzinfo=UTC),
            "FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261231T060000Z",
        ),
    ]
    lines = [_format_reminder_for_llm(r, MSK) for r in rems]
    # Simulate the rot: it omits the [rem_...] id per the brain prompt.
    user_text = "Твои напоминания:\n" + "\n".join(
        re.sub(r"\[?rem_[0-9a-f]+\]?\s*", "", ln) for ln in lines
    )
    assert not _is_predominantly_non_russian(user_text)
    assert not _is_reasoning_leak_after_tool(user_text, {"list_reminders"})
