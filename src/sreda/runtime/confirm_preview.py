"""#338 часть 2: человекочитаемые подтверждения (кандидат-confirm B2b-2).

БИБЛИЯ (g-075, владелец 2026-07-10): никакие технические данные юзеру - ни имён
инструментов, ни ISO-дат, ни сырых аргументов. Прод-инцидент 755682022: юзер увидел
«Я поняла как «schedule_reminder» (title=…, trigger_iso=2026-08-18T15:00:00+03:00)».

Дизайн (принят владельцем): факты договора (код) → ДОПУСТИМЫЕ человеческие
формулировки по календарю (код) → живая фраза «рта» в персоне (LLM, отдельная
проводка) → железная проверка (код, этот модуль) → сбой = человеческий шаблон.
«Завтра в три часа дня» - допустимо, потому что вычислено календарным кодом,
не фантазией модели.

Модуль - ЧИСТЫЕ функции (без БД, без LLM): извлечение фактов из kwargs инструмента,
генераторы формулировок, шаблон-фолбэк, верификатор. Вызов «рта» - в react_loop
(_generic_confirm_wrap), с фолбэком на fallback_template при любом сбое.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

_MSK = ZoneInfo("Europe/Moscow")

_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
_WEEKDAYS_RU_LOC = ("в понедельник", "во вторник", "в среду", "в четверг",
                    "в пятницу", "в субботу", "в воскресенье")
# числительные для «в N часа/часов …»
_HOUR_WORDS = {1: "час", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть",
               7: "семь", 8: "восемь", 9: "девять", 10: "десять", 11: "одиннадцать",
               12: "двенадцать"}


@dataclass(frozen=True)
class ConfirmFacts:
    """Факты договора: что подтверждаем. when_local - в МСК (для рендера)."""
    action: str            # человеческое действие («поставить напоминание»)
    object_title: str      # название объекта (дословно в тексте)
    when_local: datetime | None  # момент (None - действию время не нужно)


def confirm_facts(tool_name: str, kwargs: dict) -> ConfirmFacts | None:
    """Факты из аргументов инструмента. Неизвестный инструмент → None (generic-путь).
    Реестр расширяется по мере надобности (write-инструменты под кандидат-confirm)."""
    if tool_name == "schedule_reminder":
        title = str(kwargs.get("title") or "").strip()
        raw = str(kwargs.get("trigger_iso") or "").strip()
        if not title or not raw:
            return None
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo("UTC"))
        return ConfirmFacts(action="поставить напоминание",
                            object_title=title,
                            when_local=when.astimezone(_MSK))
    return None


def allowed_day_phrases(when: datetime, *, now: datetime) -> list[str]:
    """Допустимые человеческие названия ДНЯ по календарю: «сегодня»/«завтра»/
    «послезавтра», «18 августа», «во вторник» (день недели - только в ближайшую
    неделю, иначе неоднозначен)."""
    w, n = when.astimezone(_MSK), now.astimezone(_MSK)
    delta = (w.date() - n.date()).days
    out: list[str] = []
    if delta == 0:
        out.append("сегодня")
    elif delta == 1:
        out.append("завтра")
    elif delta == 2:
        out.append("послезавтра")
    out.append(f"{w.day} {_MONTHS_RU[w.month - 1]}")
    if 0 <= delta < 7:
        out.append(_WEEKDAYS_RU_LOC[w.weekday()])
    return out


def allowed_time_phrases(when: datetime) -> list[str]:
    """Допустимые названия ВРЕМЕНИ: «в 15:00» всегда; для ровных часов - живые
    формы «в три часа дня» / «в девять утра» / «в семь вечера»."""
    w = when.astimezone(_MSK)
    out = [f"в {w.hour}:{w.minute:02d}"]
    if w.minute == 0:
        h12 = w.hour % 12 or 12
        word = _HOUR_WORDS.get(h12)
        if word:
            if 5 <= w.hour < 12:
                part = "утра"
            elif 12 <= w.hour < 17:
                part = "дня"
            elif 17 <= w.hour < 23:
                part = "вечера"
            else:
                part = "ночи"
            if h12 == 1:
                out.append(f"в час {part}")
            else:
                hour_noun = "часа" if h12 in (2, 3, 4) else "часов"
                out.append(f"в {word} {hour_noun} {part}")
                out.append(f"в {word} {part}")  # разговорное «в три дня» опускаем? нет: «в три дня» двусмысленно
    return out


def fallback_template(facts: ConfirmFacts, *, now: datetime) -> str:
    """Точный человеческий шаблон (запасной путь при сбое «рта»/проверки).
    Без технических данных: русская дата, время, название в кавычках."""
    if facts.when_local is not None:
        # календарная дата, не «завтра»: фолбэк обязан быть однозначным даже если
        # юзер прочтёт сообщение после полуночи (относительный день протухает)
        w = facts.when_local
        day = f"{w.day} {_MONTHS_RU[w.month - 1]}"
        t = f"в {w.hour}:{w.minute:02d}"
        return (f"Ставлю напоминание «{facts.object_title}» {day} {t}. "
                f"Подтверждаешь?")
    return f"Сделать: {facts.action} «{facts.object_title}»?"


# Русские действия для write-инструментов под кандидат-confirm, у которых нет
# полного факт-рендера (нет времени/особой структуры). Объект - из типовых
# аргументов (title/name), дословно в кавычках. Реестр пополняется по мере
# появления инструментов в candidate-потоке (канарейка покажет).
_ACTIONS_RU = {
    "add_task": "добавить задачу",
    "complete_task": "отметить задачу выполненной",
    "add_checklist_items": "добавить в список",
    "add_shopping_items": "добавить в покупки",
    "update_reminder": "изменить напоминание",
    "update_shopping_item": "изменить покупку",
    "create_checklist": "создать список",
    "save_core_fact": "запомнить",
    "save_episode": "запомнить",
    "add_recipe": "сохранить рецепт",
    "update_task": "изменить задачу",
}


def _object_from_kwargs(kwargs: dict) -> str:
    """Название объекта из типовых аргументов (для человеческого вопроса)."""
    for key in ("title", "name", "text", "fact", "query"):
        v = kwargs.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    items = kwargs.get("items")
    if isinstance(items, (list, tuple)) and items:
        first = items[0]
        if isinstance(first, str) and first.strip():
            more = f" и ещё {len(items) - 1}" if len(items) > 1 else ""
            return first.strip() + more
    return ""


def generic_action_question(tool_name: str, kwargs: dict) -> str:
    """Человеческий вопрос подтверждения для инструмента БЕЗ факт-рендера.
    БИБЛИЯ (g-075): без имени инструмента, без сырых аргументов, без ISO."""
    action = _ACTIONS_RU.get(tool_name)
    obj = _object_from_kwargs(kwargs)
    if action and obj:
        return f"Хочу {action} «{obj}». Подтверждаешь?"
    if action:
        return f"Хочу {action}. Подтверждаешь?"
    return "Подтверди, пожалуйста: сделать это изменение?"


def build_mouth_prompt(facts: ConfirmFacts, *, now: datetime) -> tuple[str, str]:
    """(system, user) для «рта»: одна живая фраза подтверждения в персоне Среды.
    Факты и ДОПУСТИМЫЕ формулировки даны; менять факты запрещено. Результат
    проверяет verify_confirm_text - сбой = fallback_template (рот не источник
    истины, только голос)."""
    system = (
        "Ты - Среда, тёплая семейная помощница. Составь ОДНУ короткую фразу-"
        "подтверждение действия. Правила железные: название объекта - ДОСЛОВНО в "
        "кавычках «…»; день и время - ТОЛЬКО из списка допустимых формулировок "
        "(выбери одну для дня, одну для времени); никаких других дат, чисел, "
        "английских слов; закончи вопросом подтверждения (например «подтверждаешь?»). "
        "Не добавляй ничего, кроме этой фразы.")
    if facts.when_local is not None:
        days = " / ".join(allowed_day_phrases(facts.when_local, now=now))
        times = " / ".join(allowed_time_phrases(facts.when_local))
        user = (f"Действие: {facts.action}. Название: «{facts.object_title}». "
                f"Допустимые формулировки дня: {days}. "
                f"Допустимые формулировки времени: {times}.")
    else:
        user = f"Действие: {facts.action}. Название: «{facts.object_title}»."
    return system, user


_TECH_RE = re.compile(
    r"[a-z_]{3,}|"                # латиница подряд (имена инструментов/полей)
    r"\d{4}-\d{2}-\d{2}|"         # ISO-дата
    r"trigger_iso|title=|=",      # сырые аргументы
    re.IGNORECASE)
_ANY_DATE_RE = re.compile(r"\b(\d{1,2})\s+(январ|феврал|март|апрел|ма[яе]|июн|июл|"
                          r"август|сентябр|октябр|ноябр|декабр)")
_ANY_TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")


def verify_confirm_text(text: str, facts: ConfirmFacts, *, now: datetime) -> bool:
    """Железная проверка фразы «рта» перед отправкой юзеру.
    Требования: название дословно; ровно допустимые день и время (если у действия
    есть момент); вопрос подтверждения; НИКАКОЙ технической начинки; нет посторонних
    дат/времён (рот не имеет права называть числа, которых нет в фактах)."""
    t = (text or "").strip()
    if not t or not t.rstrip().endswith("?"):
        return False
    if _TECH_RE.search(t):
        return False
    if facts.object_title not in t:
        return False
    if facts.when_local is None:
        return True
    lower = t.lower()
    days = allowed_day_phrases(facts.when_local, now=now)
    times = allowed_time_phrases(facts.when_local)
    if not any(d in lower for d in days):
        return False
    if not any(x in lower for x in times):
        return False
    # посторонние даты: любое «D <месяц>» вне допустимого дня
    w = facts.when_local
    for m in _ANY_DATE_RE.finditer(lower):
        if int(m.group(1)) != w.day:
            return False
    # посторонние времена: любое HH:MM, отличное от факта
    for m in _ANY_TIME_RE.finditer(lower):
        if int(m.group(1)) != w.hour or int(m.group(2)) != w.minute:
            return False
    return True
