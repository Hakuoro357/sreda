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


# --- M2: RRULE completeness (Codex R1) -------------------------------------


def test_minutely_interval_no_clock() -> None:
    rem = _rem(
        "rem_" + "1" * 24, "вода",
        datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
        "FREQ=MINUTELY;INTERVAL=30",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "каждые 30 минут" in out
    assert " в 09:00" not in out  # sub-hour → no meaningless clock
    assert "FREQ=" not in out


def test_interval_weekly_plural() -> None:
    rem = _rem(
        "rem_" + "2" * 24, "уборка",
        datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
        "FREQ=WEEKLY;INTERVAL=2",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "каждые 2 недели" in out


def test_unknown_rrule_never_oneshot() -> None:
    # A present-but-unusual rule must NOT render as a one-shot date.
    rem = _rem(
        "rem_" + "3" * 24, "что-то",
        datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
        "FREQ=SECONDLY;INTERVAL=5",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "по расписанию" in out
    assert "июня" not in out  # not rendered as a one-shot calendar date


def test_until_tz_aware_rolls_to_local_day() -> None:
    # 22:00 UTC Dec 31 = 01:00 MSK Jan 1 → "до 1 января", not "до 31 декабря".
    rem = _rem(
        "rem_" + "4" * 24, "разминка",
        datetime(2026, 6, 18, 6, 0, tzinfo=UTC),
        "FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231T220000Z",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "до 1 января" in out


# --- M3: display uses user-frame BYDAY weekday (reject, with proof) ---------


def test_near_midnight_weekday_is_user_frame() -> None:
    # 2026-06-15 is Monday. 22:00 UTC Mon = 01:00 MSK Tue. The reminder was
    # set "по понедельникам в 01:00 MSK" → RRULE keeps BYDAY=MO (MSK frame,
    # per schedule_reminder convention). Display must show the user's intent:
    # "по пн в 01:00", NOT the UTC weekday.
    rem = _rem(
        "rem_" + "5" * 24, "разминка",
        datetime(2026, 6, 15, 22, 0, tzinfo=UTC),
        "FREQ=WEEKLY;BYDAY=MO",
    )
    out = _format_reminder_for_llm(rem, MSK)
    assert "по пн" in out
    assert "01:00" in out


# --- M4: guards count language letters, ignore structural ASCII (Codex R1) --


def test_guard_english_still_flagged() -> None:
    from sreda.runtime.handlers import _is_predominantly_non_russian as nr

    assert nr("The request was rejected because it was considered high risk.")


def test_guard_russian_with_dates_and_ids_not_flagged() -> None:
    from sreda.runtime.handlers import _is_predominantly_non_russian as nr

    txt = (
        "Записала в список: молоко, хлеб. "
        "Напомню 2026-06-17 в 09:00. Ссылка: https://example.com/x rem_abc123def456"
    )
    assert not nr(txt)


def test_raw_reminder_list_passes_under_m4() -> None:
    # The pre-fix RAW tool/rot output (ISO + RRULE, no humanizing). M4 alone —
    # counting cyrillic over language letters after stripping ids/ISO/numbers —
    # must un-blank it (the systemic fix), independent of humanization.
    raw = (
        "Напоминания:\n"
        "— разминка → 2026-06-17T06:00:00+00:00\n"
        "— заняться разминкой → 2026-06-17T06:00:00+00:00\n"
        "— разминка → 2026-06-18T06:00:00+00:00 (recurring: FREQ=WEEKLY;BYDAY=MO,WE,FR)\n"
        "— разминка → 2026-06-19T06:00:00+00:00\n"
        "— заняться разминкой → 2026-06-22T06:00:00+00:00 "
        "(recurring: FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261231T060000Z) "
        "Если нужно добавить что-то ещё, дай знать!"
    )
    assert not _is_predominantly_non_russian(raw)
    assert not _is_reasoning_leak_after_tool(raw, {"list_reminders"})


# --- M5: substitution alert includes text only for internal tenants ---------


def test_alert_text_internal_only(monkeypatch) -> None:
    import sreda.runtime.handlers as h

    captured: list[dict] = []
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert",
        lambda **kw: captured.append(kw),
    )

    class _Settings:
        planner_enabled_tenants = frozenset({"tenant_internal"})

    monkeypatch.setattr(h, "get_settings", lambda: _Settings())

    secret = "секрет пользователя про здоровье"
    h._alert_reply_substituted(
        reason="guards:non_russian", tenant_id="tenant_internal",
        feature_key="hw", text=secret,
    )
    h._alert_reply_substituted(
        reason="guards:non_russian", tenant_id="tenant_external",
        feature_key="hw", text=secret,
    )
    assert len(captured) == 2
    assert secret in captured[0]["body"]  # internal → full text
    assert secret not in captured[1]["body"]  # external → redacted
    assert "tenant_external" in captured[1]["body"]  # but tenant/guard kept
    assert captured[1]["dedupe_key"].startswith("rot_substituted:")
