"""Pending-bot — чейнинг приветственных сообщений для юзеров в pending-approve.

Юзер делает /start → `Tenant.approved_at IS NULL` → раньше webhook
silent-drop'ил. Сейчас pending-бот отвечает цепочкой из 5 сообщений:

  1. intro   → представление + краткий список доменов + «тапай кнопки»
  2. voice   → блок про голос (основной режим)
  3. routine → бытовые сценарии
  4. memory  → запоминаю важное (расписание, дела, покупки, рецепты,
                семью + произвольные факты)
  5. done    → pending-closing до одобрения

Каждое сообщение, кроме финального, содержит ОДНУ кнопку «следующая
тема →». Юзер свободен пропустить — следующий /start или текст
вернёт его в intro. Закрытая ветка (`done`) без кнопок — финал.

История изменений:
* 2026-04-25: первая версия с 7 ветками демо (welcome + 6 веток).
* 2026-04-27 утро: упрощено до одного длинного welcome без кнопок.
* 2026-04-27 вечер: разбито обратно на 10 коротких сообщений с
  цепочкой кнопок — длинная портянка плохо читалась в Telegram.
* 2026-04-29: 11-step tour (добавлены schedule/reminders/dont_do).
* 2026-05-08: сокращено 11 → 4 (юзеры устают на 4-5 экране, средний
  drop-off ~50% к 6-му тапу). Убраны: schedule, reminders,
  checklists, shopping, recipes, family, dont_do — упомянуты в
  расширенном memory + intro. Старые ветки aliased на intro для
  in-progress tours без потери истории прогресса.

Документация по welcome-сценариям: ``docs/copy/welcome.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PendingReply:
    """Ответ pending-бота. ``buttons`` — массив (label, branch_key).
    ``branch_key`` → callback_data будет ``pb:<branch_key>``."""

    text: str
    buttons: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------
# Branch texts (utterly user-facing; docs/copy/welcome.md indexes flow)
# ---------------------------------------------------------------------------

_INTRO = PendingReply(
    text=(
        "Покажу на примерах, как я помогаю дома.\n\n"
        "Можно писать текстом или голосом: напоминания, покупки, "
        "меню, рецепты, чек-листы, семейные дела, погода, "
        "поиск в интернете.\n\n"
        "Дальше — несколько коротких примеров."
    ),
    buttons=(("🎙️ Голос →", "voice"),),
)

_VOICE = PendingReply(
    text=(
        "🎙️ Голос\n\n"
        "Со мной можно просто поговорить, без команд и специальных "
        "формулировок.\n\n"
        "Например:\n"
        "«Добавь молоко и яблоки в покупки»\n"
        "«Завтра в 9 напомни про музыкалку»\n"
        "«Составь меню на неделю»\n"
        "«Запомни, что Маша не ест грибы»\n\n"
        "Я расшифрую голос, пойму задачу и разложу по местам."
    ),
    buttons=(("🏠 Бытовая рутина →", "routine"),),
)

_ROUTINE = PendingReply(
    text=(
        "🏠 Бытовая рутина\n\n"
        "⏰ Напоминания и задачи — поставлю на дату и время, сделаю "
        "повторяющимися, помогу изменить или отменить.\n\n"
        "🛒 Покупки — добавлю продукты, покажу список, отмечу купленное, "
        "соберу продукты из меню.\n\n"
        "📋 Чек-листы — помогу собрать список дел: сборы на дачу, уборка, "
        "подготовка к празднику.\n\n"
        "🍽 Меню — составлю меню на неделю, обновлю отдельные дни, "
        "подберу блюда под семью.\n\n"
        "📖 Рецепты — сохраню рецепт, найду его потом, покажу ингредиенты "
        "и способ приготовления."
    ),
    buttons=(("🧠 Память и поиск →", "memory"),),
)

_MEMORY = PendingReply(
    text=(
        "🧠 Память и поиск\n\n"
        "👨‍👩‍👧 Семья — запоминаю состав семьи, привычки, диеты, "
        "аллергии, любимые и нелюбимые продукты.\n\n"
        "🧠 Факты и договоренности — могу помнить важное: дни рождения, "
        "расписания, предпочтения, бытовые правила.\n\n"
        "🌤 Погода — подскажу прогноз, температуру, осадки, что ждать "
        "утром, днем или вечером.\n\n"
        "🔍 Интернет — найду факты, новости, расписания, адреса и "
        "ближайшие места вроде аптек или магазинов.\n\n"
        "Можно начать с любой простой задачи."
    ),
    buttons=(("Готово ✓", "done"),),
)

_DONE = PendingReply(
    text=(
        "На этом всё, что хотела рассказать про себя.\n\n"
        "Сейчас Среда на закрытом бета-тестировании. Как только "
        "модератор откроет доступ — я сама выйду на контакт.\n\n"
        "Делать ничего не нужно — просто жди сообщения."
    ),
    buttons=(),
)


# 2026-05-22: approved persona-tour closing. После финального экрана
# следующий ответ пользователя сохраняется как display_name.
_DONE_BROADCAST = PendingReply(
    text=(
        "Готово.\n\n"
        "Теперь давай познакомимся. Как мне к тебе обращаться?"
    ),
    buttons=(),
)


_BRANCHES: dict[str, PendingReply] = {
    "intro": _INTRO,
    "voice": _VOICE,
    "routine": _ROUTINE,
    "memory": _MEMORY,
    "done": _DONE,
    # ⚠ Aliases для backwards-compat 2026-05-08 — 7 веток сокращены до
    # intro. Юзеры в середине старого 11-step tour'а тапающие старые
    # кнопки (если у них в чате остались сообщения с inline keyboard'ом
    # из прошлой версии) попадают в intro и могут пройти новый короткий
    # tour. Их progress (`last_branch=schedule` etc.) не теряется —
    # `record_pb_tour_progress` запишет последний branch как был.
    "schedule": _INTRO,
    "reminders": _INTRO,
    "checklists": _INTRO,
    "shopping": _INTRO,
    "recipes": _INTRO,
    "family": _INTRO,
    "dont_do": _INTRO,
    # Старые aliases с pre-edit-wizard эры — оставляем для совсем
    # старых клиентов которые могут тапнуть `pb:welcome` etc.
    "welcome": _INTRO,
    "what": _INTRO,
    "demo_morning": _INTRO,
    "menu_example": _INTRO,
    "life": _INTRO,
}


# Linear order of tour branches (used for idempotency check —
# tap on an "older" branch button after user already advanced is a
# no-op). Aliases (welcome/what/demo_morning/menu_example/life
# и устаревшие schedule/reminders/checklists/shopping/recipes/family/
# dont_do) НЕ В ORDER — они map'ятся в intro сразу.
BRANCH_ORDER: tuple[str, ...] = (
    "intro", "voice", "routine", "memory", "done",
)


def branch_index(branch: str) -> int:
    """Linear position of branch in tour. -1 если неизвестен."""
    try:
        return BRANCH_ORDER.index(branch)
    except ValueError:
        return -1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_CB_PREFIX = "pb:"


def is_pending_callback(callback_data: str) -> bool:
    """True если callback_data относится к pending-боту."""
    return callback_data.startswith(_CB_PREFIX)


def match(input_text: str | None, *, is_callback: bool = False) -> PendingReply:
    """Главная функция: принимает либо callback_data (``pb:<branch>``),
    либо свободный текст. Возвращает PendingReply.

    * Любой text/voice от юзера → ``intro``.
    * Callback с известным branch-key → соответствующий блок.
    * Callback с неизвестным branch-key → ``intro`` (fallback).
    """
    if not input_text:
        return _BRANCHES["intro"]
    raw = input_text.strip()
    if not raw:
        return _BRANCHES["intro"]

    if is_callback or raw.startswith(_CB_PREFIX):
        branch_key = (
            raw[len(_CB_PREFIX):] if raw.startswith(_CB_PREFIX) else raw
        )
        return _BRANCHES.get(branch_key, _BRANCHES["intro"])

    # Любой text-ввод от pending-юзера → intro (с кнопкой на голос).
    return _BRANCHES["intro"]


def build_inline_keyboard(reply: PendingReply) -> dict | None:
    """Legacy keyboard builder — одна кнопка = одна строка вертикально.

    Сохранён для backwards-compat (call site'ы переезжают на
    ``build_navigation_keyboard`` в edit-flow). Новые сообщения
    отправляйте через nav_keyboard, а не этот builder.
    """
    if not reply.buttons:
        return None
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"{_CB_PREFIX}{branch}"}]
            for label, branch in reply.buttons
        ],
    }


# 2026-04-29 (edit-based wizard rework): короткие лейблы для каждой
# ветки тура. Используются в `build_navigation_keyboard()` чтобы юзер
# видел куда ведёт каждая кнопка («← 🎙️ Голос», «🏠 Бытовая рутина →»).
_BRANCH_LABELS: dict[str, str] = {
    "intro":  "Привет",
    "voice":  "🎙️ Голос",
    "routine": "🏠 Бытовая рутина",
    "memory": "🧠 Память и поиск",
    "done":   "Готово",
}


def build_navigation_keyboard(current_branch: str) -> dict:
    """Inline-keyboard для wizard-style edit-навигации.

    2026-04-29: pending_bot tour теперь edit-based — одно сообщение
    в чате, текст и кнопки эволюционируют через editMessageText.
    Этот builder возвращает клавиатуру с prev/next переходами на
    основе позиции в ``BRANCH_ORDER``.

    Контракт:
    * `intro` (первая ветка) → одна кнопка: «🎙️ Голос →»
    * Промежуточные ветки → две кнопки в одном ряду:
      «← <prev_label>» + «<next_label> →»
    * `memory` (предпоследняя) → «← 🏠 Бытовая рутина» + «Готово ✓»
    * `done` (финал) → одна кнопка «← 🧠 Память и поиск» —
      без next, юзер может скроллить тур обратно.

    2026-04-29: tour остаётся permanent reference в чате после
    прохождения. Юзер может в любой момент вернуться к нему и
    переглянуть. Раньше на `done` клавиатура пропадала (`inline_keyboard=[]`),
    теперь там prev-кнопка для отката назад.

    Возвращает всегда dict чтобы edit-flow всегда явно прописывал
    состояние клавиатуры — иначе Telegram сохранит старую при edit'е.
    """
    try:
        idx = BRANCH_ORDER.index(current_branch)
    except ValueError:
        # Unknown branch — fallback на intro keyboard
        return build_navigation_keyboard("intro")

    row: list[dict] = []
    # Prev button (если не на первой ветке)
    if idx > 0:
        prev_branch = BRANCH_ORDER[idx - 1]
        prev_label = _BRANCH_LABELS.get(prev_branch, prev_branch)
        row.append({
            "text": f"← {prev_label}",
            "callback_data": f"{_CB_PREFIX}{prev_branch}",
        })
    # Next button (если не на последней ветке)
    if idx < len(BRANCH_ORDER) - 1:
        next_branch = BRANCH_ORDER[idx + 1]
        next_label = _BRANCH_LABELS.get(next_branch, next_branch)
        if next_branch == "done":
            # Особый кейс: финальная кнопка — «Готово ✓», без эмодзи лейбла
            next_text = "Готово ✓"
        else:
            next_text = f"{next_label} →"
        row.append({
            "text": next_text,
            "callback_data": f"{_CB_PREFIX}{next_branch}",
        })

    return {"inline_keyboard": [row]}
