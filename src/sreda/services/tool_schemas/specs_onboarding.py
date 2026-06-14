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
)


# Codex Sub-A4 onboarding R2 MAJOR #1: restrict planner-side input
# to only the topic that's in the ACTIVE flow per
# housewife_onboarding.py:68. The full ``OnboardingTopic`` Literal
# (6 vals) lives in housewife.py for OUTPUT validation only (runtime
# echoes whatever topic was passed in; we keep the wider Literal so
# legacy clients passing other topics don't fail parsing). But the
# planner contract here REQUIRES addressing — that's the only topic
# the LLM should call onboarding_answered / onboarding_deferred on.
ActiveOnboardingTopic = Literal["addressing"]


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
"""Codex Sub-A4 onboarding R1 MAJOR #3: short reason for skip
(«потом», «не сейчас», «не отвечу»). Runtime ``mark_deferred``
does NOT actually store this value (housewife_chat_tools.py:602
calls service without passing reason). Schema-level: the field
remains in the planner contract as a forced moment of
self-reflection («why are we skipping?») — even unstored, the
planner must justify the skip in plan logs. Per A/B-derived
docs: «not stored — planner-facing reasoning context»."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class OnboardingAnsweredInput(BaseModel):
    """Mark onboarding addressing topic as answered + advance.

    Runtime ``HousewifeOnboardingService.mark_answered`` stores the
    summary as the user's saved answer, advances current_topic, and
    flips onboarding_status. Since active flow is only `addressing`
    (R2 MAJOR #1), this typically closes onboarding outright
    (status flips to 'complete').

    For ``addressing``: summary MUST be ONLY a short name/nickname
    (1-3 words), WITHOUT prefixes like «Пользователя зовут» /
    «Меня зовут». Runtime has a backend sanitiser as last-line
    defense; schema doesn't enforce that pattern because valid
    addressing answers vary widely (Russian short forms with
    diminutives, English names, nicknames)."""

    model_config = ConfigDict(extra="forbid")
    topic: ActiveOnboardingTopic
    summary: OnboardingSummary


class OnboardingDeferredInput(BaseModel):
    """Mark onboarding addressing topic as deferred + advance.

    First skip → topic_state=skipped_once (back in retry queue,
    will be re-asked). Second skip on the same topic →
    topic_state=skipped (permanently dropped).

    Codex R2 MAJOR #3: ``reason`` is NOT stored by runtime
    (housewife_chat_tools.py:602 drops it). Kept in schema as a
    planner-facing self-reflection field — forces the LLM to
    justify the skip in its plan logs, even though the value
    never reaches DB. NOT audit, NOT user-visible."""

    model_config = ConfigDict(extra="forbid")
    topic: ActiveOnboardingTopic
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
    # #144: ужато — статус-эхо (есть в output-строке) и цитата кода вырезаны
    description=(
        "Отметить тему ``addressing`` (имя/обращение) как отвеченную и "
        "перейти к следующей — это ЕДИНСТВЕННАЯ активная тема онбординга. "
        "Вызывай когда юзер дал осмысленный ответ на запрос имени из "
        "[ОНБОРДИНГ] block системного промпта. ``summary`` — ТОЛЬКО "
        "короткое имя/ник (1-3 слова, БЕЗ «Пользователя зовут», «Меня "
        "зовут»). После addressing онбординг авто-закрывается."
    ),
    family="onboarding",
    effect="write",
    read_domains=["onboarding"],
    write_domains=["onboarding"],
    input_model=OnboardingAnsweredInput,
    output_model=OnboardingAnsweredOutput,
    trigger_examples=[
        "Меня зовут Борис",
        "Я Аркадий",
        "Зови меня Шеф",
        "Анна, можно просто Аня",
    ],
    mutex_notes=[
        "Только когда юзер дал ответ на текущую тему онбординга (addressing). Для пропуска — onboarding_deferred. Для досрочного закрытия — onboarding_complete.",
        "topic ОБЯЗАН быть `addressing` — это единственная активная тема в текущем TOPIC_ORDER.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ONBOARDING_DEFERRED_SPEC = ToolSpec(
    name="onboarding_deferred",
    # #144: ужато — статус-эхо (есть в output-строке) вырезано
    description=(
        "Отметить тему ``addressing`` как отложенную. Используй когда юзер "
        "явно просит пропустить («потом», «не сейчас», «пропусти»). Первый "
        "пропуск оставляет тему в retry-queue (skipped_once) — runtime "
        "может re-ask; второй делает skip permanent (skipped). ``reason`` "
        "— короткое обоснование skip'а; runtime его НЕ хранит, но поле "
        "обязательно, чтобы планировщик явно артикулировал решение."
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
        "для нормального flow. Возвращает ok:complete:status=complete "
        "(runtime mark_complete всегда ставит status=complete, без "
        "других вариантов)."
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
