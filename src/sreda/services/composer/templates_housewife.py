"""Housewife-skill composer templates (Sub-A5 foundation).

One Russian-language Jinja2 template per typical plan outcome. The
planner references these by ``template_id`` in its ``compose`` step;
the executor renders with values from ``execution_log_json``.

Naming convention: ``<scope>_<outcome>`` (e.g. ``shopping_added_ok``,
``recipe_not_found_ask_alt``). Lower-case + underscores so the planner
matches deterministically.

Voice: брунетка, на «ты», коротко, с лёгким эмодзи там где уместно.
Snapshot-tested in ``test_composer_registry.py::test_render_*`` so
voice drift is caught at PR time, not in production.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Shopping
# ---------------------------------------------------------------------------

_SHOPPING_ADDED_OK = "Записала: {{ items | join(', ') }}."

_SHOPPING_ADDED_EMPTY = (
    "Все уже было в списке — {{ duplicates | join(', ') }}. "
    "Ничего нового не добавила."
)

# #118: элемент может быть объектом ({raw_line}) ИЛИ плоской строкой — план
# вправе привязать ${sN.created} (список строк из write-выдачи #115); строгая
# форма роняла рендер уже ПОСЛЕ исполнения («'str object' has no attribute…»),
# где повтора планировщика больше нет. Терпимый рендер отдаёт имена в обоих
# случаях; формы элементов поверяет контракт (composer_contracts).
# #118 R4: ``count`` ОПЦИОНАЛЕН — у list_shopping в выдаче нет такого поля,
# планировщик не может удовлетворить его ссылкой (только неизвестным до
# исполнения литералом) → обязательный count делал шаблон неиспользуемым
# (диагноз: 3/3 проб модель привязывала items, но не count).
# истинностный сторож (R4 MINOR): count=None/'' /0 не рендерит «(None)»/«()»
_SHOPPING_LIST_SHOW = (
    "В списке покупок{% if count is defined and count %} ({{ count }}){% endif %}:"
    "{% for it in items %}"
    "\n• {% if it is mapping %}{{ it.raw_line }}{% else %}{{ it }}{% endif %}"
    "{% endfor %}"
)

_SHOPPING_LIST_EMPTY = "Список покупок пуст."

# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

_REMINDER_SET_OK = "Напомню {{ when_phrase }}: {{ what }}."

_REMINDER_SKIPPED_PAST = (
    "Это время уже прошло ({{ trigger_at_local }}, {{ late_by_minutes }} минут назад). "
    "Назначить на завтра в это же время?"
)

_REMINDERS_LIST_SHOW = (
    "Напоминания{% if count is defined and count %} ({{ count }}){% endif %}:"
    "{% for it in items %}"
    "\n• {% if it is mapping %}{{ it.raw_line }}{% else %}{{ it }}{% endif %}"
    "{% endfor %}"
)

_REMINDERS_LIST_EMPTY = "Активных напоминаний нет."

# ---------------------------------------------------------------------------
# Checklists (show_checklist → ShowChecklistOk / ShowChecklistEmpty)
# ---------------------------------------------------------------------------
# Mirrors the shopping-list pair. Fields come from ShowChecklistOk:
# ``title`` (str) + ``items`` (list of ShowChecklistItem{title, item_status}).
# A done item gets a ✅; pending/cancelled render plain.

# #118: то же — объект ({title, item_status}) ИЛИ плоская строка (см. выше).
_CHECKLIST_SHOW = (
    "{{ title }}:"
    "{% for it in items %}"
    "\n• {% if it is mapping %}{{ it.title }}"
    "{% if it.item_status == 'done' %} ✅{% endif %}"
    "{% else %}{{ it }}{% endif %}"
    "{% endfor %}"
)

_CHECKLIST_EMPTY = "Список «{{ title }}» пока пустой."

# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

_RECIPE_SHOW = "{{ recipe_text }}"

_RECIPE_NOT_FOUND_ASK_ALT = (
    "Не нашла рецепт «{{ query }}». Подскажи название точнее "
    "или скажи другое блюдо."
)

# ---------------------------------------------------------------------------
# Clarification (Plan.clarity=needs_clarification → template, no LLM call)
# (vex-assistant#77 item #7)
# ---------------------------------------------------------------------------

_ASK_USER_FOR_CLARIFICATION = (
    # issue #88 Root-Cause-B: DO NOT echo the planner's internal
    # ``clarity_reason`` to the user — it leaks as raw reasoning
    # ("приветствие — не указано, чем могу помочь"). The user-facing ask is
    # built from the structured ``missing_fields`` (rendered into friendly
    # bullet prompts via ``clarify_ru`` filter) plus a clean canned question.
    # ``clarity_reason`` stays internal (logged / trace only), never rendered.
    # (StrictUndefined: use ``is defined and`` so callers may omit fields.)
    # issue #88 PR-a: raw ``{{ field }}`` else-branch removed. All field codes
    # now go through the ``clarify_ru`` Jinja filter which maps each closed-
    # enum code to its Russian question fragment, falling back to the generic
    # phrase for any unknown code — the raw English code never reaches the user.
    "{% if missing_fields is defined and missing_fields %}"
    "Уточни{% if missing_fields|length > 1 %} пару моментов{% endif %}:"
    "{% for field in missing_fields %}"
    "\n— {{ field | clarify_ru }}"
    "{% endfor %}"
    "{% else %}"
    "Не до конца поняла запрос. Скажи чуть подробнее, что именно нужно?"
    "{% endif %}"
)

_ASK_WHEN_TO_REMIND = (
    "Хорошо, поставлю напоминание про «{{ what }}». "
    "А когда напомнить — сегодня, завтра, или конкретная дата?"
)

# Mixed mode — some actions ran successfully, others need clarification.
# Codex Sub-A-77 #2 R1 MAJOR #3 — without this template, mixed-mode plans
# would silently drop the acknowledgement of completed actions, leaving
# the user wondering whether their request was processed.
_PARTIAL_WITH_CLARIFICATION = (
    # issue #88 Root-Cause-B: same fix as ask_user_for_clarification —
    # never echo the internal ``clarity_reason`` to the user; build the ask
    # from ``missing_fields`` + a clean canned line. Keep the done_summary ack.
    # issue #88 PR-a: raw ``{{ field }}`` else-branch removed. All field codes
    # now go through the ``clarify_ru`` Jinja filter (same as
    # ask_user_for_clarification) — the raw English code never reaches the user.
    "{% if done_summary is defined and done_summary %}"
    "Сделала: {{ done_summary }}.\n\n"
    "{% endif %}"
    "Не до конца поняла остальное."
    "{% if missing_fields is defined and missing_fields %}"
    "\n\nУточни{% if missing_fields|length > 1 %} пару моментов{% endif %}:"
    "{% for field in missing_fields %}"
    "\n— {{ field | clarify_ru }}"
    "{% endfor %}"
    "{% endif %}"
)

# ---------------------------------------------------------------------------
# PR-c 2d — Selector ambiguity clarification
# ---------------------------------------------------------------------------
# Rendered by the execute→compose consumer (replay harness / future PR-2b node)
# when ExecutionLog.abort_kind == "selector_ambiguity".  The template receives:
#   options  : list[str]  — id-stripped public labels (may be empty for 0-case)
#   count    : int        — actual number of items found (0 or >1)
#   partial  : bool       — True when aborted_partial (earlier writes committed)
#   done_summary : str    — (only when partial=True) what was already done
#
# Voice: на «ты», terse, matching Среда style.
# ``is defined and`` guards: template_data keys may be absent in edge cases.
_SELECTOR_AMBIGUITY_CLARIFICATION = (
    # RUNTIME-ONLY template (Codex PR-c R1): produced by
    # selector.clarification_compose_for_abort() AFTER execution for a `.only`
    # ambiguity (>1 matches) — NEVER emitted by the planner, so it is NOT in
    # CLARIFICATION_TEMPLATE_IDS. Handles BOTH abort cases (Codex PR-c R4 high):
    #   - plain aborted (no prior writes, partial=False): option labels + count.
    #   - aborted_partial (prior writes committed, partial=True): a GENERIC
    #     literal done_summary acknowledgement ("Сделала: …") THEN the same
    #     option labels + count — so the partial user still sees WHAT to
    #     disambiguate (partial_with_clarification has no options slot). The
    #     done_summary is a generic literal built in selector.py, NEVER raw tool
    #     names (the PR-c R1 leak class stays closed).
    # ``options`` are pre-sanitized (id-stripped) public labels.
    "{% if partial is defined and partial and done_summary is defined and done_summary %}"
    "Сделала: {{ done_summary }}.\n\n"
    "{% endif %}"
    "{% if options is defined and options %}"
    "Уточни, какое из {{ count }}:"
    "{% for opt in options %}"
    "\n— {{ opt }}"
    "{% endfor %}"
    "{% else %}"
    "Нашла {{ count }} вариантов — уточни словами, какой именно."
    "{% endif %}"
)

# ---------------------------------------------------------------------------
# Error / partial / fallback
# ---------------------------------------------------------------------------

_GENERIC_TOOL_ERROR = (
    # issue #88 (Codex fix-B R1): do NOT leak the internal ``error_code``
    # (e.g. "identity_question", "llm_composer_error:RuntimeError") to the
    # user — it reads as a broken internal diagnostic. Render a clean canned
    # message; ``error_code`` stays in ComposeResult / logs for triage.
    "Ой, не получилось обработать запрос. Попробуй ещё раз через минуту."
)

_PARTIAL_WITH_COMPOSE_ERROR = (
    "Сделала что просила"
    "{% if execution_summary is defined and execution_summary %}"
    ": {{ execution_summary }}"
    "{% endif %}. "
    "С финальным сообщением что-то пошло не так, но действия выполнены."
)
# Codex review 2026-05-26 MEDIUM/LOW fix: StrictUndefined raises on
# bare ``{% if x %}`` when ``x`` is missing from template_data (not
# just falsy). Use ``is defined and`` so callers can legitimately
# omit execution_summary entirely without crashing the partial path.
"""Group 6.5 ``compose_failure_after_execution`` path — used when the
planner-chosen template_id became invalid between Phase B validation
and Phase D compose (registry deploy race). Tools already ran; we just
need to acknowledge without re-running."""


_INVALID_PLAN_FALLBACK = (
    "{% if attempt_count is defined and attempt_count == 2 %}"
    "Не получилось разобрать что ты хочешь. Попробуй переформулировать, "
    "например: «купи молоко» или «покажи список покупок»."
    "{% else %}"
    "Не получилось обработать запрос, попробуй ещё раз."
    "{% endif %}"
)
"""Sub-A12 Phase B.1 — used when planner returns invalid plan after
all retries are exhausted (orchestrator.run returns success=False).
NO tools have been called — this is the entry-point failure path.

``attempt_count``: 1 = first attempt failed only (single-shot
no-retry mode); 2 = after one explicit retry. Default branch covers
non-2 values (no validation crash; honest message)."""


# ---------------------------------------------------------------------------
# Conversational / identity — rot-enablement Phase 1 (issue #88)
# ---------------------------------------------------------------------------

# Fallback for smalltalk turns when the LLM composer fails/times-out/blanks,
# AND the deterministic gate-OFF greeting path (when the smalltalk LLM key is
# disabled). Never blank — a warm reply without any LLM call.
# Registered so compose() can fall through to it on llm_composer failure for
# reply_only+smalltalk turns (per-type fallback, not generic_tool_error).
# Codex #88 Phase-2 R3 (medium MINOR): kept GREETING-AGNOSTIC on purpose —
# the boundary covers «привет», «спасибо», «как дела», «что нового», so the
# text must not presume a greeting (a «Привет!» reply to «спасибо» is wrong).
# A neutral warm "I'm here to help" reads fine across all of them.
_SMALLTALK_FALLBACK = "Я здесь и рада помочь 😊"

# Deterministic witty reply for «кто ты / на каком LLM / что ты за модель»
# questions. MUST NOT name the real model/provider (MiMo, OpenAI, Anthropic,
# GPT, Claude, Qwen, mimo — case-insensitive denylist tested in
# test_planner_schemas_reply_only.py::test_identity_template_denylist).
# Reliable and cheap — no LLM call, can't hallucinate the real model.
# Per R2 decision: deterministic template preferred over LLM for identity.
_IDENTITY_PLAYFUL = (
    "Я Среда — твоя помощница по дому и делам 🏠✨ "
    "Слежу за списком покупок, напоминаниями, рецептами и всем, "
    "что нужно по хозяйству.\n\n"
    "А что у меня «под капотом» — маленький секрет 😄 "
    "Скажем так: магия и немного технологий."
)


HOUSEWIFE_TEMPLATES: dict[str, str] = {
    # shopping
    "shopping_added_ok": _SHOPPING_ADDED_OK,
    "shopping_added_empty": _SHOPPING_ADDED_EMPTY,
    "shopping_list_show": _SHOPPING_LIST_SHOW,
    "shopping_list_empty": _SHOPPING_LIST_EMPTY,
    # reminders
    "reminder_set_ok": _REMINDER_SET_OK,
    "reminder_skipped_past": _REMINDER_SKIPPED_PAST,
    "reminders_list_show": _REMINDERS_LIST_SHOW,
    "reminders_list_empty": _REMINDERS_LIST_EMPTY,
    # checklists (show_checklist)
    "checklist_show": _CHECKLIST_SHOW,
    "checklist_empty": _CHECKLIST_EMPTY,
    # recipes
    "recipe_show": _RECIPE_SHOW,
    "recipe_not_found_ask_alt": _RECIPE_NOT_FOUND_ASK_ALT,
    # clarification (Plan.clarity=needs_clarification — vex-assistant#77 #7 + #2)
    "ask_user_for_clarification": _ASK_USER_FOR_CLARIFICATION,
    "ask_when_to_remind": _ASK_WHEN_TO_REMIND,
    "partial_with_clarification": _PARTIAL_WITH_CLARIFICATION,
    # PR-c 2d — selector ambiguity clarification (runtime-generated, not plan-time)
    "selector_ambiguity_clarification": _SELECTOR_AMBIGUITY_CLARIFICATION,
    # error / fallback
    "generic_tool_error": _GENERIC_TOOL_ERROR,
    "partial_with_compose_error": _PARTIAL_WITH_COMPOSE_ERROR,
    # Sub-A12 Phase B.1 — planner-side invalid-plan fallback
    "invalid_plan_fallback": _INVALID_PLAN_FALLBACK,
    # rot-enablement Phase 1 (issue #88) — conversational / identity
    "identity_playful": _IDENTITY_PLAYFUL,
    # Smalltalk LLM fallback — used when llm_composer fails on reply_only+smalltalk.
    # Deterministic warm greeting, never calls an LLM.
    "smalltalk_fallback": _SMALLTALK_FALLBACK,
}


__all__ = ["HOUSEWIFE_TEMPLATES"]
