"""Textual rendering of the tool registry for the planner LLM prompt.

Sub-A-77 item #1 (Epic #74): emit the registry as Russian text grouped
by family with anti-pattern headers. Goes into the planner system
prompt's cached prefix.

Pipeline (planner-side, Phase B):
  1. Planner builds the prompt at startup: render_registry_for_planner(
       sorted_tool_specs_for_tenant)
  2. Result is concatenated into the system message (Sub-B1).
  3. MiMo caches the prefix because shape is stable per-tenant.
  4. Each tenant chat only sends the trailing user-input delta.

Per-family layout:

    ГРУППА: <RUSSIAN_NAME> (<N> инструментов)
    <purpose>
    ⚠ НЕ ИСПОЛЬЗОВАТЬ:
      • <anti_pattern_1>
      • <anti_pattern_2>
      ...

      <tool_name_1>(<args_summary>) — <description>
      <tool_name_2>(<args_summary>) — <description>
      ...

Families are emitted in the canonical ``FAMILIES`` order. Tools within
a family are emitted in alphabetical order by ``name``, which keeps
prompt cache stable across re-renders (no order skew when tools shift
in the source ``ToolSpec`` definitions).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.families import (
    FAMILIES,
    FAMILY_HEADERS,
    Family,
)


def _pluralize_instruments(n: int) -> str:
    """Russian-grammatical pluralization for «инструмент».

    Examples: 1 → «инструмент», 2 → «инструмента», 5 → «инструментов»,
    11 → «инструментов», 21 → «инструмент», 22 → «инструмента», 25 →
    «инструментов». Russian plurals depend on the last two digits;
    11-14 are always genitive plural regardless of last-digit pattern.
    Codex R1 MINOR #12.
    """

    n_abs = abs(n)
    last_two = n_abs % 100
    if 11 <= last_two <= 14:
        return "инструментов"
    last_digit = n_abs % 10
    if last_digit == 1:
        return "инструмент"
    if 2 <= last_digit <= 4:
        return "инструмента"
    return "инструментов"


def render_family_header(family: Family, tool_count: int) -> str:
    """Render the standalone anti-pattern block for a single family.

    Used in tests as a unit (header without tools) so they can verify
    the warning marker and exclusion list independently of the registry
    composition.

    Returns a multi-line string with NO trailing newline; the renderer
    that combines headers + tools adds spacing between sections.
    """

    header = FAMILY_HEADERS[family]
    plural = _pluralize_instruments(tool_count)
    lines: list[str] = [
        f"ГРУППА: {header.russian_name} ({tool_count} {plural})",
        header.purpose,
        "⚠ НЕ ИСПОЛЬЗОВАТЬ:",
    ]
    for ap in header.anti_patterns:
        # Bullet rendering: explicit Unicode bullet + space, two-space
        # indent. Stable so prompt caches don't drift on render.
        lines.append(f"  • {ap}")
    return "\n".join(lines)


def _format_tool_line(spec: ToolSpec) -> str:
    """One-line summary of a ToolSpec for the family body.

    Format: ``<name>(<short args summary>) — <description first line>``

    Args summary is intentionally short — the planner gets the full
    input schema from the JSON-Schema in the structured-output contract
    (Sub-B1 ``response_format``). This text registry is for fast
    semantic routing ("which family / tool fits this user message?"),
    not exhaustive parameter docs.
    """

    # ``description`` may contain multi-line content; keep only the
    # first line for the registry text to stay compact.
    summary = spec.description.splitlines()[0].strip()
    args_hint = _summarize_input_model(spec)
    return f"  {spec.name}({args_hint}) — {summary}"


def _summarize_input_model(spec: ToolSpec) -> str:
    """Extract a short args hint like ``items, category?`` from the
    input model's required vs optional fields.

    Required fields appear bare, optional fields get a trailing ``?``.
    Empty input models render as ``""`` (so the line reads
    ``name() — description``). We stop at 4 names + ellipsis to keep
    each tool line under ~120 chars.

    Field NAME priority (Codex R1 MINOR #11): pydantic ``alias`` (which
    is what shows up in the JSON Schema the planner LLM consumes) wins
    over the Python attribute name. Without this, the planner sees one
    name in the prompt and a different one in the schema and may build
    invalid args.
    """

    fields = spec.input_model.model_fields
    if not fields:
        return ""

    parts: list[str] = []
    for fname, finfo in fields.items():
        # ``finfo.is_required()`` is the canonical pydantic-v2 way; using
        # the .default sentinel is a fallback for older builds in case.
        try:
            required = finfo.is_required()
        except AttributeError:  # pragma: no cover — pydantic v1 shim
            from pydantic_core import PydanticUndefined  # type: ignore

            required = finfo.default is PydanticUndefined
        # Prefer alias (planner-facing JSON-Schema name) over attribute
        # name; fall back gracefully when no alias is declared.
        display_name = (getattr(finfo, "alias", None) or fname)
        parts.append(display_name if required else f"{display_name}?")
        if len(parts) == 4:
            parts.append("...")
            break
    return ", ".join(parts)


def render_registry_for_planner(specs: Iterable[ToolSpec]) -> str:
    """Render the full tool registry as planner-facing Russian text.

    Empty families are SKIPPED — if a tenant tier has no shopping tools
    (hypothetically), no shopping family block appears. This keeps the
    prompt tight and avoids confusing the planner with "this family has
    0 tools" placeholders.

    Tools without a recognised ``family`` field are collected into a
    final block titled ``ГРУППА: НЕОТНЕСЁННЫЕ`` for visibility — caller
    should never let this list be non-empty in production (a CI test
    in Phase B will enforce this). Failing loud here avoids silently
    dropping tools that miss a family assignment.
    """

    by_family: dict[Family, list[ToolSpec]] = defaultdict(list)
    unfamilied: list[ToolSpec] = []
    for spec in specs:
        family = getattr(spec, "family", None)
        if family in FAMILY_HEADERS:
            by_family[family].append(spec)  # type: ignore[index]
        else:
            unfamilied.append(spec)

    sections: list[str] = []
    for family in FAMILIES:
        family_specs = by_family.get(family, [])
        if not family_specs:
            continue
        # Alphabetical for cache stability.
        family_specs_sorted = sorted(family_specs, key=lambda s: s.name)
        body = [render_family_header(family, len(family_specs_sorted))]
        body.append("")  # blank line between header and tool list
        body.extend(_format_tool_line(s) for s in family_specs_sorted)
        sections.append("\n".join(body))

    if unfamilied:
        # Surface as a separate trailing block — never silently drop.
        block = ["ГРУППА: НЕОТНЕСЁННЫЕ (ОШИБКА КОНФИГА)",
                 "Эти инструменты не имеют family — назначь семью в ToolSpec.",
                 ""]
        for s in sorted(unfamilied, key=lambda s: s.name):
            block.append(_format_tool_line(s))
        sections.append("\n".join(block))

    # Double-newline between family sections — clearer visual separation
    # for the planner LLM than a single newline.
    return "\n\n".join(sections)


__all__ = [
    "render_family_header",
    "render_registry_for_planner",
]
