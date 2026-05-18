"""R-39: детерминированный рендер времени в русскую разговорную форму.

Используется в первой строке подтверждения (полу-шаблон). LLM не
участвует — строго из ISO datetime + user_tz + now_user.

Примеры вывода (now = понедельник 18 мая 2026, 10:00 MSK):

    "сегодня в 14:00"
    "завтра в 9:00"
    "послезавтра в 10:00"
    "в четверг в 14:00"            (через 3 дня)
    "в воскресенье в 14:00"        (через 6 дней)
    "1 июня в 12:00"               (через >7 дней этого года)
    "5 января 2027 в 10:00"        (следующий год)
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


# ─── Словари склонений ────────────────────────────────────────────────


# Винительный падеж — «в среду», «в воскресенье»
_WEEKDAY_ACC: tuple[str, ...] = (
    "понедельник",   # 0
    "вторник",       # 1
    "среду",         # 2
    "четверг",       # 3
    "пятницу",       # 4
    "субботу",       # 5
    "воскресенье",   # 6
)


# Родительный падеж — «1 июня», «31 декабря»
_MONTH_GEN: tuple[str, ...] = (
    "",          # 0 — заглушка чтобы индекс совпадал с .month
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


# ─── Главная функция ──────────────────────────────────────────────────


def format_trigger_human(
    trigger_iso_utc: datetime,
    user_tz: str,
    now_user: datetime,
) -> str:
    """Рендер времени напоминания в русскую разговорную форму.

    Args:
        trigger_iso_utc: целевой момент (timezone-aware, UTC или любой).
        user_tz: IANA-имя часового пояса пользователя ("Europe/Moscow").
        now_user: текущий момент в часовом поясе пользователя
                  (timezone-aware, ``tzinfo`` должен совпадать с ``user_tz``).

    Returns:
        Короткая русская фраза («сегодня в 14:00», «в среду в 9:00»,
        «22 мая в 12:00», «5 января 2027 в 10:00»).
    """
    tz = ZoneInfo(user_tz)
    target = trigger_iso_utc.astimezone(tz)

    delta_days = (target.date() - now_user.date()).days
    time_part = _format_time(target.hour, target.minute)

    # Защитная мера: время в прошлом не должно доходить до этого слоя
    # (parser возвращает Invalid для past). Если всё же дошло — явно
    # помечаем чтобы пользователь увидел «было», а не принял за будущее.
    if delta_days < 0:
        return f"было {target.day} {_MONTH_GEN[target.month]} в {time_part}"

    # Если год отличается — всегда полная дата (день+месяц+год).
    # Иначе «в среду» через год было бы двусмысленно («в эту среду или
    # через год?»).
    if target.year != now_user.year:
        return f"{target.day} {_MONTH_GEN[target.month]} {target.year} в {time_part}"

    if delta_days == 0:
        return f"сегодня в {time_part}"
    if delta_days == 1:
        return f"завтра в {time_part}"
    if delta_days == 2:
        return f"послезавтра в {time_part}"
    if 3 <= delta_days <= 6:
        weekday = _WEEKDAY_ACC[target.weekday()]
        return f"в {weekday} в {time_part}"

    # 7+ дней этого же года: дата без года
    return f"{target.day} {_MONTH_GEN[target.month]} в {time_part}"


# ─── Внутренние помощники ─────────────────────────────────────────────


def _format_time(hour: int, minute: int) -> str:
    """Часы без ведущего нуля, минуты — с zero-pad: '9:00', '14:05'."""
    return f"{hour}:{minute:02d}"
