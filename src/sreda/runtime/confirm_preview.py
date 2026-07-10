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
    recurrence_human: str = ""   # человеческое «каждый час [до 20:00 / 5 раз]»; "" = разовое


_FREQ_RU = {"HOURLY": "каждый час", "DAILY": "каждый день",
            "WEEKLY": "каждую неделю", "MONTHLY": "каждый месяц"}


def _recurrence_ru(rrule: str) -> str | None:
    """RRULE → человеческая фраза повтора. Не смогли безопасно очеловечить →
    None (R1 Codex high: подтверждение НЕ должно скрывать/перевирать повтор -
    лучше generic-вопрос, чем неверный договор)."""
    r = (rrule or "").strip().upper()
    if not r:
        return ""
    parts = dict(p.split("=", 1) for p in r.split(";") if "=" in p)
    # R2 medium: ТОЛЬКО полностью очеловечиваемые комбинации. FREQ=WEEKLY;BYDAY=MO;COUNT=5
    # раньше рендерился «каждую неделю, всего 5 раз», СКРЫВАЯ понедельник - юзер
    # подтверждал не тот договор. Любой ключ вне {FREQ,BYDAY}+{COUNT|UNTIL} → None (generic).
    if set(parts) - {"FREQ", "BYDAY", "COUNT", "UNTIL"}:
        return None
    if "COUNT" in parts and "UNTIL" in parts:
        return None
    base = _FREQ_RU.get(parts.get("FREQ", ""))
    if base is None:
        return None
    # R2 Codex high: BYDAY рендерим ЯВНО (не прячем за generic) - но только для WEEKLY
    # и только известные дни; иначе None (не рискуем переврать расписание).
    if "BYDAY" in parts:
        if parts.get("FREQ") != "WEEKLY":
            return None
        _by = {"MO": "понедельникам", "TU": "вторникам", "WE": "средам", "TH": "четвергам",
               "FR": "пятницам", "SA": "субботам", "SU": "воскресеньям"}
        try:
            days_ru = [_by[d.strip()] for d in parts["BYDAY"].split(",")]
        except KeyError:
            return None
        base = "по " + " и ".join(days_ru)
    if "COUNT" in parts:
        try:
            n = int(parts["COUNT"])
        except ValueError:
            return None
        return f"{base}, всего {n} раз"
    if "UNTIL" in parts:
        try:
            u = datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=ZoneInfo("UTC")).astimezone(_MSK)
        except ValueError:
            return None
        return f"{base} до {u.day} {_MONTHS_RU[u.month - 1]} {u.hour}:{u.minute:02d}"
    return base


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
        rec = _recurrence_ru(str(kwargs.get("recurrence_rule") or ""))
        if rec is None:  # повтор есть, но очеловечить не смогли → generic (не врать)
            return None
        return ConfirmFacts(action="поставить напоминание",
                            object_title=title,
                            when_local=when.astimezone(_MSK),
                            recurrence_human=rec)
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
                # R1 MAJOR-4: короткая форма «в {слово} {часть суток}» двусмысленна
                # ТОЛЬКО для «дня» («в три дня» = «через три дня») - её не допускаем;
                # «в девять утра» / «в семь вечера» - обычная речь, оставляем.
                if part != "дня":
                    out.append(f"в {word} {part}")
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
        # R1 оба Codex: повтор ОБЯЗАН быть в договоре - иначе юзер подтверждает
        # разовое, а получает серию
        rec = f", повтор {facts.recurrence_human}" if facts.recurrence_human else ""
        return (f"Ставлю напоминание «{facts.object_title}» {day} {t}{rec}. "
                f"Подтверждаешь?")
    return f"Сделать: {facts.action} «{facts.object_title}»?"


# Русские действия для write-инструментов под кандидат-confirm, у которых нет
# полного факт-рендера (нет времени/особой структуры). Объект - из типовых
# аргументов (title/name), дословно в кавычках. Реестр пополняется по мере
# появления инструментов в candidate-потоке (канарейка покажет).
_ACTIONS_RU = {
    "schedule_reminder": "поставить напоминание",  # R1 MINOR-8: фолбэк при кривых аргументах
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
    # R2 medium: неочеловечиваемый RRULE уходит generic-путём - но юзер ОБЯЗАН
    # знать, что подтверждает СЕРИЮ, не разовое (иначе «да» исполнит скрытый повтор)
    if action and str(kwargs.get("recurrence_rule") or "").strip():
        action = f"{action} (повторяющееся)"
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
        rec = (f" Повторение (вставь ДОСЛОВНО): {facts.recurrence_human}."
               if facts.recurrence_human else "")
        user = (f"Действие: {facts.action}. Название: «{facts.object_title}». "
                f"Допустимые формулировки дня: {days}. "
                f"Допустимые формулировки времени: {times}.{rec}")
    else:
        user = f"Действие: {facts.action}. Название: «{facts.object_title}»."
    return system, user


_TECH_RE = re.compile(
    r"[a-z_]{2,}|"                # латиница подряд от 2 (R2: «ID 18»; title исключён до скана)
    r"\d{4}-\d{2}-\d{2}|"         # ISO-дата
    r"trigger_iso|title=|=",      # сырые аргументы
    re.IGNORECASE)
_ANY_DATE_RE = re.compile(r"\b(\d{1,2})\s+(январ|феврал|март|апрел|ма[яе]|июн|июл|"
                          r"август|сентябр|октябр|ноябр|декабр)")
_ANY_TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
# R1 MAJOR-3: месяц из стема (позиция в _ANY_DATE_RE) - для сверки посторонней даты
_MONTH_STEM_NUM = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "мае": 5,
                   "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
                   "ноябр": 11, "декабр": 12}


def _phrase_in(phrase: str, text: str) -> bool:
    """Вхождение по ГРАНИЦАМ СЛОВА (R1 MAJOR-3): «завтра» не должна матчиться
    внутри «послезавтра» - иначе верификатор пропускает перевранный день."""
    return re.search(rf"(?<![а-яёa-z0-9]){re.escape(phrase)}(?![а-яёa-z0-9])",
                     text, re.IGNORECASE) is not None


# R1 medium MAJOR-4: маркеры ДЕЙСТВИЯ - «Отменяю «X»…» не должно проходить
# договором «поставить». Хотя бы один маркер соответствующего действия.
_ACTION_MARKERS = {
    # R2 medium: только однозначные ПОЛОЖИТЕЛЬНЫЕ глаголы (существительное
    # «напоминание» матчилось в «Не ставлю напоминание…»/«Отменяю напоминание…»)
    "поставить напоминание": ("ставлю", "поставлю", "напомню"),
}
# отрицание/отмена рядом с действием = не тот договор
_NEGATION_RE = re.compile(
    r"(?:^|[^а-яёa-z])не\s+(?:став|буд|поставл|напомн)|отмен", re.IGNORECASE)
# все словесные часовые формулировки (для отлова вранья «в четыре вечера» при 15:00)
_ANY_WORD_HOUR_RE = re.compile(
    r"\bв\s+(?:час|" + "|".join(w for h, w in _HOUR_WORDS.items() if h != 1) +
    r")(?:\s+час(?:а|ов))?\s+(?:утра|дня|вечера|ночи)\b")


def verify_confirm_text(text: str, facts: ConfirmFacts, *, now: datetime) -> bool:
    """Железная проверка фразы «рта» перед отправкой юзеру.
    Требования: «название» дословно в кавычках; маркер ДЕЙСТВИЯ; допустимые день и
    время (по границам слова); повтор упомянут, если он есть; вопрос подтверждения;
    НИКАКОЙ технической начинки; никаких посторонних дат/времён/чисел."""
    t = (text or "").strip()
    if not t or not t.rstrip().endswith("?"):
        return False
    # R1 high/medium MINOR: точное «название» в кавычках обязательно; его же
    # ИСКЛЮЧАЕМ из tech-скана и число-скана (легитимные «Zoom с командой», «3 таблетки»)
    quoted = f"«{facts.object_title}»"
    if quoted not in t:
        return False
    rest = t.replace(quoted, " ")
    if _TECH_RE.search(rest):
        return False
    lower_rest = rest.lower()
    # R1 medium MAJOR-4 + R2: действие положительным глаголом, отрицание/отмена - враньё
    markers = _ACTION_MARKERS.get(facts.action, ())
    if markers and not any(_phrase_in(mk, lower_rest) for mk in markers):
        return False
    if _NEGATION_RE.search(lower_rest):
        return False
    # R1 оба Codex: повтор в фактах → обязан быть в тексте (и наоборот - слово
    # «повтор»/«кажд» без факта повтора = враньё)
    if facts.recurrence_human:
        # R2 medium: ВСЯ фраза повтора дословно (раньше split(",")[0] пропускал
        # фразу без «всего 5 раз» - юзер подтверждал бесконечную серию как 5 раз)
        if facts.recurrence_human not in lower_rest:
            return False
        # R2 Claude MINOR: фразу повтора ВЫРЕЗАЕМ до сканов дат/времён - иначе
        # «до 20 августа 18:00» из UNTIL резался бы как «посторонняя дата» и
        # живой голос был бы мёртв для всего UNTIL-класса (инструктируем X - режем X)
        lower_rest = lower_rest.replace(facts.recurrence_human, " ")
    elif re.search(r"повтор|кажд", lower_rest):
        return False
    if facts.when_local is None:
        return True
    days = allowed_day_phrases(facts.when_local, now=now)
    times = allowed_time_phrases(facts.when_local)
    # R1 MAJOR-3: по границам слова («послезавтра» не матчит «завтра»)
    if not any(_phrase_in(d, lower_rest) for d in days):
        return False
    if not any(_phrase_in(x, lower_rest) for x in times):
        return False
    # относительные дни вне допустимых - враньё («завтра, то есть послезавтра»)
    for rel in ("сегодня", "завтра", "послезавтра"):
        if _phrase_in(rel, lower_rest) and rel not in days:
            return False
    # R2 medium: дни недели вне допустимых («в среду» при факте-вторнике)
    for wd in _WEEKDAYS_RU_LOC:
        if wd in lower_rest and wd not in days:
            return False
    # словесные часы вне допустимых («в четыре вечера» при факте 15:00) - R1 high
    for m in _ANY_WORD_HOUR_RE.finditer(lower_rest):
        if not any(_phrase_in(x, m.group(0)) or m.group(0) in x for x in times):
            return False
    # посторонние даты: «D <месяц>» вне допустимого дня И месяца (R1 MAJOR-3: месяц!)
    w = facts.when_local
    for m in _ANY_DATE_RE.finditer(lower_rest):
        if int(m.group(1)) != w.day or _MONTH_STEM_NUM.get(m.group(2)) != w.month:
            return False
    # посторонние времена HH:MM
    for m in _ANY_TIME_RE.finditer(lower_rest):
        if int(m.group(1)) != w.hour or int(m.group(2)) != w.minute:
            return False
    # любые прочие числа вне разрешённого набора (день/час/минуты/count) - враньё
    # класса «ID 42» (R1 high) или чужое число
    _allowed_nums = {str(w.day), str(w.hour), f"{w.minute:02d}", str(w.minute)}
    if facts.recurrence_human:
        _allowed_nums |= set(re.findall(r"\d+", facts.recurrence_human))
    for num in re.findall(r"\d+", lower_rest):
        if num not in _allowed_nums:
            return False
    return True
