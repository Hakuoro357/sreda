"""ToolSpec instances for the UI family (Sub-A4 phase 10).

1 tool migrated: reply_with_buttons (Telegram inline keyboard).

Sources of truth:
- Tool signature: ``services/housewife_chat_tools.py:2349``
- Output schema + parser: ``services/tool_schemas/housewife.py``
  — ReplyWithButtonsOk
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.housewife import ReplyWithButtonsOutput


# ---------------------------------------------------------------------------
# UI-specific aliases
# ---------------------------------------------------------------------------


ReplyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
"""Telegram message body cap (4096 chars max per Bot API).
Runtime housewife_chat_tools.py:2385 does ``(text or "").strip()``
without further truncation; schema enforces upstream."""


ButtonLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20),
]
"""Short button-label string. Runtime caps via «короткие (≤20 символов)»
guidance in docstring; we enforce strictly so Telegram inline button
doesn't get truncated mid-word."""


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


class ReplyWithButtonsInput(BaseModel):
    """Send a Telegram reply with 2-4 inline buttons.

    Use ONLY when your reply contains a question to the user.
    Buttons are short user-replies they could've typed themselves —
    NO «Да/Нет» (too vague), always concrete («Да, собери меню»,
    «Не сейчас», «Покажи список»). Telegram provides standard
    back-button automatically — don't add empty «Отмена»/«Назад».

    Runtime constraints:
    - At least 2 buttons (min_length=2 below)
    - At most 4 (max_length=4); runtime clamps anyway
    - Each label ≤20 chars (ButtonLabel alias)"""

    model_config = ConfigDict(extra="forbid")
    text: ReplyText
    buttons: list[ButtonLabel] = Field(min_length=2, max_length=4)


# ---------------------------------------------------------------------------
# ToolSpec instance
# ---------------------------------------------------------------------------


REPLY_WITH_BUTTONS_SPEC = ToolSpec(
    name="reply_with_buttons",
    description=(
        "Отправить ответ юзеру с 2-4 inline-кнопками-вариантами. "
        "ИСПОЛЬЗУЙ ОБЯЗАТЕЛЬНО когда твой ответ содержит ВОПРОС к "
        "юзеру. Кнопки — короткие (≤20 симв) реплики, которые юзер "
        "сам мог бы написать. БЕЗ «Да/Нет» (vague) — всегда "
        "конкретика: «Да, собери меню», «Не сейчас», «Покажи "
        "список». БЕЗ пустых «Отмена»/«Назад» (Telegram сам даёт "
        "back-button). Если в ответе вопроса НЕТ — НЕ вызывай "
        "этот tool, отвечай обычным текстом. Возвращает "
        "ok:buttons:N где N — число принятых кнопок (2-4)."
    ),
    family="ui",
    effect="write",
    read_domains=[],
    write_domains=["ui"],
    input_model=ReplyWithButtonsInput,
    output_model=ReplyWithButtonsOutput,
    trigger_examples=[
        "Помню у Пети безлактозная. Собрать меню?",
        "Кто идёт к врачу?",
        "Когда тебе напомнить — утром или вечером?",
        "На сколько дней планируем меню?",
    ],
    mutex_notes=[
        "ТОЛЬКО когда в ответе есть вопрос с конкретными вариантами. Без вопроса — обычный текст ответа.",
        "buttons label ≤20 симв, БЕЗ Да/Нет, БЕЗ Отмена/Назад. 2-4 штуки.",
    ],
    timeout_seconds=5,
    side_effect_class="transactional_write",
    # request_local marker: reply_with_buttons is REQUEST-LOCAL. It writes
    # to an in-memory state dict consumed by the post-loop renderer, NOT to
    # any user-data DB table. Without this flag the derived is_durable_write
    # predicate (effect='write' + side_effect_class='transactional_write')
    # would classify it as a durable write and the registry build would
    # demand a recovery adapter for a UI render call. (Sub-A12 Phase E,
    # accepted plan §PR-2a.1 — request_local bool, not a recovery_class enum.)
    request_local=True,
)


UI_SPECS: list[ToolSpec] = [
    REPLY_WITH_BUTTONS_SPEC,
]


__all__ = [
    "ButtonLabel",
    "REPLY_WITH_BUTTONS_SPEC",
    "ReplyText",
    "ReplyWithButtonsInput",
    "UI_SPECS",
]
