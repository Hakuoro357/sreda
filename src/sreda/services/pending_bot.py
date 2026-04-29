"""Pending-bot — чейнинг приветственных сообщений для юзеров в pending-approve.

Юзер делает /start → `Tenant.approved_at IS NULL` → раньше webhook
silent-drop'ил. Сейчас pending-бот отвечает цепочкой из 11 сообщений:

  1. intro      → представление + список доменов + «тапай кнопки»
  2. voice      → блок про голос (основной режим)
  3. schedule   → расписание и задачи
  4. reminders  → напоминания + кнопки Сделал/Отложить
  5. checklists → дела (чек-листы)
  6. shopping   → список покупок
  7. recipes    → книга рецептов (КБЖУ, без тегов в UI)
  8. family     → семья без обещаний имён (152-ФЗ Часть 2 ready)
  9. memory     → произвольные факты «запомни, что Х»
  10. dont_do   → чего я не делаю
  11. done      → бета-тест closing (без кнопок)

Каждое сообщение, кроме closing, содержит ОДНУ кнопку «следующая
тема →». Юзер свободен пропустить — следующий /start или текст
вернёт его в intro. Закрытая ветка (`done`) без кнопок — финал.

История изменений:
* 2026-04-25: первая версия с 7 ветками демо (welcome + 6 веток).
* 2026-04-27 утро: упрощено до одного длинного welcome без кнопок.
* 2026-04-27 вечер: разбито обратно на 10 коротких сообщений с
  цепочкой кнопок — длинная портянка плохо читалась в Telegram.

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
        "Привет! Я Среда — персональный ассистент в Telegram. "
        "Работаю голосом и текстом, как удобно.\n\n"
        "Помогаю с тем, что обычно держишь в голове или раскидано "
        "по нескольким приложениям и заметкам: расписание, "
        "напоминания, дела по дому, список покупок, рецепты, семья.\n\n"
        "Расскажу подробнее по каждому пункту — тапай кнопки."
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
    buttons=(("📅 Расписание →", "schedule"),),
)

_SCHEDULE = PendingReply(
    text=(
        "📅 Расписание и задачи\n\n"
        "Запишу любую задачу на конкретный день и время — «во "
        "вторник к 16 Машу к стоматологу», «в субботу вечером "
        "позвонить маме». Можно повторяющиеся — «каждое утро в "
        "8:30 кормить кота», «каждый понедельник в 17 кружок Пети». "
        "Всё видно в приложении внутри Telegram, с разбивкой по "
        "дням. К любой задаче можно прицепить напоминание — за "
        "сколько минут предупредить."
    ),
    buttons=(("🔔 Напоминания →", "reminders"),),
)

_REMINDERS = PendingReply(
    text=(
        "🔔 Напоминания\n\n"
        "На каждом напоминании — кнопки «Сделал ✅» и «Отложить ⏰». "
        "Тапнул «Сделал» — закрыл. Тапнул «Отложить» — пришлю снова "
        "через 10 минут. Если не реагируешь — повторно дёргать не "
        "буду, чтобы не быть навязчивой."
    ),
    buttons=(("📝 Дела →", "checklists"),),
)

_CHECKLISTS = PendingReply(
    text=(
        "📝 Дела (чек-листы)\n\n"
        "Это списки без дат — там, где «расписание» не подходит. "
        "Например «Сборы на дачу», «Что купить в новую квартиру», "
        "«Уборка перед гостями». Каждый пункт — галочка. Можно "
        "добавлять и удалять пункты на ходу. Всё видно в приложении."
    ),
    buttons=(("🛒 Покупки →", "shopping"),),
)

_SHOPPING = PendingReply(
    text=(
        "🛒 Список покупок\n\n"
        "Скажи голосом «добавь в список молоко, хлеб, килограмм "
        "яблок и пачку макарон» — разберу по категориям (молочка, "
        "хлеб, фрукты, бакалея) и добавлю в общий список. Купил — "
        "отметил, оно уйдёт. Весь список можно очистить одной кнопкой."
    ),
    buttons=(("📖 Рецепты →", "recipes"),),
)

_RECIPES = PendingReply(
    text=(
        "📖 Рецепты\n\n"
        "Сохраняю в книгу рецептов — со временем приготовления и "
        "КБЖУ. Скажи «у меня есть полкурицы, 3 картошки, морковка "
        "и лук» — найду подходящий из сохранённых рецептов или "
        "предложу ещё несколько вариантов блюд на выбор."
    ),
    buttons=(("👨‍👩‍👧 Семья →", "family"),),
)

_FAMILY = PendingReply(
    text=(
        "👨‍👩‍👧 Семья\n\n"
        "Запоминаю про близких — кто в семье есть, возраст, "
        "кружки, любимые и нелюбимые блюда. Это нужно, чтобы "
        "напоминания понимали, о ком речь, а меню и покупки "
        "учитывали, кто что ест. Сама ничего не выпытываю. Любой "
        "факт можно убрать словом «забудь про X»."
    ),
    buttons=(("🧠 Память →", "memory"),),
)

_MEMORY = PendingReply(
    text=(
        "🧠 Память\n\n"
        "Запоминаю произвольные факты, которые сам(-а) скажешь. "
        "Например: «запомни, что я не люблю баранину» — в меню "
        "больше не предложу. «У бабушки день рождения 12 ноября» — "
        "за неделю до даты напомню. «Машина на сервисе до "
        "пятницы» — если потом спросишь «что у меня по делам», не "
        "забуду упомянуть.\n\n"
        "Память выручает, когда нужно отложить мысль на потом, не "
        "записывая её в задачу или напоминание. Любой факт можно "
        "убрать словом «забудь про X»."
    ),
    buttons=(("🚫 Чего не делаю →", "dont_do"),),
)

_DONT_DO = PendingReply(
    text=(
        "🚫 Чего я не делаю\n\n"
        "🤐 Не пишу первой просто так\n"
        "Только если сам(-а) просил поставить напоминание или "
        "когда-то упоминал регулярное событие.\n\n"
        "📍 Не отслеживаю местоположение\n\n"
        "📅 Не лезу в твой календарь\n\n"
        "🛒 Не покупаю продукты за тебя\n"
        "Собираю только список.\n\n"
        "📷 Не работаю с фото и документами\n"
        "Только текст и голос.\n\n"
        "🩺 Не записываю медицинские данные\n"
        "Диагнозы, аллергии, лекарства — даже если упомянешь, "
        "такие слова маскирую и в открытом виде их не сохраняю."
    ),
    buttons=(("Готово ✓", "done"),),
)

_DONE = PendingReply(
    text=(
        "На этом всё, что хотела рассказать про себя.\n\n"
        "Сейчас Среда на закрытом бета-тестировании. Как только "
        "модератор одобрит твой доступ — я сама выйду на контакт "
        "первой. Делать ничего не нужно — просто жди сообщения. "
        "Обычно занимает пару часов."
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
    "schedule": _SCHEDULE,
    "reminders": _REMINDERS,
    "checklists": _CHECKLISTS,
    "shopping": _SHOPPING,
    "recipes": _RECIPES,
    "family": _FAMILY,
    "memory": _MEMORY,
    "dont_do": _DONT_DO,
    "done": _DONE,
    # Aliases для backwards-compat: устаревшие callback_data из прошлой
    # версии не должны падать в fallback. Если юзер на старой клавиатуре
    # тапнет старую кнопку — сразу попадает в intro (новый главный экран).
    "welcome": _INTRO,
    "what": _INTRO,
    "demo_morning": _INTRO,
    "menu_example": _INTRO,
    "life": _INTRO,
}


# Linear order of tour branches (used for idempotency check —
# tap on an "older" branch button after user already advanced is a
# no-op). Aliases (welcome / what / demo_morning / menu_example /
# life) НЕ В ORDER — они map'ятся в intro сразу.
BRANCH_ORDER: tuple[str, ...] = (
    "intro", "voice", "schedule", "reminders", "checklists",
    "shopping", "recipes", "family", "memory", "dont_do", "done",
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
    "intro":      "Привет",
    "voice":      "🎙️ Голос",
    "schedule":   "📅 Расписание",
    "reminders":  "🔔 Напоминания",
    "checklists": "📝 Дела",
    "shopping":   "🛒 Покупки",
    "recipes":    "📖 Рецепты",
    "family":     "👨‍👩‍👧 Семья",
    "memory":     "🧠 Память",
    "dont_do":    "🚫 Чего не делаю",
    "done":       "Готово",
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
    * `done` (финал) → пустой ``inline_keyboard`` — Telegram
      убирает клавиатуру при edit'е с пустым массивом.

    Возвращает всегда dict (не None как legacy ``build_inline_keyboard``)
    чтобы edit-flow всегда явно прописывал состояние клавиатуры —
    иначе Telegram сохранит старую при edit'е (не удаляя).
    """
    if current_branch == "done":
        # Final: явно убираем клавиатуру через empty inline_keyboard
        return {"inline_keyboard": []}

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
