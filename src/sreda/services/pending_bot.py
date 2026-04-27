"""Pending-bot — scripted-engine для юзеров в статусе «ожидает одобрения».

Часть B плана v2. Юзер делает /start → `Tenant.approved_at IS NULL` →
раньше webhook silent-drop'ил. Теперь вместо silent-drop'а вызывается
``PendingBot.match(input)``, которая возвращает сценарный ответ из
заранее прошитых веток: welcome + 6 тем + fallback.

Весь модуль — синхронный, без LLM, без БД. Задача — держать юзера
в диалоге пока админ не одобрит (10-30 мин по плану), показывая что
умеет Среда, и собирая якорь доверия до первого реального turn'а.

Кнопки: используем callback_data ``pb:<branch_key>``. Webhook-хэндлер
для pending'а маршрутизирует callback_query по такому префиксу обратно
в ``PendingBot.match``.

Фильтры анти-сталкер из плана применены ко всем текстам:
- «зачем» явно рядом с каждым фактом, что запоминаем (ветка 4)
- read-back без счёта
- нет спонтанных чек-инов / «как прошёл день»
- любая информация необязательна — бот работает и без неё
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PendingReply:
    """Ответ pending-бота. ``buttons`` — массив (label, branch_key).
    ``branch_key`` → callback_data будет ``pb:<branch_key>``."""

    text: str
    buttons: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Welcome
# ---------------------------------------------------------------------------

_WELCOME_TEXT = (
    "Привет! Я Среда — персональный ассистент.\n\n"
    "Пока модератор подключает тебя (обычно 10–30 минут) — "
    "покажу чем могу быть полезна. Тапни любую кнопку или напиши вопрос."
)

_WELCOME_BUTTONS: tuple[tuple[str, str], ...] = (
    ("🔔 Что умеешь?", "what"),
    ("🍽️ Покажи пример меню", "menu_example"),
    ("👨\u200d👩\u200d👧 Как запоминаешь семью?", "memory"),
    ("⏱️ Как это выглядит в жизни", "life"),
)


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

_BRANCH_WHAT = PendingReply(
    text=(
        "Я делаю три вещи:\n\n"
        "🔔 Напоминаю — кружки, расписания, лекарства, дни рождения.\n"
        "🍽️ Составляю меню под диеты каждого в семье.\n"
        "🛒 Собираю список покупок из меню.\n\n"
        "Работаю в Telegram голосом или текстом. "
        "Вот пример реального дня:"
    ),
    buttons=(
        ("🎬 Пример диалога", "demo_morning"),
        ("📅 Как с расписанием?", "schedule"),
        ("📋 Покажи меню", "menu_example"),
        ("↩️ К началу", "welcome"),
    ),
)

_BRANCH_DEMO_MORNING = PendingReply(
    text=(
        "Вот как выглядит день:\n\n"
        "21:00 🔔 Среда:\n"
        "       Завтра у Маши кружок к 9:00.\n"
        "       Форма и сменка готовы?\n"
        "       [Готово ✅] [Напомни утром ⏰]\n\n"
        "21:03 👤 Ты: тап «Напомни утром»\n\n"
        "08:15 🔔 Среда:\n"
        "       Через 45 минут выход — форма у кровати, сменка в рюкзаке?\n"
        "       [Всё ок ✅] [Напомни через 20]\n\n"
        "08:32 👤 Ты: ✅\n\n"
        "Всё запомнила с двумя тапами."
    ),
    buttons=(
        ("🍽️ А как с меню?", "menu_example"),
        ("👨\u200d👩\u200d👧 Про семью подробнее", "memory"),
        ("↩️ К началу", "welcome"),
    ),
)

_BRANCH_MENU_EXAMPLE = PendingReply(
    text=(
        "Я помню диеты каждого. Вот как:\n\n"
        "👤 Ты: У Пети безлактозная диета.\n\n"
        "🍽️ Среда: Без молочки — собрать меню на неделю?\n"
        "          [Да, собери] [Не сейчас]\n\n"
        "👤 Ты: [Да, собери]\n\n"
        "🍽️ Среда: Готово:\n"
        "          пн — омлет, суп с фрикадельками, плов\n"
        "          вт — каша на воде, …\n"
        "          [Список покупок] [Заменить блюдо]\n\n"
        "Меню видно в приложении внутри Telegram. "
        "Продукты уже сгруппированы по категориям."
    ),
    buttons=(
        ("🛒 Как с покупками?", "life"),
        ("🔔 Про напоминания", "demo_morning"),
        ("↩️ К началу", "welcome"),
    ),
)

_BRANCH_MEMORY = PendingReply(
    text=(
        "Что запоминаю и зачем:\n\n"
        "• имена и возрасты\n"
        "  → чтобы обращаться правильно в напоминаниях\n"
        "• диеты и аллергии\n"
        "  → чтобы меню подходило всем в семье\n"
        "• лекарства — если нужны ежедневные напоминания\n"
        "  → чтобы не пропустить приём\n"
        "• кружки и регулярные занятия\n"
        "  → чтобы за день предупредить «завтра в 9 кружок»\n"
        "• любимые/нелюбимые блюда (опционально)\n"
        "  → чтобы меню не повторялось и все ели\n\n"
        "Про дни рождения, адреса, телефоны родни — я сама "
        "не спрашиваю. Если тебе удобно — расскажешь когда "
        "понадобится.\n\n"
        "Любой факт можно убрать словом «забудь про X»."
    ),
    buttons=(
        ("🎬 Ещё пример", "demo_morning"),
        ("📅 Про расписание", "schedule"),
        ("↩️ К началу", "welcome"),
    ),
)

_BRANCH_SCHEDULE = PendingReply(
    text=(
        "Я помню задачи, которые ты ставишь. Пример:\n\n"
        "Понедельник:\n"
        "👤 Ты: Надо Машу записать к стоматологу.\n\n"
        "Четверг утром:\n"
        "🧠 Среда: В понедельник был разговор про стоматолога для Маши. "
        "Поставить напоминание на эту неделю?\n"
        "          [Да, на пятницу] [Напишу позже] [Уже готово]"
    ),
    buttons=(
        ("🎬 Ещё пример", "demo_morning"),
        ("📋 Про меню", "menu_example"),
        ("↩️ К началу", "welcome"),
    ),
)

_BRANCH_LIFE = PendingReply(
    text=(
        "Типичная неделя со Средой:\n\n"
        "пн: напомнила про кружок Маши, помогла со списком к ужину\n"
        "вт: обсудили меню на неделю, собрала список продуктов\n"
        "ср: напомнила про лекарство Пети — по графику, который ты задал(-а)\n"
        "чт: записала заметку «Машу к стоматологу» — напомню ближе к выходным\n"
        "пт: напомнила про стоматолога, помогла с меню на ужин\n"
        "сб, вс: тихо — выходные не трогаю\n\n"
        "В среднем — 3-4 сообщения от меня в день, все по делу "
        "и с кнопками «Готово / Отложить».\n\n"
        "Я не пишу без повода — только о том, что ты просишь напомнить."
    ),
    buttons=(
        ("🔔 Что умеешь?", "what"),
        ("🍽️ Про меню", "menu_example"),
        ("↩️ К началу", "welcome"),
    ),
)

_BRANCH_FALLBACK_TEXT = (
    "Интересный вопрос! Вернусь к нему как только модератор "
    "подключит тебя (обычно 10–30 минут). Пока можешь посмотреть:"
)
_BRANCH_FALLBACK_BUTTONS = _WELCOME_BUTTONS


_BRANCHES: dict[str, PendingReply] = {
    "welcome": PendingReply(text=_WELCOME_TEXT, buttons=_WELCOME_BUTTONS),
    "what": _BRANCH_WHAT,
    "demo_morning": _BRANCH_DEMO_MORNING,
    "menu_example": _BRANCH_MENU_EXAMPLE,
    "memory": _BRANCH_MEMORY,
    "schedule": _BRANCH_SCHEDULE,
    "life": _BRANCH_LIFE,
}


# ---------------------------------------------------------------------------
# Lightweight keyword matcher for free-form text
# ---------------------------------------------------------------------------

# Когда юзер пишет text-сообщение (не нажимает кнопку), пытаемся мягко
# угадать намерение по ключевым словам. При промахе → fallback.
_TEXT_HEURISTICS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("что умеешь", "умеешь", "возможност", "функц"), "what"),
    (("меню", "рецепт", "блюд", "готов"), "menu_example"),
    (("покупк", "список", "магазин", "корзин"), "life"),
    (("семь", "ребён", "запомин", "память"), "memory"),
    (("напомн", "расписан", "кружок", "событ"), "schedule"),
    (("пример", "демо", "покажи"), "demo_morning"),
)


def _match_text_heuristic(text: str) -> str | None:
    norm = text.lower().strip()
    if not norm:
        return None
    for keywords, branch in _TEXT_HEURISTICS:
        for kw in keywords:
            if kw in norm:
                return branch
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_CB_PREFIX = "pb:"


def is_pending_callback(callback_data: str) -> bool:
    """True если callback_data относится к pending-боту."""
    return callback_data.startswith(_CB_PREFIX)


def match(input_text: str | None, *, is_callback: bool = False) -> PendingReply:
    """Главная функция: принимает либо callback_data (``pb:<branch>``),
    либо свободный текст от юзера. Возвращает сценарный ответ.

    ``is_callback=True`` — приходит из callback_query handler'а;
    парсим branch-key из префикса. Иначе — пытаемся угадать ветку
    по ключевым словам, fallback → welcome-промпт с кнопками.
    """
    if not input_text:
        return _BRANCHES["welcome"]
    raw = input_text.strip()
    if not raw:
        return _BRANCHES["welcome"]

    # callback path
    if is_callback or raw.startswith(_CB_PREFIX):
        branch_key = raw[len(_CB_PREFIX):] if raw.startswith(_CB_PREFIX) else raw
        return _BRANCHES.get(branch_key, _BRANCHES["welcome"])

    # /start and plain welcome triggers
    low = raw.lower()
    if low in {"/start", "start", "welcome", "привет", "здравствуй"}:
        return _BRANCHES["welcome"]

    # Heuristic text match
    branch = _match_text_heuristic(raw)
    if branch:
        return _BRANCHES[branch]

    # Nothing matched → polite fallback with re-offer of main menu.
    return PendingReply(
        text=_BRANCH_FALLBACK_TEXT,
        buttons=_BRANCH_FALLBACK_BUTTONS,
    )


def build_inline_keyboard(reply: PendingReply) -> dict | None:
    """Telegram inline_keyboard из ``PendingReply.buttons``. Одна
    кнопка = одна строка (вертикальное расположение, лучше читается)."""
    if not reply.buttons:
        return None
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"{_CB_PREFIX}{branch}"}]
            for label, branch in reply.buttons
        ],
    }
