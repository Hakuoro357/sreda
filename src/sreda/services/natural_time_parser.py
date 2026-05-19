"""R-39: парсер времени из текста пользователя.

Возвращает типизированный результат:
- TimeResolved — однозначное распознавание времени
- TimeAmbiguous — несколько кандидатов (например «на 2 часа» = через 2ч ИЛИ в 14:00)
- TimeInvalid — распознали но недопустимо (прошлое для напоминаний, out_of_range)
- TimeUnrecognized — фрагмент времени не распознан (R-40 расширит)

Объём v1 (R-39):
- ISO формат
- «ЧЧ:ММ» (с эвристикой откатить на завтра если прошёл)
- «через N часов/минут/полчаса/час»
- «завтра/сегодня/послезавтра в ЧЧ[:ММ]»
- «в N утра/дня/вечера/ночи»
- «в N часов» без квалификатора → Ambiguous (утра или дня)

Российский часовой пояс по умолчанию (Europe/Moscow, UTC+3).

R-40 расширит на: «после обеда», дни недели, абстрактные периоды.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo


# ─── Типы результата ───────────────────────────────────────────────────


@dataclass
class TimeResolved:
    """Однозначно распознанное время."""

    iso_utc: datetime  # время в UTC
    iso_user_tz: datetime  # время в часовом поясе пользователя
    source_span: tuple[int, int]  # позиции в исходном тексте
    timezone_source: str  # "user_profile" | "explicit_in_text"


@dataclass
class TimeAmbiguous:
    """Двусмысленное время — несколько кандидатов."""

    candidates: list[TimeResolved]
    reason: str  # "через_N_часов_или_в_N" | "noon_or_midnight"


@dataclass
class TimeInvalid:
    """Распознали но недопустимо для напоминаний."""

    raw: str
    reason: str  # "past_date" | "out_of_range"


@dataclass
class TimeUnrecognized:
    """Время не распознано в объёме v1."""

    raw: str


ParseResult = Union[TimeResolved, TimeAmbiguous, TimeInvalid, TimeUnrecognized]


# ─── Главная функция ───────────────────────────────────────────────────


def parse_natural_time(
    text: str, user_tz: str, now_utc: datetime
) -> ParseResult:
    """Парсит время из русского текста пользователя.

    Args:
        text: исходный текст пользователя
        user_tz: часовой пояс пользователя в IANA формате ("Europe/Moscow")
        now_utc: текущий момент в UTC

    Returns:
        Один из четырёх типизированных результатов.
    """
    if not text or not text.strip():
        return TimeUnrecognized(raw=text)

    tz = ZoneInfo(user_tz)
    now_user = now_utc.astimezone(tz)
    low = text.lower()

    # 1. ISO формат — приоритетный (явное указание + offset)
    iso_result = _try_parse_iso(text, tz)
    if iso_result is not None:
        return iso_result

    # 2. «вчера/позавчера» = past_date Invalid (для напоминаний нельзя)
    if re.search(r"\b(вчера|позавчера)\b", low):
        return TimeInvalid(raw=text, reason="past_date")

    # 3. Out-of-range ЧЧ:ММ (например 25:00)
    out_match = re.search(r"\b(\d{2,3}):(\d{2})\b", text)
    if out_match:
        hh = int(out_match.group(1))
        mm = int(out_match.group(2))
        if hh > 23 or mm > 59:
            return TimeInvalid(raw=text, reason="out_of_range")

    # 4. «через N часов/минут/полчаса/час»
    cherez = _try_parse_cherez(text, low, now_user, tz)
    if cherez is not None:
        return cherez

    # 5. «завтра/сегодня/послезавтра в ЧЧ[:ММ]»
    day_in = _try_parse_day_in_time(text, low, now_user, tz)
    if day_in is not None:
        return day_in

    # 6. «N утра/дня/вечера/ночи» (с/без «в», с/без «часа/часов»)
    v_n_period = _try_parse_v_n_period(text, low, now_user, tz)
    if v_n_period is not None:
        # Может вернуть Resolved (квалификатор однозначный)
        if isinstance(v_n_period, TimeAmbiguous):
            return v_n_period
        # Дополнительно: если есть «на» рядом — возможна двусмысленность с «через N часов»
        if re.search(r"\bна\s+\d+\s+(час|часа|часов)\s+(утра|дня|вечера|ночи)", low):
            # «на 2 часа дня» — здесь квалификатор «дня» однозначен
            return v_n_period
        return v_n_period

    # 7. «на N часов/минут» БЕЗ квалификатора периода → Ambiguous
    na_n = _try_parse_na_n_chasov(text, low, now_user, tz)
    if na_n is not None:
        return na_n

    # 8. «ЧЧ:ММ» простое
    hh_mm = _try_parse_hh_mm(text, low, now_user, tz)
    if hh_mm is not None:
        return hh_mm

    # 9. «в N часов» без квалификатора периода → Ambiguous
    v_n = _try_parse_v_n_chasov(text, low, now_user, tz)
    if v_n is not None:
        return v_n

    return TimeUnrecognized(raw=text)


# ─── Реализация отдельных шаблонов ─────────────────────────────────────


_ISO_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})"
)


def _try_parse_iso(text: str, tz: ZoneInfo) -> Optional[TimeResolved]:
    """Ищет ISO datetime с явным offset или Z."""
    match = _ISO_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(0)
    # Нормализуем Z → +00:00 для fromisoformat
    iso_str = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    return TimeResolved(
        iso_utc=dt.astimezone(timezone.utc),
        iso_user_tz=dt.astimezone(tz),
        source_span=match.span(),
        timezone_source="explicit_in_text",
    )


_CHEREZ_NUM_HOUR = re.compile(
    r"через\s+(\d+)\s*(?:часов|часа|час|ч\b)",
    re.IGNORECASE,
)
_CHEREZ_NUM_MIN = re.compile(
    r"через\s+(\d+)\s*(?:минуты|минуту|минут|мин\b)",
    re.IGNORECASE,
)
_CHEREZ_POLCHASA = re.compile(r"через\s+полчаса", re.IGNORECASE)
_CHEREZ_CHAS = re.compile(r"через\s+час\b(?!\s*[аов])", re.IGNORECASE)


def _try_parse_cherez(
    text: str, low: str, now_user: datetime, tz: ZoneInfo
) -> Optional[TimeResolved]:
    """«через N часов», «через полчаса», «через час»."""
    # Сначала более специфические
    m = _CHEREZ_POLCHASA.search(text)
    if m:
        dt = now_user + timedelta(minutes=30)
        return TimeResolved(
            iso_utc=dt.astimezone(timezone.utc),
            iso_user_tz=dt,
            source_span=m.span(),
            timezone_source="user_profile",
        )

    m = _CHEREZ_NUM_HOUR.search(text)
    if m:
        n = int(m.group(1))
        dt = now_user + timedelta(hours=n)
        return TimeResolved(
            iso_utc=dt.astimezone(timezone.utc),
            iso_user_tz=dt,
            source_span=m.span(),
            timezone_source="user_profile",
        )

    m = _CHEREZ_NUM_MIN.search(text)
    if m:
        n = int(m.group(1))
        dt = now_user + timedelta(minutes=n)
        return TimeResolved(
            iso_utc=dt.astimezone(timezone.utc),
            iso_user_tz=dt,
            source_span=m.span(),
            timezone_source="user_profile",
        )

    m = _CHEREZ_CHAS.search(text)
    if m:
        dt = now_user + timedelta(hours=1)
        return TimeResolved(
            iso_utc=dt.astimezone(timezone.utc),
            iso_user_tz=dt,
            source_span=m.span(),
            timezone_source="user_profile",
        )

    return None


_DAY_WORDS = {
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}

# 2026-05-19: расширено с `\s+в\s+` до `\s+(?:в|на)\s+` — реальные
# пользовательские формулировки часто используют предлог «на» вместо «в»
# («поставь напоминание на завтра НА 12 потягать гантели»). Без этого
# parser возвращал TimeUnrecognized → planner шёл на LLM → LLM иногда
# просил уточнения вместо schedule_reminder.
_DAY_IN_TIME = re.compile(
    r"\b(сегодня|завтра|послезавтра)\s+(?:в|на)\s+(\d{1,2})(?::(\d{2}))?",
    re.IGNORECASE,
)


def _try_parse_day_in_time(
    text: str, low: str, now_user: datetime, tz: ZoneInfo
) -> Optional[Union[TimeResolved, TimeInvalid]]:
    """«завтра в 9», «сегодня в 18:00», «послезавтра в 10»."""
    m = _DAY_IN_TIME.search(low)
    if not m:
        return None
    day_word = m.group(1)
    hour = int(m.group(2))
    minute = int(m.group(3)) if m.group(3) else 0

    if hour > 23 or minute > 59:
        return TimeInvalid(raw=text, reason="out_of_range")

    offset_days = _DAY_WORDS[day_word]
    target_date = now_user.date() + timedelta(days=offset_days)
    dt = datetime.combine(
        target_date, datetime.min.time().replace(hour=hour, minute=minute), tzinfo=tz
    )

    if dt < now_user:
        return TimeInvalid(raw=text, reason="past_date")

    # Span — позиции по оригинальному тексту через case-insensitive поиск
    span_match = re.search(re.escape(m.group(0)), text, re.IGNORECASE)
    span = span_match.span() if span_match else m.span()

    return TimeResolved(
        iso_utc=dt.astimezone(timezone.utc),
        iso_user_tz=dt,
        source_span=span,
        timezone_source="user_profile",
    )


# «(на)? N (часа|часов)? утра/дня/вечера/ночи»
# (?<!\d) перед \d — чтобы не зацепить «позвони222 утра» (низкий риск,
# но проще исключить структурно).
_N_PERIOD = re.compile(
    r"(?:на\s+|в\s+)?(?<!\d)(\d{1,2})\s*(?:часов|часа|час)?\s+(утра|дня|вечера|ночи)",
    re.IGNORECASE,
)


def _try_parse_v_n_period(
    text: str, low: str, now_user: datetime, tz: ZoneInfo
) -> Optional[TimeResolved]:
    """«N утра/дня/вечера/ночи» с любым префиксом (на/в/без)."""
    m = _N_PERIOD.search(low)
    if not m:
        return None
    n = int(m.group(1))
    period = m.group(2)

    # Маппинг N + период → час 0-23
    hour = _period_to_hour(n, period)
    if hour is None:
        return None  # некорректное сочетание (e.g. «25 утра»)

    candidate = now_user.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate < now_user:
        candidate = candidate + timedelta(days=1)

    span_match = re.search(re.escape(m.group(0)), text, re.IGNORECASE)
    span = span_match.span() if span_match else m.span()

    return TimeResolved(
        iso_utc=candidate.astimezone(timezone.utc),
        iso_user_tz=candidate,
        source_span=span,
        timezone_source="user_profile",
    )


def _period_to_hour(n: int, period: str) -> Optional[int]:
    """N + период → час (0-23). None если некорректно."""
    if period == "утра":
        if 1 <= n <= 11:
            return n
        if n == 12:
            return 0  # «12 утра» = полночь, но это спорно — пропустим
        return None
    if period == "дня":
        if 1 <= n <= 11:
            return n + 12  # 1 дня = 13, 2 дня = 14
        if n == 12:
            return 12  # «12 дня» = полдень
        return None
    if period == "вечера":
        # 5..11 — обычное русское «вечера» (5 вечера = 17, 9 = 21, 11 = 23).
        # 1..4 не покрываем намеренно — в речи это «дня», а не «вечера»;
        # пусть TimeUnrecognized чем перетянуть час у «дня».
        if 5 <= n <= 11:
            return n + 12
        return None
    if period == "ночи":
        if 1 <= n <= 4:
            return n  # 3 ночи = 03:00
        if 5 <= n <= 11:
            return n + 12  # 9 ночи = 21, 11 ночи = 23 (разговорный синоним «вечера»)
        if n == 12:
            return 0  # 12 ночи = полночь
        return None
    return None


# «на N часов/минут» БЕЗ квалификатора периода
_NA_N_HOURS = re.compile(
    r"на\s+(\d{1,2})\s*(?:часов|часа|час)\b(?!\s*(?:утра|дня|вечера|ночи))",
    re.IGNORECASE,
)


def _try_parse_na_n_chasov(
    text: str, low: str, now_user: datetime, tz: ZoneInfo
) -> Optional[TimeAmbiguous]:
    """«на 2 часа» без квалификатора → Ambiguous: через 2 часа ИЛИ в 14:00."""
    m = _NA_N_HOURS.search(low)
    if not m:
        return None
    n = int(m.group(1))
    if n < 1 or n > 23:
        return None

    span_match = re.search(re.escape(m.group(0)), text, re.IGNORECASE)
    span = span_match.span() if span_match else m.span()

    # Кандидат 1: через N часов от сейчас
    via_n_hours = now_user + timedelta(hours=n)
    cand1 = TimeResolved(
        iso_utc=via_n_hours.astimezone(timezone.utc),
        iso_user_tz=via_n_hours,
        source_span=span,
        timezone_source="user_profile",
    )

    # Кандидат 2: в N часов сегодня (с откатом на завтра если прошло)
    # Эвристика: если N <= 11 — день (N+12), иначе N как есть
    hour_of_day = n + 12 if n <= 11 else n
    if hour_of_day > 23:
        return None  # «на 24 часа» — out of range
    candidate_today = now_user.replace(hour=hour_of_day, minute=0, second=0, microsecond=0)
    if candidate_today < now_user:
        candidate_today = candidate_today + timedelta(days=1)
    cand2 = TimeResolved(
        iso_utc=candidate_today.astimezone(timezone.utc),
        iso_user_tz=candidate_today,
        source_span=span,
        timezone_source="user_profile",
    )

    return TimeAmbiguous(
        candidates=[cand1, cand2],
        reason="через_N_часов_или_в_N",
    )


# «в ЧЧ:ММ» или просто ЧЧ:ММ
_HH_MM = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _try_parse_hh_mm(
    text: str, low: str, now_user: datetime, tz: ZoneInfo
) -> Optional[Union[TimeResolved, TimeInvalid]]:
    """ЧЧ:ММ — однозначно (если не прошло — сегодня, иначе завтра)."""
    m = _HH_MM.search(text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh > 23 or mm > 59:
        return TimeInvalid(raw=text, reason="out_of_range")

    candidate = now_user.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate < now_user:
        candidate = candidate + timedelta(days=1)

    return TimeResolved(
        iso_utc=candidate.astimezone(timezone.utc),
        iso_user_tz=candidate,
        source_span=m.span(),
        timezone_source="user_profile",
    )


# «в N часов» БЕЗ квалификатора периода
_V_N_CHASOV = re.compile(
    r"в\s+(\d{1,2})\s+(?:часов|часа|час)\b(?!\s*(?:утра|дня|вечера|ночи))",
    re.IGNORECASE,
)


def _try_parse_v_n_chasov(
    text: str, low: str, now_user: datetime, tz: ZoneInfo
) -> Optional[Union[TimeResolved, TimeAmbiguous]]:
    """«в N часов» без квалификатора периода → Ambiguous."""
    m = _V_N_CHASOV.search(low)
    if not m:
        return None
    n = int(m.group(1))
    if n < 0 or n > 23:
        return None

    span_match = re.search(re.escape(m.group(0)), text, re.IGNORECASE)
    span = span_match.span() if span_match else m.span()

    # Если N >= 13 — однозначно дневное (15 = 15:00)
    if n >= 13:
        candidate = now_user.replace(hour=n, minute=0, second=0, microsecond=0)
        if candidate < now_user:
            candidate = candidate + timedelta(days=1)
        return TimeResolved(
            iso_utc=candidate.astimezone(timezone.utc),
            iso_user_tz=candidate,
            source_span=span,
            timezone_source="user_profile",
        )

    # Иначе N от 0 до 12 → ambiguous (утра или дня)
    # Кандидат 1: N утра (00-12)
    hour_morn = n
    cand1_dt = now_user.replace(hour=hour_morn, minute=0, second=0, microsecond=0)
    if cand1_dt < now_user:
        cand1_dt = cand1_dt + timedelta(days=1)
    cand1 = TimeResolved(
        iso_utc=cand1_dt.astimezone(timezone.utc),
        iso_user_tz=cand1_dt,
        source_span=span,
        timezone_source="user_profile",
    )

    # Кандидат 2: N дня/вечера (N+12)
    if n <= 11:
        hour_day = n + 12
        cand2_dt = now_user.replace(hour=hour_day, minute=0, second=0, microsecond=0)
        if cand2_dt < now_user:
            cand2_dt = cand2_dt + timedelta(days=1)
        cand2 = TimeResolved(
            iso_utc=cand2_dt.astimezone(timezone.utc),
            iso_user_tz=cand2_dt,
            source_span=span,
            timezone_source="user_profile",
        )
        return TimeAmbiguous(
            candidates=[cand1, cand2],
            reason="noon_or_midnight",
        )

    # n == 12 — однозначно «12 часов» = полдень
    return cand1
