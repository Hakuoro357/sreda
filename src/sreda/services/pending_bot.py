"""Pending-bot — чейнинг приветственных сообщений для юзеров в pending-approve.

Юзер делает /start → `Tenant.approved_at IS NULL` → раньше webhook
silent-drop'ил. Сейчас pending-бот отвечает цепочкой из 4 сообщений:

  1. intro   → представление + краткий список доменов + «тапай кнопки»
  2. voice   → блок про голос (основной режим)
  3. memory  → запоминаю важное (расписание, дела, покупки, рецепты,
                семью + произвольные факты)
  4. done    → бета-тест closing (без кнопок)

Каждое сообщение, кроме closing, содержит ОДНУ кнопку «следующая
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

Source-of-truth по тексту: ``docs/copy/welcome.md`` секция 1.
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
# Branch texts (utterly user-facing — координируем по docs/copy/welcome.md)
# ---------------------------------------------------------------------------

_INTRO = PendingReply(
    text=(
        "Работаю голосом и текстом, как удобно.\n\n"
        "Помогаю с тем, что обычно держишь(ишь) в голове или раскидано "
        "по нескольким приложениям и заметкам: расписание, "
        "напоминания, дела по дому, список покупок, рецепты, семья.\n\n"
        "Расскажу подробнее по каждому пункту — тапни кнопку."
    ),
    buttons=(("🎙️ Голос →", "voice"),),
)

_VOICE = PendingReply(
    text=(
        "🎙️ Голос\n\n"
        "Голос — самый удобный режим работы. Можно просто говорить "
        "как с человеком, без команд и формальностей: «добавь в "
        "покупки молоко, хлеб и пачку гречки», «напомни завтра в "
        "девять отвезти Машу в музыкалку», «у меня в холодильнике "
        "полкурицы, картошка и луковица — что приготовить». "
        "Расшифрую русский, всё пойму.\n\n"
        "Голосовые принимаю до 30 секунд за раз. Этого хватает на "
        "3-4 фразы — обычно больше и не нужно. Можно надиктовать "
        "сразу несколько дел одним сообщением — разберу.\n\n"
        "Текстом работаю так же — выбирай, что удобнее в моменте."
    ),
    buttons=(("🧠 Память →", "memory"),),
)

_MEMORY = PendingReply(
    text=(
        "🧠 Память\n\n"
        "Главная фича: я запоминаю! Запоминаю всё что важно, чтобы "
        "ты не держал(а) в голове.\n\n"
        "📅 Расписание и напоминания — «во вторник к 16 Машу к "
        "стоматологу», «каждое утро в 8:30 кормить кота». "
        "На напоминаниях кнопки «Сделал(а) ✅» / «Отложить ⏰».\n\n"
        "📝 Дела и покупки — чек-листы без дат («сборы на дачу») и "
        "общий список покупок с авто-категоризацией.\n\n"
        "📖 Рецепты — сохраню с КБЖУ. «У меня полкурицы, картошка, "
        "лук — что приготовить» — подберу из книги.\n\n"
        "👨‍👩‍👧 Семья — кто в семье, возраст, кружки, что любят и "
        "не любят. Меню и покупки учитывают.\n\n"
        "Плюс произвольные факты: «запомни, что я не люблю "
        "баранину», «у бабушки ДР 12 ноября», «машина на сервисе "
        "до пятницы». Любой факт убирается словом «забудь про X».\n\n"
        "Сама ничего не выпытываю — только то, что говоришь сам(а). "
        "Не пишу первой просто так, не отслеживаю местоположение, "
        "не лезу в календарь, не покупаю продукты за тебя (только "
        "собираю список), не работаю с фото/документами, "
        "медицинские данные маскирую."
    ),
    buttons=(("Готово ✓", "done"),),
)

_DONE = PendingReply(
    text=(
        "На этом всё.\n\n"
        "Пиши голосом или текстом, попробуй любую из тем выше. "
        "Если что-то непонятно — спрашивай прямо в чате.\n\n"
        "Прежде чем приступим, подскажи, как к тебе обращаться? "
        "Имя или ник, как удобно."
    ),
    buttons=(),
)


# 2026-04-28: Closing для broadcast-рассылки existing approved юзерам.
# Оригинальный `_DONE` обещает «модератор одобрит твой доступ — я
# напишу» — это для новых /start юзеров в pending-фазе. Приближает
# доступ. Существующим юзерам, у которых approve уже есть, такая
# фраза вводит в замешательство («модератор? я же давно работаю»).
# Используется в `telegram_bot._handle_callback` для approved юзеров —
# tour может пройти только approved (pre-approve `pb:done` обрабатывается
# в `telegram_webhook.py` и шлёт оригинальный `_DONE`).
_DONE_BROADCAST = PendingReply(
    text=(
        "На этом всё, что хотела рассказать про себя.\n\n"
        "Если что-то нужно — пиши или говори голосом, как удобно. Я тут."
    ),
    buttons=(),
)


_BRANCHES: dict[str, PendingReply] = {
    "intro": _INTRO,
    "voice": _VOICE,
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
    "intro", "voice", "memory", "done",
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
# видел куда ведёт каждая кнопка («← 🎙️ Голос», «📅 Расписание →»).
_BRANCH_LABELS: dict[str, str] = {
    "intro":  "Привет",
    "voice":  "🎙️ Голос",
    "memory": "🧠 Память",
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
    * `dont_do` (предпоследняя) → «← 🧠 Память» + «Готово ✓»
    * `done` (финал) → одна кнопка «← 🚫 Чего не делаю» —
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
