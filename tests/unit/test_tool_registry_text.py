"""Tests for ``services/tool_schemas/registry_text.py`` — the planner-
facing Russian text registry (Sub-A-77 item #1).

What this guards:

1. ``render_family_header`` emits the exact anti-pattern block shape
   the prompt cache depends on.
2. ``render_registry_for_planner`` groups tools by family in the
   canonical FAMILIES order; missing-family tools surface in a final
   ``НЕОТНЕСЁННЫЕ`` block, never silently dropped.
3. Empty families are skipped (tenant tier may have a subset of tools).
4. Tool line order within a family is alphabetical (cache stability).
5. Args summary handles empty / required-only / required+optional /
   >4-fields correctly.

Snapshot tests for the full text would be brittle while the family
content evolves — instead we assert structural properties (family
labels present, anti-pattern marker present, tool order, etc.).
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, Field

from sreda.services.tool_schemas.base import ToolOutput, ToolSpec
from sreda.services.tool_schemas.families import FAMILIES, FAMILY_HEADERS
from sreda.services.tool_schemas.registry_text import (
    render_family_header,
    render_registry_for_planner,
)


# ---------------------------------------------------------------------------
# Fixtures — ad-hoc ToolSpecs for testing the renderer
# ---------------------------------------------------------------------------


class _EmptyInput(BaseModel):
    pass


class _OneRequiredInput(BaseModel):
    title: str


class _RequiredAndOptionalInput(BaseModel):
    items: list[str]
    category: str | None = None


class _ManyFieldsInput(BaseModel):
    a: str
    b: str
    c: str
    d: str
    e: str  # 5th — should be truncated with ellipsis
    f: str  # 6th — never appears


class _OkOutput(ToolOutput):
    status: Literal["ok"] = "ok"


def _spec(
    name: str,
    family: str,
    *,
    description: str = "Базовое описание инструмента",
    effect: str = "read",
    input_model: type[BaseModel] = _OneRequiredInput,
) -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name=name,
        description=description,
        family=family,  # type: ignore[arg-type]
        effect=effect,  # type: ignore[arg-type]
        read_domains=["shopping"] if effect == "read" else [],
        write_domains=["shopping"] if effect == "write" else [],
        input_model=input_model,
        output_model=_OkOutput,
    )


# ---------------------------------------------------------------------------
# render_family_header — single-family block
# ---------------------------------------------------------------------------


def test_render_family_header_starts_with_label() -> None:
    text = render_family_header("shopping", tool_count=7)
    first_line = text.splitlines()[0]
    assert first_line == "ГРУППА: ПОКУПКИ (7 инструментов)"


def test_render_family_header_includes_purpose() -> None:
    text = render_family_header("shopping", tool_count=3)
    assert FAMILY_HEADERS["shopping"].purpose in text


def test_render_family_header_includes_warning_marker() -> None:
    text = render_family_header("reminders", tool_count=4)
    # The ⚠ marker is the explicit cue for the LLM that the next bullets
    # are exclusions, not capabilities. Must always be present.
    assert "⚠ НЕ ИСПОЛЬЗОВАТЬ:" in text


def test_render_family_header_lists_every_anti_pattern() -> None:
    text = render_family_header("tasks", tool_count=11)
    for ap in FAMILY_HEADERS["tasks"].anti_patterns:
        assert ap in text


def test_render_family_header_uses_bullet_indent() -> None:
    text = render_family_header("recipes", tool_count=5)
    # Every anti-pattern is rendered as `  • <text>` — two-space indent
    # + bullet + space. Stable shape so prompt cache doesn't drift.
    for ap in FAMILY_HEADERS["recipes"].anti_patterns:
        assert f"  • {ap}" in text


def test_render_family_header_no_trailing_newline() -> None:
    # Renderer combining headers + tools controls inter-section spacing;
    # the header itself must not pad the end.
    text = render_family_header("memory", tool_count=3)
    assert not text.endswith("\n")


# ---------------------------------------------------------------------------
# render_registry_for_planner — empty + single + multi-family
# ---------------------------------------------------------------------------


def test_render_empty_registry_returns_empty_string() -> None:
    # No specs → no family blocks → empty result. Caller never builds a
    # prompt from this in production, but the API mustn't crash.
    assert render_registry_for_planner([]) == ""


def test_render_single_family_produces_one_block() -> None:
    specs = [
        _spec("add_shopping_items", "shopping", effect="write"),
        _spec("list_shopping", "shopping", effect="read"),
    ]
    text = render_registry_for_planner(specs)
    # Russian grammatical plural: 2 → «инструмента» (genitive singular).
    assert "ГРУППА: ПОКУПКИ (2 инструмента)" in text
    # Only one family **header block** — no other family's header label.
    # Anti-patterns intentionally mention other families by name as
    # redirect targets («см. группу ПАМЯТЬ»), so we can't assert the
    # russian_name is fully absent — only that no other family's
    # ``ГРУППА: <name>`` block heading appears.
    other_families = [f for f in FAMILIES if f != "shopping"]
    for f in other_families:
        block_label = f"ГРУППА: {FAMILY_HEADERS[f].russian_name}"
        assert block_label not in text


def test_render_emits_families_in_canonical_order() -> None:
    # Even if we pass specs in random order, families appear in
    # ``FAMILIES`` order. Cache stability invariant.
    specs = [
        _spec("recall_memory", "memory"),
        _spec("schedule_reminder", "reminders", effect="write"),
        _spec("add_shopping_items", "shopping", effect="write"),
    ]
    text = render_registry_for_planner(specs)
    # FAMILIES order: shopping(0), reminders(1), ..., memory(9)
    pos_shop = text.find("ГРУППА: ПОКУПКИ")
    pos_rem = text.find("ГРУППА: НАПОМИНАНИЯ")
    pos_mem = text.find("ГРУППА: ПАМЯТЬ")
    assert 0 <= pos_shop < pos_rem < pos_mem


def test_render_tools_within_family_in_alphabetical_order() -> None:
    # Same family → alphabetical by name for cache stability.
    specs = [
        _spec("zeta_tool", "shopping"),
        _spec("alpha_tool", "shopping"),
        _spec("mid_tool", "shopping"),
    ]
    text = render_registry_for_planner(specs)
    pos_a = text.find("alpha_tool(")
    pos_m = text.find("mid_tool(")
    pos_z = text.find("zeta_tool(")
    assert 0 <= pos_a < pos_m < pos_z


def test_render_skips_families_with_no_tools() -> None:
    # Only shopping has tools → only shopping family block exists.
    specs = [_spec("list_shopping", "shopping")]
    text = render_registry_for_planner(specs)
    assert "ГРУППА: НАПОМИНАНИЯ" not in text
    assert "(0 инструментов)" not in text


def test_render_tool_count_in_header_matches_actual_count() -> None:
    specs = [
        _spec(f"shop_tool_{i}", "shopping")
        for i in range(7)
    ]
    text = render_registry_for_planner(specs)
    assert "ГРУППА: ПОКУПКИ (7 инструментов)" in text


def test_render_separates_family_blocks_with_blank_line() -> None:
    # Visual separation for the planner LLM — two newlines between
    # consecutive family blocks.
    specs = [
        _spec("list_shopping", "shopping"),
        _spec("list_reminders", "reminders"),
    ]
    text = render_registry_for_planner(specs)
    assert "\n\n" in text


# ---------------------------------------------------------------------------
# Tool line formatting — args summary corner cases
# ---------------------------------------------------------------------------


def test_tool_line_with_empty_input_model_renders_no_args() -> None:
    specs = [_spec("ping", "utility", input_model=_EmptyInput,
                   description="Проверка живости.")]
    text = render_registry_for_planner(specs)
    assert "ping() — Проверка живости." in text


def test_tool_line_with_required_arg_renders_bare_name() -> None:
    specs = [_spec("get_recipe", "recipes", input_model=_OneRequiredInput,
                   description="Получить рецепт по ID.")]
    text = render_registry_for_planner(specs)
    assert "get_recipe(title) — Получить рецепт по ID." in text


def test_tool_line_with_optional_arg_renders_question_mark() -> None:
    specs = [_spec(
        "add_shopping_items", "shopping",
        input_model=_RequiredAndOptionalInput,
        description="Добавить продукты.",
    )]
    text = render_registry_for_planner(specs)
    assert "add_shopping_items(items, category?) — Добавить продукты." in text


def test_tool_line_with_many_fields_truncates_with_ellipsis() -> None:
    # >4 fields → first 4 + ellipsis. Don't crowd the line.
    specs = [_spec(
        "complex_tool", "utility",
        input_model=_ManyFieldsInput,
        description="Много параметров.",
    )]
    text = render_registry_for_planner(specs)
    assert "complex_tool(a, b, c, d, ...) — Много параметров." in text


def test_tool_line_uses_first_line_of_multiline_description() -> None:
    # Some legacy descriptions have multi-line docstrings; the registry
    # only shows the first line so the planner doesn't see formatting
    # noise.
    specs = [_spec(
        "verbose_tool", "memory",
        description="Главная строка описания.\nВторая строка с деталями.\nТретья.",
    )]
    text = render_registry_for_planner(specs)
    assert "verbose_tool(title) — Главная строка описания." in text
    assert "Вторая строка" not in text


# ---------------------------------------------------------------------------
# Unfamilied surface — bad config visible, not silently dropped
# ---------------------------------------------------------------------------


def test_render_unfamilied_tools_surface_as_error_block() -> None:
    # We can't pass a tool with family=None through ToolSpec (it's a
    # required field). Simulate by monkey-attacking: build a valid spec
    # then override .family to an unknown literal value to mimic
    # configuration drift (e.g. someone added a 13th literal value to
    # Family but forgot to add it to FAMILY_HEADERS).
    specs = [_spec("rogue_tool", "shopping")]
    # Override directly — pydantic frozen blocks set, but ToolSpec is
    # not frozen. If pydantic complains we use model_copy.
    rogue = specs[0].model_copy(update={"family": "thirteenth_family"})  # type: ignore[arg-type]
    text = render_registry_for_planner([rogue])
    # The block is explicit so the operator notices on first render
    # instead of finding a silently missing tool weeks later.
    assert "ГРУППА: НЕОТНЕСЁННЫЕ" in text
    assert "rogue_tool" in text


def test_render_unfamilied_block_appears_after_known_families() -> None:
    known = _spec("list_shopping", "shopping")
    rogue = known.model_copy(
        update={"name": "rogue_tool", "family": "thirteenth_family"}  # type: ignore[arg-type]
    )
    text = render_registry_for_planner([known, rogue])
    pos_known = text.find("ГРУППА: ПОКУПКИ")
    pos_rogue = text.find("ГРУППА: НЕОТНЕСЁННЫЕ")
    assert 0 <= pos_known < pos_rogue


# ---------------------------------------------------------------------------
# Token budget regression — full 12-family render with placeholder tools
# ---------------------------------------------------------------------------


def test_full_12_family_render_within_rough_budget() -> None:
    # Render with one placeholder tool per family. Soft budget: <2000
    # chars for the headers + tool lines (≈500 tokens). Tools tier
    # adds 50-100 tokens per tool on top in production; budget here
    # checks just the framework.
    specs = []
    for fam in FAMILIES:
        specs.append(_spec(
            f"placeholder_{fam}", fam,
            input_model=_OneRequiredInput,
            description=f"Placeholder for {fam}.",
        ))
    text = render_registry_for_planner(specs)
    # Every family header label should appear.
    for fam in FAMILIES:
        assert FAMILY_HEADERS[fam].russian_name in text
    # Soft 8000-char ceiling on the full skeleton — sound the alarm
    # if headers balloon past it.
    assert len(text) < 8000, (
        f"Full 12-family render is {len(text)} chars — exceeds 8000 "
        f"soft cap. Trim anti-patterns or split sections."
    )


# ---------------------------------------------------------------------------
# Deterministic output — same input → same output
# ---------------------------------------------------------------------------


def test_render_is_deterministic_for_same_input() -> None:
    specs = [
        _spec("z_tool", "shopping"),
        _spec("a_tool", "shopping"),
        _spec("m_tool", "reminders"),
    ]
    text_a = render_registry_for_planner(specs)
    # Same list passed again — must produce byte-identical output for
    # the prompt cache to hit.
    text_b = render_registry_for_planner(specs)
    assert text_a == text_b
    # Same content in a different iteration order also must produce
    # identical output (groupby + sort eliminates input-order
    # dependence).
    reshuffled = [specs[2], specs[0], specs[1]]
    text_c = render_registry_for_planner(reshuffled)
    assert text_a == text_c


# ---------------------------------------------------------------------------
# Codex R1 MINOR #12 — Russian pluralization for «инструмент»
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n,expected", [
    (0, "инструментов"),         # genitive plural for 0
    (1, "инструмент"),           # nominative singular
    (2, "инструмента"),          # genitive singular (Russian rule)
    (3, "инструмента"),
    (4, "инструмента"),
    (5, "инструментов"),
    (10, "инструментов"),
    (11, "инструментов"),        # 11-14 always genitive plural
    (12, "инструментов"),
    (14, "инструментов"),
    (15, "инструментов"),
    (21, "инструмент"),          # 21 follows last-digit 1 rule
    (22, "инструмента"),         # 22 follows last-digit 2 rule
    (25, "инструментов"),
    (101, "инструмент"),
    (111, "инструментов"),       # last two digits 11 → plural
])
def test_pluralize_instruments_russian_grammar(n: int, expected: str) -> None:
    from sreda.services.tool_schemas.registry_text import _pluralize_instruments
    assert _pluralize_instruments(n) == expected


def test_render_family_header_uses_pluralized_form_for_one_tool() -> None:
    # 1 → «инструмент», not «инструментов».
    text = render_family_header("shopping", tool_count=1)
    assert "1 инструмент)" in text
    assert "1 инструментов" not in text


def test_render_family_header_uses_pluralized_form_for_few_tools() -> None:
    # 3 → «инструмента» (genitive singular for 2-4).
    text = render_family_header("recipes", tool_count=3)
    assert "3 инструмента)" in text


# ---------------------------------------------------------------------------
# Codex R1 MINOR #11 — args summary respects pydantic aliases
# ---------------------------------------------------------------------------


def test_tool_line_uses_alias_when_present() -> None:
    """If a pydantic field has an alias, the planner-facing args list
    must use that alias (because the JSON-Schema the planner consumes
    uses aliases too)."""
    from pydantic import Field as PydField

    class AliasedInput(BaseModel):
        # Python attribute is ``items_internal`` but planner sees ``items``.
        items_internal: list[str] = PydField(alias="items")

    specs = [_spec(
        "aliased_tool", "shopping",
        input_model=AliasedInput,
        description="Aliased description.",
    )]
    text = render_registry_for_planner(specs)
    # Alias wins.
    assert "aliased_tool(items)" in text
    assert "items_internal" not in text


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #3 — ToolSpec.family is now Optional (additive change)
# ---------------------------------------------------------------------------


def test_tool_spec_without_family_surfaces_in_unfamilied_block() -> None:
    """With ``family: Family | None = None`` the renderer reaches the
    НЕОТНЕСЁННЫЕ branch naturally — no model_copy hackery required."""
    # Build a spec WITHOUT passing family — exercises default=None.
    spec_no_family = ToolSpec(  # type: ignore[arg-type]
        name="unassigned_tool",
        description="No family declared.",
        # family omitted — defaults to None
        effect="read",
        read_domains=["shopping"],
        write_domains=[],
        input_model=_OneRequiredInput,
        output_model=_OkOutput,
    )
    text = render_registry_for_planner([spec_no_family])
    assert "ГРУППА: НЕОТНЕСЁННЫЕ" in text
    assert "unassigned_tool" in text


def test_tool_spec_without_family_does_not_pollute_known_families() -> None:
    # An untyped tool should not accidentally land in the shopping block.
    spec_no_family = ToolSpec(  # type: ignore[arg-type]
        name="unassigned_tool",
        description="No family declared.",
        effect="read",
        read_domains=["shopping"],
        write_domains=[],
        input_model=_OneRequiredInput,
        output_model=_OkOutput,
    )
    spec_known = _spec("list_shopping", "shopping")
    text = render_registry_for_planner([spec_no_family, spec_known])
    # In the shopping block — only the known spec.
    shopping_block_start = text.find("ГРУППА: ПОКУПКИ")
    unfamilied_block_start = text.find("ГРУППА: НЕОТНЕСЁННЫЕ")
    shopping_block = text[shopping_block_start:unfamilied_block_start]
    assert "list_shopping(" in shopping_block
    assert "unassigned_tool" not in shopping_block
