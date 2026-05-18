"""R-39 День 1: тесты парсера времени из текста пользователя.

Парсер возвращает типизированный результат:
- TimeResolved — однозначное распознавание
- TimeAmbiguous — два-три кандидата (например «на 2 часа» = через 2ч или в 14:00)
- TimeInvalid — распознали но недопустимо (прошлое для напоминания, out_of_range)
- TimeUnrecognized — фрагмент не распознан (откладывается на R-40)

Объём v1 (в R-39):
- ISO формат
- «ЧЧ:ММ»
- «через N часов/минут/полчаса/час»
- «завтра/сегодня/послезавтра в ЧЧ»
- «в N часов» (с эвристикой утра/дня/вечера/ночи)
- «в N утра/дня/вечера/ночи»

Москва (UTC+3) — часовой пояс по умолчанию для России.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sreda.services.natural_time_parser import (
    TimeAmbiguous,
    TimeInvalid,
    TimeResolved,
    TimeUnrecognized,
    parse_natural_time,
)

# Фиксированный момент для всех тестов: 18 мая 2026, 10:00 UTC = 13:00 MSK
NOW_UTC = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
USER_TZ = "Europe/Moscow"
MSK = ZoneInfo("Europe/Moscow")


# ─── ISO формат ─────────────────────────────────────────────────────────


def test_iso_with_offset() -> None:
    """ISO с явным offset → Resolved."""
    result = parse_natural_time("2026-05-18T14:00:00+03:00", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved)
    assert result.iso_utc == datetime(2026, 5, 18, 11, 0, 0, tzinfo=timezone.utc)
    assert result.timezone_source == "explicit_in_text"


def test_iso_z_format() -> None:
    """ISO с Z (UTC) → Resolved."""
    result = parse_natural_time("Поставь на 2026-05-18T11:00:00Z", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved)
    assert result.iso_utc == datetime(2026, 5, 18, 11, 0, 0, tzinfo=timezone.utc)


# ─── «ЧЧ:ММ» ─────────────────────────────────────────────────────────────


def test_hh_mm_today_future() -> None:
    """«14:00» в 13:00 МСК → сегодня 14:00 МСК → 11:00 UTC."""
    result = parse_natural_time("Поставь на 14:00", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 14, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected
    assert result.iso_utc == expected.astimezone(timezone.utc)


def test_hh_mm_today_already_passed_rolls_to_tomorrow() -> None:
    """«09:00» в 13:00 МСК — уже прошло → завтра 09:00."""
    result = parse_natural_time("в 09:00", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    # 9:00 уже прошло сегодня (текущее 13:00), значит завтра
    expected = datetime(2026, 5, 19, 9, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


# ─── «через N часов/минут/полчаса» ──────────────────────────────────────


def test_cherez_2_chasa() -> None:
    """«через 2 часа» в 13:00 МСК → 15:00 МСК."""
    result = parse_natural_time("напомни через 2 часа", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    # 13:00 MSK + 2h = 15:00 MSK
    expected = datetime(2026, 5, 18, 15, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_cherez_30_minut() -> None:
    """«через 30 минут» в 13:00 МСК → 13:30 МСК."""
    result = parse_natural_time("через 30 минут", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 13, 30, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_cherez_polchasa() -> None:
    """«через полчаса» → +30 минут."""
    result = parse_natural_time("позвони через полчаса", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 13, 30, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_cherez_chas() -> None:
    """«через час» (без числа) → +1 час."""
    result = parse_natural_time("через час", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 14, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


# ─── «сегодня/завтра/послезавтра в ЧЧ» ──────────────────────────────────


def test_zavtra_v_9() -> None:
    """«завтра в 9» → завтра 09:00 МСК."""
    result = parse_natural_time("завтра в 9", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 19, 9, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_segodnya_v_18() -> None:
    """«сегодня в 18:00» → сегодня 18:00 МСК."""
    result = parse_natural_time("сегодня в 18:00 встреча", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 18, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_poslezavtra_v_10() -> None:
    """«послезавтра в 10» → послезавтра 10:00 МСК."""
    result = parse_natural_time("послезавтра в 10", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 20, 10, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


# ─── «в N утра/дня/вечера/ночи» ──────────────────────────────────────────


def test_v_9_utra() -> None:
    """«в 9 утра» → сегодня 09:00 МСК (или завтра если прошло).
    13:00 сейчас, 9 утра прошло → завтра."""
    result = parse_natural_time("в 9 утра", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 19, 9, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_v_2_chasa_dnya() -> None:
    """«в 2 часа дня» → сегодня 14:00 МСК. КЛЮЧЕВОЙ КЕЙС КАТИ."""
    result = parse_natural_time("поставь на 2 часа дня. Разбудить Катю", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 14, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_v_9_vechera() -> None:
    """«в 9 вечера» → сегодня 21:00 МСК."""
    result = parse_natural_time("в 9 вечера", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 21, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_v_11_nochi() -> None:
    """«в 11 ночи» → сегодня 23:00 МСК."""
    result = parse_natural_time("в 11 ночи", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 23, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected


def test_v_5_vechera_returns_17() -> None:
    """R-39 review MAJOR 1: «5 вечера» = 17:00 (раньше выпадало в Unrecognized)."""
    result = parse_natural_time("напомни в 5 вечера зайти за хлебом", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    assert result.iso_user_tz.hour == 17


def test_v_9_nochi_returns_21() -> None:
    """R-39 review MAJOR 2: «9 ночи» = 21:00 (раньше gap 5..9)."""
    result = parse_natural_time("в 9 ночи позвонить маме", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    assert result.iso_user_tz.hour == 21


# ─── Ambiguous (двусмысленный) ─────────────────────────────────────────


def test_na_2_chasa_ambiguous() -> None:
    """«на 2 часа» — кандидаты: через 2 часа ИЛИ в 14:00. Без уточнения дня/часов."""
    result = parse_natural_time("поставь на 2 часа разбудить", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeAmbiguous), f"got: {result}"
    # Два кандидата: через 2ч (15:00) и в 14:00 МСК
    times_msk = sorted(c.iso_user_tz for c in result.candidates)
    assert datetime(2026, 5, 18, 14, 0, 0, tzinfo=MSK) in times_msk
    assert datetime(2026, 5, 18, 15, 0, 0, tzinfo=MSK) in times_msk


def test_v_3_chasa_without_qualifier() -> None:
    """«в 3 часа» без уточнения утра/дня → ambiguous: 03:00 ночью или 15:00 днём."""
    result = parse_natural_time("в 3 часа", USER_TZ, NOW_UTC)
    # Может быть Resolved с эвристикой (3 утра прошло → завтра 03:00; ИЛИ 15:00 сегодня)
    # Допустим оба варианта: Ambiguous предпочтительнее
    assert isinstance(result, (TimeAmbiguous, TimeResolved)), f"got: {result}"


# ─── Invalid (распознали но недопустимо) ───────────────────────────────


def test_vchera_invalid() -> None:
    """«вчера в 14:00» → Invalid past_date — для напоминаний нельзя."""
    result = parse_natural_time("вчера в 14:00", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeInvalid), f"got: {result}"
    assert result.reason == "past_date"


def test_v_25_chasov_invalid() -> None:
    """«в 25:00» out_of_range."""
    result = parse_natural_time("в 25:00", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeInvalid), f"got: {result}"
    assert result.reason == "out_of_range"


# ─── Unrecognized (откладывается на R-40) ──────────────────────────────


def test_posle_obeda_unrecognized() -> None:
    """«после обеда» → Unrecognized (нет конкретного времени, требует knowledge)."""
    result = parse_natural_time("напомни после обеда", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeUnrecognized), f"got: {result}"


def test_v_sredu_unrecognized() -> None:
    """«в среду» → Unrecognized (день недели — R-40)."""
    result = parse_natural_time("в среду", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeUnrecognized), f"got: {result}"


def test_potom_unrecognized() -> None:
    """«потом» → Unrecognized."""
    result = parse_natural_time("сделай потом", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeUnrecognized), f"got: {result}"


def test_no_time_in_text_unrecognized() -> None:
    """Текст без упоминания времени → Unrecognized."""
    result = parse_natural_time("разбудить Катю", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeUnrecognized), f"got: {result}"


def test_empty_string_unrecognized() -> None:
    """Пустая строка → Unrecognized."""
    result = parse_natural_time("", USER_TZ, NOW_UTC)
    assert isinstance(result, TimeUnrecognized), f"got: {result}"


# ─── source_span (где в тексте найдено) ──────────────────────────────────


def test_source_span_for_hh_mm() -> None:
    """Resolved содержит source_span — позиции в исходном тексте."""
    text = "Поставь на 14:00 разбудить"
    result = parse_natural_time(text, USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved)
    start, end = result.source_span
    assert text[start:end] == "14:00"


def test_source_span_for_cherez() -> None:
    text = "напомни через 2 часа купить хлеб"
    result = parse_natural_time(text, USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved)
    start, end = result.source_span
    assert "через 2 часа" in text[start:end] or "2 часа" in text[start:end]


# ─── Кати regression (полный текст) ─────────────────────────────────────


def test_kati_correction_full_text() -> None:
    """Полный текст исправления Кати (turn 2):
    «Нет, это неправильное напоминание. Убери. Поставь напоминание
    на 2 часа дня. Разбудить Катю.»
    Парсер должен извлечь «на 2 часа дня» → 14:00 МСК.
    """
    text = (
        "Нет, это неправильное напоминание. Убери. "
        "Поставь напоминание на 2 часа дня. Разбудить Катю."
    )
    result = parse_natural_time(text, USER_TZ, NOW_UTC)
    assert isinstance(result, TimeResolved), f"got: {result}"
    expected = datetime(2026, 5, 18, 14, 0, 0, tzinfo=MSK)
    assert result.iso_user_tz == expected
