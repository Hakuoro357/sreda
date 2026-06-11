"""Tests for the composer registry (Sub-A5 foundation).

Coverage:
- Registry registration + render happy path
- Unknown ``template_id`` raises ``UnknownTemplateError``
- Missing template variable raises (StrictUndefined contract)
- ``snapshot_hash`` is stable across calls + changes on source change
- ``template_ids()`` is sorted + covers expected housewife set
- Every default template renders with realistic sample data (drift guard)
"""

from __future__ import annotations

import pytest
from jinja2 import TemplateError

from sreda.services.composer import (
    REGISTRY,
    ComposerRegistry,
    UnknownTemplateError,
    render,
)
from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES


# ---------------------------------------------------------------------------
# ComposerRegistry — registration / render basics
# ---------------------------------------------------------------------------


def test_register_and_render_basic() -> None:
    reg = ComposerRegistry()
    reg.register("greeting", "Привет, {{ name }}!")
    assert reg.render("greeting", {"name": "Боря"}) == "Привет, Боря!"


def test_unknown_template_raises() -> None:
    reg = ComposerRegistry()
    with pytest.raises(UnknownTemplateError) as exc:
        reg.render("nonexistent_id", {})
    assert "nonexistent_id" in str(exc.value)


def test_register_empty_template_id_rejected() -> None:
    reg = ComposerRegistry()
    with pytest.raises(ValueError):
        reg.register("", "anything")


def test_missing_variable_raises_strict_undefined() -> None:
    """StrictUndefined contract — typos in template_data fail loud,
    not silent empty render."""
    reg = ComposerRegistry()
    reg.register("greeting", "Привет, {{ name }}!")
    with pytest.raises(TemplateError):
        reg.render("greeting", {})


def test_re_register_overwrites() -> None:
    reg = ComposerRegistry()
    reg.register("x", "v1: {{ a }}")
    reg.register("x", "v2: {{ a }}")
    assert reg.render("x", {"a": "ok"}) == "v2: ok"


# ---------------------------------------------------------------------------
# template_ids + snapshot_hash
# ---------------------------------------------------------------------------


def test_template_ids_sorted() -> None:
    reg = ComposerRegistry()
    reg.register("zeta", "z")
    reg.register("alpha", "a")
    reg.register("middle", "m")
    assert reg.template_ids() == ["alpha", "middle", "zeta"]


def test_snapshot_hash_is_stable() -> None:
    reg = ComposerRegistry()
    reg.register("x", "{{ a }}")
    reg.register("y", "{{ b }}")
    h1 = reg.snapshot_hash()
    h2 = reg.snapshot_hash()
    assert h1 == h2


def test_snapshot_hash_changes_when_source_changes() -> None:
    reg1 = ComposerRegistry()
    reg1.register("x", "{{ a }}")
    h1 = reg1.snapshot_hash()
    reg2 = ComposerRegistry()
    reg2.register("x", "{{ b }}")  # different source, same id
    h2 = reg2.snapshot_hash()
    assert h1 != h2


def test_snapshot_hash_changes_when_id_added() -> None:
    reg = ComposerRegistry()
    reg.register("x", "{{ a }}")
    h_before = reg.snapshot_hash()
    reg.register("y", "{{ b }}")
    assert reg.snapshot_hash() != h_before


# ---------------------------------------------------------------------------
# Default REGISTRY — module shortcut + housewife coverage
# ---------------------------------------------------------------------------


def test_default_registry_render_shortcut() -> None:
    out = render("shopping_added_ok", {"items": ["хлеб", "молоко"]})
    assert out == "Записала: хлеб, молоко."


def test_default_registry_covers_expected_housewife_ids() -> None:
    """Sanity — top-5 tool outcomes + the partial_with_compose_error
    race fallback (Group 6.5) are all present."""
    expected = {
        "shopping_added_ok",
        "shopping_added_empty",
        "shopping_list_show",
        "shopping_list_empty",
        "reminder_set_ok",
        "reminder_skipped_past",
        "reminders_list_show",
        "reminders_list_empty",
        "recipe_show",
        "recipe_not_found_ask_alt",
        # clarification (vex-assistant#77 item #7)
        "ask_user_for_clarification",
        "ask_when_to_remind",
        "partial_with_clarification",
        # error / fallback
        "generic_tool_error",
        "partial_with_compose_error",
        # Sub-A12 Phase B.1 — planner-side invalid-plan fallback
        "invalid_plan_fallback",
    }
    assert set(REGISTRY.template_ids()) >= expected


def test_clarification_template_ids_match_schema_allowlist() -> None:
    """Codex Sub-A-77 #2 R1 — schema's CLARIFICATION_TEMPLATE_IDS
    allowlist and the actual registered templates must agree. If they
    drift, a planner emitting clarity='needs_clarification' could
    point at a template name in the allowlist that isn't registered
    → UnknownTemplateError at render time instead of validation time."""
    from sreda.runtime.planner.schemas import CLARIFICATION_TEMPLATE_IDS

    registered = set(REGISTRY.template_ids())
    for tid in CLARIFICATION_TEMPLATE_IDS:
        assert tid in registered, (
            f"CLARIFICATION_TEMPLATE_IDS lists {tid!r} but composer "
            f"registry does not have it. Register the template in "
            f"templates_housewife.py or remove from the allowlist."
        )


# ---------------------------------------------------------------------------
# Voice drift snapshots — one render per template with realistic data
# ---------------------------------------------------------------------------


def test_render_shopping_added_ok() -> None:
    assert render("shopping_added_ok", {"items": ["молоко"]}) == "Записала: молоко."


def test_render_shopping_added_empty() -> None:
    out = render(
        "shopping_added_empty", {"duplicates": ["хлеб", "молоко"]}
    )
    assert "Всё уже было" in out
    assert "хлеб, молоко" in out


def test_render_shopping_list_show() -> None:
    # #119: шаблон рендерит ЧИСТОЕ поле display_line — прежний снимок кормил
    # raw_line с [sh_…]-префиксом и закреплял утечку внутренних номеров.
    items = [
        {"display_line": "молоко (1 л)"},
        {"display_line": "хлеб"},
    ]
    out = render("shopping_list_show", {"count": 2, "items": items})
    assert "(2)" in out
    assert "молоко" in out
    assert "хлеб" in out
    assert "sh_" not in out


def test_render_shopping_list_empty() -> None:
    assert render("shopping_list_empty", {}) == "Список покупок пуст."


def test_render_reminder_set_ok() -> None:
    out = render(
        "reminder_set_ok",
        {"when_phrase": "сегодня в 18:00", "what": "забрать ребёнка"},
    )
    assert out == "Напомню сегодня в 18:00: забрать ребёнка."


def test_render_reminder_skipped_past() -> None:
    out = render(
        "reminder_skipped_past",
        {"trigger_at_local": "15:00", "late_by_minutes": 42},
    )
    assert "15:00" in out
    assert "42" in out
    assert "завтра" in out


def test_render_reminders_list_show() -> None:
    items = [{"display_line": "купить хлеб → 18:00"}]  # #119: чистое поле
    out = render("reminders_list_show", {"count": 1, "items": items})
    assert "(1)" in out
    assert "купить хлеб" in out
    assert "rem_" not in out


def test_render_reminders_list_empty() -> None:
    assert render("reminders_list_empty", {}) == "Активных напоминаний нет."


def test_render_recipe_show_passthrough() -> None:
    body = "Борщ\nИнгредиенты:\n- свёкла"
    assert render("recipe_show", {"recipe_text": body}) == body


def test_render_recipe_not_found_ask_alt() -> None:
    out = render("recipe_not_found_ask_alt", {"query": "плов с курицей"})
    assert "плов с курицей" in out
    assert "точнее" in out or "другое" in out


# ---------------------------------------------------------------------------
# Clarification — Plan.clarity=needs_clarification → template, no LLM call
# (vex-assistant#77 item #7)
# ---------------------------------------------------------------------------


def test_render_ask_when_to_remind_happy_path() -> None:
    """Specific narrow template — used by planner when only the time
    is missing for a reminder."""
    out = render("ask_when_to_remind", {"what": "купить подарок Маше"})
    assert "купить подарок Маше" in out
    assert "когда" in out
    # Контракт текста — должно быть предложение временного варианта.
    assert "сегодня" in out or "завтра" in out


def test_render_ask_user_for_clarification_single_known_field() -> None:
    """One known field (`time`) → uses the field-specific question line."""
    out = render(
        "ask_user_for_clarification",
        {
            "clarity_reason": "не указано время напоминания",
            "missing_fields": ["time"],
        },
    )
    assert "не указано время напоминания" not in out  # issue #88: clarity_reason NOT leaked
    # Single field path doesn't use plural "пару моментов".
    assert "пару моментов" not in out
    assert "когда" in out


def test_render_ask_user_for_clarification_multiple_known_fields() -> None:
    """Two known fields → plural prefix + both field-specific lines."""
    out = render(
        "ask_user_for_clarification",
        {
            "clarity_reason": "запрос двусмысленный",
            "missing_fields": ["time", "recipient"],
        },
    )
    assert "запрос двусмысленный" not in out  # issue #88: clarity_reason NOT leaked to user
    assert "пару моментов" in out
    assert "когда" in out
    assert "кому напомнить" in out


def test_render_ask_user_for_clarification_unknown_field_no_raw_echo() -> None:
    """Unknown field code → ``clarify_ru`` filter returns the generic phrase;
    the raw English/internal code is NEVER echoed to the user.

    issue #88 PR-a: the old else-branch (``— {{ field }}``) that echoed
    the raw code was replaced by ``{{ field | clarify_ru }}``. The filter
    returns ``GENERIC_CLARIFICATION`` for any code not in the closed enum,
    so the user always sees a friendly Russian phrase — never the planner's
    internal field name.  Unknown codes are also rejected by the schema at
    plan-time (``validate_clarification_payload``), so this path is an
    additional defense-in-depth guard at the render layer.
    """
    # Render directly (bypassing schema validation) to test filter fallback.
    out = render(
        "ask_user_for_clarification",
        {
            "clarity_reason": "не понимаю про что речь",
            "missing_fields": ["продукт_бренд_или_общий"],
        },
    )
    # Raw code must NOT appear in output.
    assert "продукт_бренд_или_общий" not in out
    # Generic fallback phrase must be present.
    assert "Уточни" in out or "пожалуйста" in out


def test_render_ask_user_for_clarification_no_fields() -> None:
    """No ``missing_fields`` provided → generic fallback question."""
    out = render(
        "ask_user_for_clarification",
        {
            "clarity_reason": "запрос непонятен",
            "missing_fields": [],
        },
    )
    assert "запрос непонятен" not in out  # issue #88: clarity_reason NOT leaked to user
    assert "подробнее" in out


def test_render_ask_user_for_clarification_no_reason() -> None:
    """No ``clarity_reason`` key in data → uses generic
    «Не до конца поняла запрос» opener. Defensive against planner
    forgetting the reason field. Same StrictUndefined-safe pattern as
    ``partial_with_compose_error`` (Codex 2026-05-26 MEDIUM)."""
    out = render(
        "ask_user_for_clarification",
        {
            "missing_fields": ["time"],
        },
    )
    # issue #88: with missing_fields present, the ask is built from the
    # structured field (no generic opener, no leaked clarity_reason).
    assert "когда" in out


def test_render_ask_user_for_clarification_empty_data() -> None:
    """Both ``clarity_reason`` and ``missing_fields`` absent → still
    renders. Generic opener + fallback question, no crash."""
    out = render("ask_user_for_clarification", {})
    assert "Не до конца поняла" in out
    assert "подробнее" in out


# ---------------------------------------------------------------------------
# Mixed-mode clarification: done_summary + ask in the same reply
# (Codex Sub-A-77 #2 R1 MAJOR #3 — without this, mixed-mode plans
# dropped acknowledgement of completed actions)
# ---------------------------------------------------------------------------


def test_render_partial_with_clarification_full() -> None:
    """Happy path — actions executed AND clarification needed for the rest."""
    out = render(
        "partial_with_clarification",
        {
            "done_summary": "добавила молоко в покупки",
            "clarity_reason": "не указано время для напоминания",
            "missing_fields": ["time"],
        },
    )
    assert "Сделала: добавила молоко в покупки" in out
    assert "не указано время для напоминания" not in out  # issue #88: clarity_reason NOT leaked
    assert "когда" in out


def test_render_partial_with_clarification_done_only() -> None:
    """Edge case — done_summary present but no clarification fields.
    Weird usage (probably planner bug — why use this template?) but
    should still render without crash."""
    out = render(
        "partial_with_clarification",
        {"done_summary": "добавила хлеб"},
    )
    assert "Сделала: добавила хлеб" in out
    # Generic opener falls back when clarity_reason missing.
    assert "Не до конца поняла" in out


def test_render_partial_with_clarification_clarify_only() -> None:
    """Edge case — no actions ran (executor short-circuited or planner
    used wrong template), only the clarification part renders. Should
    behave like ask_user_for_clarification."""
    out = render(
        "partial_with_clarification",
        {
            "clarity_reason": "не указано время",
            "missing_fields": ["time"],
        },
    )
    assert "Сделала:" not in out
    assert "не указано время" not in out  # issue #88: clarity_reason NOT leaked
    assert "когда" in out


def test_render_partial_with_clarification_multiple_fields() -> None:
    """Two+ missing fields → plural «пару моментов»."""
    out = render(
        "partial_with_clarification",
        {
            "done_summary": "записала молоко",
            "clarity_reason": "уточни ещё пару вещей",
            "missing_fields": ["time", "recipient"],
        },
    )
    assert "Сделала: записала молоко" in out
    assert "пару моментов" in out
    assert "когда" in out
    assert "кому напомнить" in out


def test_render_generic_tool_error() -> None:
    out = render("generic_tool_error", {"error_code": "internal"})
    # issue #88: error_code must NOT leak to the user. Правило Бориса
    # 2026-06-11: текст — живая «поломка» из пула.
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
    assert "internal" not in out
    assert out in BREAKDOWN_POOL


def test_render_partial_with_compose_error_with_summary() -> None:
    out = render(
        "partial_with_compose_error",
        {"execution_summary": "хлеб в покупках, напоминание на 18:00"},
    )
    assert "хлеб в покупках" in out
    assert "действия выполнены" in out


def test_render_partial_with_compose_error_without_summary() -> None:
    """``execution_summary`` empty should still render — the Jinja
    ``{% if %}`` guard skips the conditional clause."""
    out = render("partial_with_compose_error", {"execution_summary": ""})
    assert "действия выполнены" in out
    # Should NOT have an empty ": " hanging where the summary would go
    assert ": ." not in out


def test_render_partial_with_compose_error_missing_key_entirely() -> None:
    """Codex 2026-05-26 MEDIUM: even when caller omits
    ``execution_summary`` from template_data entirely (not just empty),
    StrictUndefined-safe template should still render. The
    ``is defined and`` guard handles missing-key case."""
    out = render("partial_with_compose_error", {})
    assert "действия выполнены" in out


# ---------------------------------------------------------------------------
# Codex review 2026-05-26 — autoescape OFF for Telegram plain-text delivery
# ---------------------------------------------------------------------------


def test_autoescape_disabled_for_telegram_output() -> None:
    """Variables containing ``&`` / ``<`` / ``>`` must NOT be entity-
    encoded — Telegram receives plain text, ``M&amp;M`` reads
    broken to users. Was MEDIUM Codex finding for Sub-A5."""
    reg = ComposerRegistry()
    reg.register("with_special", "Привет, {{ name }}!")
    out = reg.render("with_special", {"name": "M&M's <best>"})
    assert "M&M's <best>" in out  # raw, not entity-encoded
    assert "&amp;" not in out
    assert "&lt;" not in out


# ---------------------------------------------------------------------------
# Drift guard — each template in HOUSEWIFE_TEMPLATES is wired into REGISTRY
# ---------------------------------------------------------------------------


def test_every_template_in_dict_is_in_registry() -> None:
    """Defends against forgetting to re-register a new template entry."""
    for template_id in HOUSEWIFE_TEMPLATES:
        assert template_id in REGISTRY.template_ids(), (
            f"template_id={template_id!r} in HOUSEWIFE_TEMPLATES "
            f"but not in REGISTRY"
        )


# ---------------------------------------------------------------------------
# PR-d (Piece 3) — per-template contract registry COVERAGE
# ---------------------------------------------------------------------------


def test_every_exposed_template_is_contracted_or_explicit_no_contract() -> None:
    """Every planner-exposed template_id in the live composer REGISTRY must be
    classified in composer_contracts._COMPOSER_CONTRACTS as EITHER a contract
    callable OR the explicit NO_CONTRACT marker. None (unknown to the registry)
    means a template was added without a contract decision — fail loudly.

    Runtime-only templates (selector_ambiguity_clarification) are excluded:
    the planner never emits them, the validator rejects them upstream, so they
    carry no plan-time contract.
    """
    from sreda.services.clarification_contract import RUNTIME_ONLY_TEMPLATE_IDS
    from sreda.services.composer_contracts import (
        NO_CONTRACT,
        get_composer_contract,
    )

    unclassified: list[str] = []
    for tid in REGISTRY.template_ids():
        if tid in RUNTIME_ONLY_TEMPLATE_IDS:
            continue
        contract = get_composer_contract(tid)
        if contract is None:
            unclassified.append(tid)
        else:
            # Must be either NO_CONTRACT or a callable contract.
            assert contract is NO_CONTRACT or callable(contract), (
                f"template_id={tid!r} mapped to a non-callable, non-NO_CONTRACT "
                f"value {contract!r}"
            )
    assert not unclassified, (
        f"template_id(s) {unclassified} are exposed in the composer REGISTRY "
        f"but NOT classified in composer_contracts._COMPOSER_CONTRACTS. Add a "
        f"contract validator OR the explicit NO_CONTRACT marker for each — "
        f"every exposed template must have a contract decision (PR-d Piece 3)."
    )


def test_contract_registry_has_no_stale_or_runtime_only_keys() -> None:
    """Inverse guard: every key in the contract registry must be a real,
    planner-exposed template_id (not a typo, not a removed template, not a
    runtime-only id). Keeps the registry from drifting away from REGISTRY."""
    from sreda.services.clarification_contract import RUNTIME_ONLY_TEMPLATE_IDS
    from sreda.services.composer_contracts import _COMPOSER_CONTRACTS

    registered = set(REGISTRY.template_ids())
    for tid in _COMPOSER_CONTRACTS:
        assert tid in registered, (
            f"contract registry key {tid!r} is not a registered composer "
            f"template — remove it or fix the typo."
        )
        assert tid not in RUNTIME_ONLY_TEMPLATE_IDS, (
            f"contract registry must not contain runtime-only id {tid!r}"
        )


def test_clarification_family_is_contracted_not_no_contract() -> None:
    """The two clarification templates must map to the clarification validator
    (a callable), NOT NO_CONTRACT — they carry the closed missing_fields enum +
    literal done_summary contract."""
    from sreda.services.composer_contracts import (
        NO_CONTRACT,
        get_composer_contract,
    )

    for tid in ("ask_user_for_clarification", "partial_with_clarification"):
        contract = get_composer_contract(tid)
        assert callable(contract) and contract is not NO_CONTRACT, (
            f"{tid!r} must be a contract callable, got {contract!r}"
        )


# ---------------------------------------------------------------------------
# Sub-A12 Phase B.1 — invalid_plan_fallback (planner-side failure)
# ---------------------------------------------------------------------------


def test_render_invalid_plan_fallback_any_attempt_count_pool() -> None:
    """Правило Бориса 2026-06-11: невалидный план = «поломка» из пула при
    ЛЮБОМ attempt_count (ключ остаётся легальным опциональным — для логов)."""
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL

    for data in ({"attempt_count": 2}, {"attempt_count": 1}, {},
                 {"attempt_count": 3}):
        out = render("invalid_plan_fallback", data)
        assert out in BREAKDOWN_POOL, (data, out)
