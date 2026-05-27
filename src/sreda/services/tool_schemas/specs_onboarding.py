"""ToolSpec instances for the ONBOARDING family (Sub-A4 phase 8).

3 tools migrated: onboarding_answered, onboarding_deferred,
onboarding_complete.

Sources of truth:
- Tool signatures: ``services/housewife_chat_tools.py:555-660``
- Output schemas: ``services/tool_schemas/housewife.py`` — 3 outputs
- 6 topics + 4 states + 4 statuses from
  ``services/housewife_onboarding.py:76-93``
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.housewife import (
    OnboardingAnsweredOutput,
    OnboardingCompleteOutput,
    OnboardingDeferredOutput,
    OnboardingTopic,
)


# ---------------------------------------------------------------------------
# Onboarding-specific aliases
# ---------------------------------------------------------------------------


OnboardingSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""User's 1-2 sentence answer preserving their wording. 500 char
cap is generous for free-form replies; runtime stores via
EncryptedString without further truncation."""


OnboardingDeferReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Short audit-purpose reason for skip («потом», «не сейчас»,
«не отвечу»). Not shown to user — only stored for analytics."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class OnboardingAnsweredInput(BaseModel):
    """Mark onboarding topic as answered + advance to next.

    Runtime ``HousewifeOnboardingService.mark_answered`` stores the
    summary as the user's saved answer, advances current_topic to
    the next pending topic, and flips onboarding_status to
    'in_progress' if it was 'not_started'. When all topics close
    (answered or skipped), status auto-flips to 'complete'.

    For ``addressing`` topic: summary MUST be ONLY a short
    name/nickname (1-3 words), WITHOUT prefixes like «Пользователя
    зовут» / «Меня зовут». Runtime has a backend sanitiser as
    last-line defense; schema doesn't enforce that pattern because
    valid addressing answers vary widely (Russian short forms with
    diminutives, English names, nicknames)."""

    model_config = ConfigDict(extra="forbid")
    topic: OnboardingTopic
    summary: OnboardingSummary


class OnboardingDeferredInput(BaseModel):
    """Mark onboarding topic as deferred + advance to next.

    First skip → topic_state=skipped_once (back in retry queue,
    will be re-asked). Second skip on the same topic →
    topic_state=skipped (permanently dropped). Reason is for
    audit only."""

    model_config = ConfigDict(extra="forbid")
    topic: OnboardingTopic
    reason: OnboardingDeferReason


class OnboardingCompleteInput(BaseModel):
    """Explicitly mark onboarding complete. No args — operates on
    the current user's onboarding state.

    Normally onboarding auto-completes when all topics close;
    explicit call is for «всё, хватит, мне надоело» — early
    drop-out. Runtime sets status='complete' regardless of prior
    state."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


ONBOARDING_ANSWERED_SPEC = ToolSpec(
    name="onboarding_answered",
    description=(
        "Отметить тему онбординга как отвеченную и перейти к следующей. "
        "Вызывай когда юзер дал осмысленный ответ на текущую тему "
        "(см. [ОНБОРДИНГ] block в системном промпте для current_topic). "
        "Темы: addressing / self_intro / family / diet / routine / "
        "pain_point. ``summary`` — 1-2 предложения в собственных "
        "словах юзера. Для темы addressing — ТОЛЬКО короткое имя/ник "
        "(1-3 слова, БЕЗ «Пользователя зовут», «Меня зовут»). "
        "Возвращает ok:answered:topic:next=...:status=... — next "
        "может быть «none» (все темы закрыты), status флипается на "
        "in_progress / complete автоматически."
    ),
    family="onboarding",
    effect="write",
    read_domains=["onboarding"],
    write_domains=["onboarding"],
    input_model=OnboardingAnsweredInput,
    output_model=OnboardingAnsweredOutput,
    trigger_examples=[
        "Меня зовут Борис",
        "У меня жена и двое детей",
        "Я веган, мяса не ем",
        "Готовлю по воскресеньям на всю неделю",
    ],
    mutex_notes=[
        "Только когда юзер дал ответ на текущую тему онбординга. Для пропуска — onboarding_deferred. Для досрочного закрытия — onboarding_complete.",
        "topic ОБЯЗАН совпадать с current_topic из [ОНБОРДИНГ] блока — иначе runtime не запишет ответ к правильной теме.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ONBOARDING_DEFERRED_SPEC = ToolSpec(
    name="onboarding_deferred",
    description=(
        "Отметить тему как отложенную и перейти к следующей. Используй "
        "когда юзер явно просит пропустить («потом», «не сейчас», "
        "«пропусти»). Первый пропуск оставляет тему в retry-queue "
        "(topic_state=skipped_once), второй пропуск той же темы "
        "делает skip permanent (topic_state=skipped). ``reason`` — "
        "короткое объяснение для аудита, юзеру не показывается. "
        "Возвращает ok:deferred:topic:topic_state=...:next=...:status=... — "
        "топик_state различает «вернёмся позже» и «забыли совсем»."
    ),
    family="onboarding",
    effect="write",
    read_domains=["onboarding"],
    write_domains=["onboarding"],
    input_model=OnboardingDeferredInput,
    output_model=OnboardingDeferredOutput,
    trigger_examples=[
        "потом расскажу",
        "не сейчас, пропусти",
        "не хочу отвечать",
        "давай в другой раз",
    ],
    mutex_notes=[
        "Только когда юзер ЯВНО просит пропустить. Если юзер ответил уклончиво но всё же ответил — onboarding_answered с paraphrased summary.",
        "Первый skip — мягкий (вернёмся), второй — жёсткий. Различай в реакции на topic_state.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ONBOARDING_COMPLETE_SPEC = ToolSpec(
    name="onboarding_complete",
    description=(
        "Закрыть онбординг принудительно. Используй ТОЛЬКО когда "
        "юзер чётко говорит «всё, хватит, мне надоело» / «не хочу "
        "больше отвечать» ДО естественного завершения. Обычно "
        "онбординг авто-закрывается когда все темы закрыты "
        "(answered или permanently skipped) — этот tool НЕ нужен "
        "для нормального flow. Возвращает ok:complete:status=... "
        "(обычно `complete`, может быть `abandoned` при явном "
        "drop-out)."
    ),
    family="onboarding",
    effect="write",
    read_domains=["onboarding"],
    write_domains=["onboarding"],
    input_model=OnboardingCompleteInput,
    output_model=OnboardingCompleteOutput,
    trigger_examples=[
        "всё, хватит вопросов",
        "не хочу больше отвечать на это",
        "пропусти всё что осталось",
        "закрой опрос",
    ],
    mutex_notes=[
        "Только для досрочного закрытия. Авто-завершение при ответах на все темы — не требует этого вызова. Для пропуска ОДНОЙ темы — onboarding_deferred.",
    ],
    timeout_seconds=5,
    side_effect_class="transactional_write",
)


ONBOARDING_SPECS: list[ToolSpec] = [
    ONBOARDING_ANSWERED_SPEC,
    ONBOARDING_DEFERRED_SPEC,
    ONBOARDING_COMPLETE_SPEC,
]


__all__ = [
    "ONBOARDING_ANSWERED_SPEC",
    "ONBOARDING_COMPLETE_SPEC",
    "ONBOARDING_DEFERRED_SPEC",
    "ONBOARDING_SPECS",
    "OnboardingAnsweredInput",
    "OnboardingCompleteInput",
    "OnboardingDeferReason",
    "OnboardingDeferredInput",
    "OnboardingSummary",
]
