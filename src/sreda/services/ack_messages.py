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
  * Feminine grammatical forms to match the bot's self-presentation
    ("Среда" is female in the existing onboarding copy).
  * Delivered DIRECTLY via ``telegram.send_message`` — not through
    outbox. Outbox adds ~1s worker-poll latency which defeats the
    point. Failures in delivery are swallowed; the ack is UX sugar,
    not a correctness-critical signal.

Scope exclusions:
  * Callback queries (button taps) — already feel instant, an extra
    "приступаю" just adds noise.
  * New-user flow — they get a welcome screen, not an ack.
  * Empty / bodyless updates — nothing to acknowledge.
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)


# Keep the list at 15–20 entries. Shorter lists quickly feel repetitive;
# longer lists dilute the "familiar voice" effect.
_PHRASES: tuple[str, ...] = (
    "Приступаю",
    "Работаю",
    "Смотрю",
    "Секунду",
    "Секундочку",
    "Минутку",
    "Момент",
    "Думаю",
    "Поняла",
    "Ясно",
    "Принято",
    "Взяла в работу",
    "Обрабатываю",
    "Уже разбираюсь",
    "Сейчас",
    "Окей",
    "Посмотрю",
    "Пробую",
)


def pick_ack(rng: random.Random | None = None) -> str:
    """Return one of the canned acknowledgements.

    ``rng`` is exposed so tests can seed a deterministic selection
    without monkey-patching the ``random`` module globally.
    """
    r = rng or random
    return r.choice(_PHRASES)


def all_phrases() -> tuple[str, ...]:
    """Public accessor for tests / admin diagnostics."""
    return _PHRASES
