"""ToolSpec instances for the UTILITY family (Sub-A4 phase 9).

1 tool migrated: log_unsupported_request.

Sources of truth:
- Tool signature: ``runtime/tools.py:229`` (cross-skill —
  available to all skills, not just housewife)
- Output schema + parser: ``services/tool_schemas/housewife.py``
  — LogUnsupportedRequestOk
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.housewife import LogUnsupportedRequestOutput


# ---------------------------------------------------------------------------
# Utility-specific aliases
# ---------------------------------------------------------------------------


UserAskedPhrase = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Short paraphrase of user's ask. Runtime truncates at 200
(runtime/tools.py:252). Schema enforces same cap so planner doesn't
get false impression that long strings will be stored verbatim."""


UnsupportedReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Concrete reason why the assistant can't fulfil the ask — which
skill/integration/tool is missing. «нет интеграции с Яндекс.Такси»
beats «not supported». Runtime truncates at 200."""


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


class LogUnsupportedRequestInput(BaseModel):
    """Log a user request the assistant CANNOT fulfil — feeds the
    product feature-request hotspot dashboard.

    Use when user asks for something the assistant lacks the skill
    / tool / integration / data to do («закажи такси», «напиши код
    для X», «позвони в банк», «купи билеты»). DO NOT call for
    things existing tools CAN do (schedule reminders, recall, save
    facts, web search). DO NOT call as a cop-out — only when truly
    impossible with current tool set."""

    model_config = ConfigDict(extra="forbid")
    user_asked: UserAskedPhrase
    reason: UnsupportedReason


# ---------------------------------------------------------------------------
# ToolSpec instance
# ---------------------------------------------------------------------------


LOG_UNSUPPORTED_REQUEST_SPEC = ToolSpec(
    name="log_unsupported_request",
    # #144: ужато — обязанности дублировались с mutex_notes
    description=(
        "Записать запрос юзера, который ассистент НЕ может выполнить "
        "(feedback создателям). ИСПОЛЬЗУЙ когда у Среды нет нужного "
        "skill/tool/data: «закажи такси», «напиши код», «позвони в банк». "
        "НЕ для того, что умеют существующие tools (напоминания/recall/"
        "save/web search), и НЕ как cop-out. После лога ответь юзеру "
        "кратко: чего не умею + workaround если есть."
    ),
    family="utility",
    effect="write",
    read_domains=[],
    write_domains=["utility"],
    input_model=LogUnsupportedRequestInput,
    output_model=LogUnsupportedRequestOutput,
    trigger_examples=[
        "закажи мне такси",
        "позвони в банк уточни баланс",
        "напиши код для бэкенда",
        "купи билеты в кино",
    ],
    mutex_notes=[
        "ТОЛЬКО для unsupported asks. Существующие tools (reminders / shopping / recall) — нет, runtime их умеет.",
        "После лога ОБЯЗАН ответить юзеру обычным текстом — tool сам ничего не показывает.",
    ],
    timeout_seconds=5,
    side_effect_class="transactional_write",
)


UTILITY_SPECS: list[ToolSpec] = [
    LOG_UNSUPPORTED_REQUEST_SPEC,
]


__all__ = [
    "LOG_UNSUPPORTED_REQUEST_SPEC",
    "LogUnsupportedRequestInput",
    "UTILITY_SPECS",
    "UnsupportedReason",
    "UserAskedPhrase",
]
