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
    female in onboarding copy). Forms describe the bot's action ("я
    взяла", "я поняла"), not the user.
  * Delivered DIRECTLY via ``telegram.send_message`` — not through
    outbox. Outbox adds ~1s worker-poll latency which defeats the
    point. Failures in delivery are swallowed; the ack is UX sugar,
    not a correctness-critical signal.

Scope exclusions:
  * Callback queries (button taps) — already feel instant, an extra
    "приступаю" just adds noise.
  * New-user flow — they get a welcome screen, not an ack.
  * Empty / bodyless updates — nothing to acknowledge.

История:
  * 2026-04-27 (утро): ввели параметр `address_form` для split на
    пулы NEUTRAL/TY/VY (в составе функции «вы»-онбординга).
  * 2026-04-27 (вечер): откачено до простого pool — фича «вы»
    форма перенесена в backlog (`docs/tomorrow-plan.md` пункт 8).
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)


# 2026-04-29: progress-style фразы с emoji-prefix вместо нейтральных
# «Принято/Поняла/Окей». Каждая фраза однозначно говорит «работа в
# процессе», emoji даёт визуальный сигнал «что-то делается». Список
# короткий (6) — для типичного daily-use одного юзера эти фразы и
# так будут чередоваться достаточно. Перед ack-message Telegram уже
# показывает «печатает...» индикатор (sendChatAction), который сам
# гасится при появлении этого текстового сообщения.
_PHRASES: tuple[str, ...] = (
    "⏳ Работаю…",
    "⌛ Думаю…",
    "🛠️ Минутку…",
    "🔍 Смотрю…",
    "⚙️ Обрабатываю…",
    "🌀 Секунду…",
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
