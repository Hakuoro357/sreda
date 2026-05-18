"""R-39: тесты детерминированного рендеринга времени в русский формат.

``format_trigger_human(trigger_iso_utc, user_tz, now_user)`` —
pure-функция: на вход целевое время в UTC + часовой пояс +
текущий момент в этом поясе, на выход короткая русская фраза
(«сегодня в 14:00», «завтра в 9:00», «в среду в 18:00», «22 мая в
10:00»).

Эти строки идут в первую строку подтверждения (детерминированную) —
LLM их не пишет.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sreda.services.date_formatter import format_trigger_human


MSK = ZoneInfo("Europe/Moscow")


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


# ─── Базовые относительные дни ─────────────────────────────────────────


def test_today_simple_hours() -> None:
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 5, 18, 11, 0)  # 14:00 MSK
    assert format_trigger_human(target, "Europe/Moscow", now) == "сегодня в 14:00"


def test_tomorrow() -> None:
    now = _msk(2026, 5, 18, 22, 0)
    target = _utc(2026, 5, 19, 6, 0)  # завтра 09:00 MSK
    assert format_trigger_human(target, "Europe/Moscow", now) == "завтра в 9:00"


def test_day_after_tomorrow() -> None:
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 5, 20, 7, 0)  # послезавтра 10:00 MSK
    assert format_trigger_human(target, "Europe/Moscow", now) == "послезавтра в 10:00"


# ─── Дни недели на ближайшую неделю ───────────────────────────────────


def test_weekday_within_week() -> None:
    """Если разница 3..7 дней — называем день недели."""
    now = _msk(2026, 5, 18, 10, 0)  # понедельник
    # +3 дня → четверг
    target = _utc(2026, 5, 21, 11, 0)
    assert format_trigger_human(target, "Europe/Moscow", now) == "в четверг в 14:00"


def test_weekday_six_days_ahead() -> None:
    now = _msk(2026, 5, 18, 10, 0)  # понедельник
    # +6 дней → воскресенье
    target = _utc(2026, 5, 24, 11, 0)
    assert format_trigger_human(target, "Europe/Moscow", now) == "в воскресенье в 14:00"


# ─── Далеко в будущем — дата ───────────────────────────────────────────


def test_more_than_week_uses_date() -> None:
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 6, 1, 9, 0)  # 12:00 MSK 1 июня
    assert format_trigger_human(target, "Europe/Moscow", now) == "1 июня в 12:00"


def test_distant_date_with_year_omitted_same_year() -> None:
    """Год не упоминаем если этот же календарный год."""
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 12, 31, 18, 0)  # 21:00 MSK 31 декабря
    assert format_trigger_human(target, "Europe/Moscow", now) == "31 декабря в 21:00"


def test_distant_date_includes_year_when_different() -> None:
    """Следующий год — указываем."""
    now = _msk(2026, 12, 30, 10, 0)
    target = _utc(2027, 1, 5, 7, 0)  # 10:00 MSK 5 января 2027
    result = format_trigger_human(target, "Europe/Moscow", now)
    assert "2027" in result
    assert "10:00" in result


# ─── Граница «полночь» и «через час сегодня же» ───────────────────────


def test_today_late_evening() -> None:
    now = _msk(2026, 5, 18, 21, 0)
    target = _utc(2026, 5, 18, 20, 30)  # 23:30 MSK
    assert format_trigger_human(target, "Europe/Moscow", now) == "сегодня в 23:30"


def test_tomorrow_one_minute_after_midnight() -> None:
    now = _msk(2026, 5, 18, 23, 0)
    target = _utc(2026, 5, 18, 21, 1)  # 00:01 MSK следующего дня
    assert format_trigger_human(target, "Europe/Moscow", now) == "завтра в 0:01"


# ─── Минуты ────────────────────────────────────────────────────────────


def test_minutes_padded_to_two_digits() -> None:
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 5, 18, 11, 5)  # 14:05 MSK
    assert format_trigger_human(target, "Europe/Moscow", now) == "сегодня в 14:05"


def test_hour_not_padded() -> None:
    """9:00, не 09:00 — соответствует разговорной речи."""
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 5, 19, 6, 0)  # 9:00 MSK завтра
    result = format_trigger_human(target, "Europe/Moscow", now)
    assert result == "завтра в 9:00"
    assert "09:00" not in result


# ─── Кати-сценарий (главный регрессионный) ─────────────────────────────


def test_past_time_marked_with_bylo() -> None:
    """R-39 review MINOR 1: past time не должно выглядеть как future.

    Защитная мера — parser отлавливает «вчера» как Invalid, но если
    каким-то путём past datetime попало в renderer, явно помечаем «было».
    """
    now = _msk(2026, 5, 18, 10, 0)
    target = _utc(2026, 5, 17, 11, 0)  # вчера 14:00 MSK
    result = format_trigger_human(target, "Europe/Moscow", now)
    assert result.startswith("было")
    assert "14:00" in result


def test_kati_correction_time() -> None:
    """14:00 MSK сегодня — целевой тайминг исходного бага."""
    now = _msk(2026, 5, 17, 13, 21)
    target = _utc(2026, 5, 17, 11, 0)  # 14:00 MSK
    assert format_trigger_human(target, "Europe/Moscow", now) == "сегодня в 14:00"
