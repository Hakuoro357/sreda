"""Quick acknowledgement messages sent the moment a user writes to the bot.

Rationale: user-perceived responsiveness. The real reply may take 5–15s
(voice transcription → LLM tool-loop → outbox → delivery). Without an
early ack the user sees a silent bot, wonders if the message even
arrived, and starts re-sending. A one-word "работаю" sent within ~200ms
of the webhook landing makes the conversation feel live.

Design:
  * Short — one word or two — so the follow-up real reply doesn't feel
    like "two bot messages talking past each other".
  * Varied — the bot feels less robotic if it doesn't say exactly
    "принято" every single time.
  * Feminine grammatical forms for the bot's self-narration ("Среда" is
    female in onboarding copy). The bot's gender stays feminine
    regardless of how it addresses the user — the address_form toggle
    affects only the form of addressing the user (ты/вы), not the bot's
    self-description.
  * Delivered DIRECTLY via ``telegram.send_message`` — not through
    outbox. Outbox adds ~1s worker-poll latency which defeats the
    point. Failures in delivery are swallowed; the ack is UX sugar,
    not a correctness-critical signal.

Scope exclusions:
  * Callback queries (button taps) — already feel instant, an extra
    "приступаю" just adds noise.
  * New-user flow — they get a welcome screen, not an ack.
  * Empty / bodyless updates — nothing to acknowledge.

Pools by address_form (2026-04-27):
  * NEUTRAL — безличные, не зависят от формы обращения. Подходят и
    для ты, и для вы, и пока форма ещё не выбрана юзером.
  * TY — допускают чуть более тёплый/разговорный тон, могут включать
    короткие фразы типа «Уже разбираюсь».
  * VY — более формально-вежливые: «Минуту, пожалуйста», «Сейчас
    посмотрю». Никогда не «ты сделала» — это про бота, не про юзера.
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)


# Безличные и описательно-нейтральные формы — работают всегда.
_PHRASES_NEUTRAL: tuple[str, ...] = (
    "Минутку",
    "Секунду",
    "Секундочку",
    "Момент",
    "Сейчас",
    "Думаю",
    "Смотрю",
    "Посмотрю",
    "Работаю",
    "Обрабатываю",
    "Принято",
    "Поняла",
    "Записала",
    "Приступаю",
)

# Чуть более разговорно — подходит для «ты»-режима.
_PHRASES_TY: tuple[str, ...] = (
    "Уже разбираюсь",
    "Взяла в работу",
    "Окей",
    "Ясно",
    "Пробую",
)

# Формально-вежливо — подходит для «вы»-режима.
_PHRASES_VY: tuple[str, ...] = (
    "Минуту, пожалуйста",
    "Сейчас посмотрю",
    "Уже изучаю",
    "Принято в работу",
    "Хорошо",
)


def pick_ack(
    rng: random.Random | None = None,
    *,
    address_form: str | None = None,
) -> str:
    """Return one of the canned acknowledgements.

    ``address_form`` — "ty" / "vy" / None. NULL = форма не выбрана,
    отдаём только нейтральный пул. Для совместимости со старыми
    тестами вызов без kwargs (`pick_ack()`) тоже даёт нейтральный пул.

    ``rng`` is exposed so tests can seed a deterministic selection
    without monkey-patching the ``random`` module globally.
    """
    r = rng or random
    pool: tuple[str, ...] = _PHRASES_NEUTRAL
    if address_form == "ty":
        pool = _PHRASES_NEUTRAL + _PHRASES_TY
    elif address_form == "vy":
        pool = _PHRASES_NEUTRAL + _PHRASES_VY
    return r.choice(pool)


def all_phrases() -> tuple[str, ...]:
    """Public accessor for tests / admin diagnostics. Returns the union
    of all three pools (NEUTRAL + TY + VY)."""
    return _PHRASES_NEUTRAL + _PHRASES_TY + _PHRASES_VY
