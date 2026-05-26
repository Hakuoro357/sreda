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
        "generic_tool_error",
        "partial_with_compose_error",
    }
    assert set(REGISTRY.template_ids()) >= expected


# ---------------------------------------------------------------------------
# Voice drift snapshots — one render per template with realistic data
# ---------------------------------------------------------------------------


def test_render_shopping_added_ok() -> None:
    assert render("shopping_added_ok", {"items": ["молоко"]}) == "Записала: молоко."


def test_render_shopping_added_empty() -> None:
    out = render(
        "shopping_added_empty", {"duplicates": ["хлеб", "молоко"]}
    )
    assert "Все уже было" in out
    assert "хлеб, молоко" in out


def test_render_shopping_list_show() -> None:
    items = [
        {"raw_line": "[sh_abc] молоко (1 л)"},
        {"raw_line": "[sh_def] хлеб"},
    ]
    out = render("shopping_list_show", {"count": 2, "items": items})
    assert "(2)" in out
    assert "молоко" in out
    assert "хлеб" in out


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
    items = [{"raw_line": "[rem_1] купить хлеб → 18:00"}]
    out = render("reminders_list_show", {"count": 1, "items": items})
    assert "(1)" in out
    assert "купить хлеб" in out


def test_render_reminders_list_empty() -> None:
    assert render("reminders_list_empty", {}) == "Активных напоминаний нет."


def test_render_recipe_show_passthrough() -> None:
    body = "Борщ\nИнгредиенты:\n- свёкла"
    assert render("recipe_show", {"recipe_text": body}) == body


def test_render_recipe_not_found_ask_alt() -> None:
    out = render("recipe_not_found_ask_alt", {"query": "плов с курицей"})
    assert "плов с курицей" in out
    assert "точнее" in out or "другое" in out


def test_render_generic_tool_error() -> None:
    out = render("generic_tool_error", {"error_code": "internal"})
    assert "internal" in out


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
