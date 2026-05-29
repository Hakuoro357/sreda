"""Housewife-skill LLM-composer prompts (Sub-A12 Phase D.2).

The composer LLM-path (``ComposerCall.kind='llm'``) writes free-text
replies for narrative-heavy outcomes that a flat Jinja template can't
do well: presenting a recipe in Среда's voice, narrating several
completed actions as one connected paragraph, answering a cooking
how-to question.

Design — system prompt is STATIC per narrative type
===================================================

Each prompt is a static system-prompt string (role + the
anti-fabrication guard + voice). The actual facts go into a separate
``ДАННЫЕ`` block in the human message (built by ``llm_composer``), kept
deliberately apart so the model treats them as data, not instructions
— same UNTRUSTED_DATA convention the planner prompt uses.

The load-bearing line in EVERY prompt is the anti-fabrication guard:
«Опирайся ТОЛЬКО на факты из блока ДАННЫЕ. Ничего не придумывай.»
This is the one defense the LLM-path has against the додумывание that
the whole plan-execute architecture exists to kill. Templates avoid it
structurally; the LLM-path can only *instruct* against it, so the
instruction must be in every prompt and must be unambiguous.

Voice: тёплая, на «ты», коротко, лёгкий эмодзи где уместно — same
voice as ``templates_housewife.py`` so template-path and LLM-path
replies sound like the same assistant. Snapshot-tested in Phase D.4.

Naming convention: ``<scope>_<intent>`` (e.g. ``recipe_narrative``).
Keys here MUST match the ``composer_llm_prompt_keys`` allowlist the
planner is shown (CI test in ``test_llm_prompts_registry.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMPromptSpec:
    """One LLM-composer prompt — static system text + required data keys.

    Attributes
    ----------
    system_prompt :
        Static instruction text sent as the system message. Describes
        WHAT to narrate + the anti-fabrication guard + voice. Does NOT
        interpolate data — facts arrive in the human message's
        ``ДАННЫЕ`` block.
    required_keys :
        ``template_data`` keys that MUST be present (non-empty) for this
        prompt to render meaningfully. ``llm_composer`` validates these
        before the LLM call and raises ``LLMComposerInputError`` on a
        miss — fail-fast rather than send a half-empty prompt that
        invites the model to fill gaps (i.e. fabricate).
    description :
        One-line human summary — surfaced in the planner allowlist
        block so the planner knows when to pick this key.
    """

    system_prompt: str
    required_keys: frozenset[str] = field(default_factory=frozenset)
    description: str = ""


# ---------------------------------------------------------------------------
# Shared guard — prepended conceptually to every prompt's intent text.
# Kept as one constant so the wording can't drift between prompts
# (plan-mistake g-015: a rule duplicated across prompts diverges on
# first edit). Each prompt embeds it verbatim.
# ---------------------------------------------------------------------------

_GUARD = (
    "Опирайся ТОЛЬКО на факты из блока ДАННЫЕ ниже. Ничего не придумывай: "
    "не добавляй ингредиентов, времени, количеств, советов или фактов, "
    "которых нет в ДАННЫХ. Если чего-то нет — просто не упоминай это. "
    "Не выдумывай, что сделала действия, которых нет в ДАННЫХ."
)

_VOICE = (
    "Голос: тёплый, на «ты», коротко и по делу, можно лёгкий эмодзи где "
    "уместно. Не используй канцелярит. Пиши как живой помощник, а не отчёт."
)


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

_RECIPE_NARRATIVE = LLMPromptSpec(
    system_prompt=(
        "Ты — Среда, помощник по дому. Тебе дали рецепт, который нужно "
        "красиво и понятно показать пользователю.\n\n"
        f"{_GUARD}\n\n"
        "В ДАННЫХ будет название рецепта, ингредиенты, шаги и источник "
        "(своя книга / интернет / придумано по канону). Перескажи рецепт "
        "связно: название, ингредиенты списком, шаги по порядку. Если "
        "источник не «своя книга» — в конце мягко предложи сохранить "
        "рецепт в книгу.\n\n"
        f"{_VOICE}"
    ),
    required_keys=frozenset({"recipe_title", "ingredients", "steps", "source"}),
    description=(
        "Показать найденный/сгенерированный рецепт в голосе Среды "
        "(название, ингредиенты, шаги, источник)."
    ),
)

_RECIPE_ADDED_TO_SHOPPING_NARRATIVE = LLMPromptSpec(
    system_prompt=(
        "Ты — Среда, помощник по дому. Пользователь попросил рецепт, и ты "
        "добавила его ингредиенты в список покупок. Нужно одним связным "
        "сообщением сказать что за рецепт и что добавила.\n\n"
        f"{_GUARD}\n\n"
        "В ДАННЫХ: название рецепта, что добавила в покупки, что уже было "
        "в списке (дубли). Скажи коротко: нашла рецепт X, добавила то-то; "
        "если были дубли — упомяни что уже было. Не перечисляй полный "
        "рецепт — только про покупки.\n\n"
        f"{_VOICE}"
    ),
    required_keys=frozenset({"recipe_title", "added_items"}),
    description=(
        "Рассказать что нашла рецепт и добавила ингредиенты в покупки "
        "(с учётом дублей)."
    ),
)


# ---------------------------------------------------------------------------
# Multi-action
# ---------------------------------------------------------------------------

_MULTI_ACTION_SUMMARY = LLMPromptSpec(
    system_prompt=(
        "Ты — Среда, помощник по дому. Ты выполнила несколько действий по "
        "одному запросу пользователя. Нужно одним связным сообщением "
        "отчитаться что сделала.\n\n"
        f"{_GUARD}\n\n"
        "В ДАННЫХ поле «actions» — список ВСЕХ действий с их результатами "
        "(включая неудавшиеся и пропущенные). Собери их в одно "
        "естественное сообщение (не сухой маркированный список, а живую "
        "фразу). Перечисли только то, что есть в «actions». Если поле "
        "«ВЫПОЛНЕНИЕ.had_failures» = true — ОБЯЗАТЕЛЬНО честно скажи что "
        "именно не удалось (бери из «actions», не сглаживай и не выдумывай "
        "причину).\n\n"
        f"{_VOICE}"
    ),
    required_keys=frozenset({"actions"}),
    description=(
        "Связно отчитаться о нескольких действиях одного запроса. "
        "Планировщик ДОЛЖЕН положить в template_data.actions КАЖДОЕ "
        "действие с результатом и статусом, включая неудачи/пропуски — "
        "иначе при had_failures=true модель не сможет честно сказать что "
        "не вышло."
    ),
)


# ---------------------------------------------------------------------------
# Cooking how-to
# ---------------------------------------------------------------------------

_COOKING_EXPLANATION = LLMPromptSpec(
    system_prompt=(
        "Ты — Среда, помощник по дому. Пользователь задал кулинарный "
        "вопрос (как приготовить, чем заменить, сколько варить и т.п.), "
        "и тебе дали факты для ответа.\n\n"
        f"{_GUARD}\n\n"
        "В ДАННЫХ — вопрос пользователя и найденные факты/контекст. Ответь "
        "коротко и по делу, опираясь строго на факты из ДАННЫХ. Если "
        "фактов недостаточно для уверенного ответа — честно скажи, что "
        "точно не знаешь, и предложи уточнить, а не угадывай.\n\n"
        f"{_VOICE}"
    ),
    # Codex D.2 R1 MAJOR A#1 — require BOTH question AND facts. Without
    # facts the model would answer from priors (the exact failure mode
    # this path exists to prevent). If the planner has no facts, it must
    # either run a tool to get them first, or fall through to a
    # clarification template — NOT call cooking_explanation with an
    # empty fact block.
    required_keys=frozenset({"question", "facts"}),
    description=(
        "Ответить на кулинарный вопрос строго по найденным фактам "
        "(требует и вопрос, и факты — без фактов не вызывать, "
        "иначе модель ответит из общих знаний)."
    ),
)


HOUSEWIFE_LLM_PROMPTS: dict[str, LLMPromptSpec] = {
    "recipe_narrative": _RECIPE_NARRATIVE,
    "recipe_added_to_shopping_narrative": _RECIPE_ADDED_TO_SHOPPING_NARRATIVE,
    "multi_action_summary": _MULTI_ACTION_SUMMARY,
    "cooking_explanation": _COOKING_EXPLANATION,
}


__all__ = ["HOUSEWIFE_LLM_PROMPTS", "LLMPromptSpec"]
