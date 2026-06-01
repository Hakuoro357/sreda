"""Tests for ``runtime/planner/registry_manifest.py`` — PlannerManifest.

Uses REAL ToolSpec instances from MIGRATED_TOOL_SPECS so that any
structural change to the specs immediately surfaces here.

The two canonical fixtures are:
- ``list_shopping``:  effect='read', side_effect_class='read_only'
  → is_durable_write=False
- ``add_shopping_items``: effect='write', side_effect_class='transactional_write',
  request_local=False → is_durable_write=True

All tests reference these by name only; the actual ToolSpec instances are
looked up from MIGRATED_TOOL_SPECS so spec changes propagate automatically.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from sreda.runtime.planner.registry_manifest import (
    PlannerManifest,
    assert_durable_write_adapters,
    build_planner_manifest,
)
from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_BY_NAME: dict[str, ToolSpec] = {s.name: s for s in MIGRATED_TOOL_SPECS}

_ENABLED = {"list_shopping", "add_shopping_items"}


def _build_two() -> PlannerManifest:
    """Convenience: build manifest with the two canonical shopping tools."""
    return build_planner_manifest(
        all_specs=MIGRATED_TOOL_SPECS,
        enabled_names=_ENABLED,
    )


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def test_manifest_specs_contains_exactly_two_tools() -> None:
    """specs tuple contains exactly the two requested tools, no extras."""
    m = _build_two()
    assert m.names == _ENABLED
    assert len(m.specs) == 2


def test_manifest_by_name_maps_both_tools() -> None:
    """by_name maps both enabled tool names to ToolSpec instances."""
    m = _build_two()
    assert set(m.by_name.keys()) == _ENABLED
    assert isinstance(m.by_name["list_shopping"], ToolSpec)
    assert isinstance(m.by_name["add_shopping_items"], ToolSpec)


def test_manifest_durable_write_names_correct() -> None:
    """Only add_shopping_items is a durable write; list_shopping is read-only."""
    m = _build_two()
    assert m.durable_write_names == {"add_shopping_items"}
    # Confirm the property derives from is_durable_write correctly.
    list_spec = m.by_name["list_shopping"]
    add_spec = m.by_name["add_shopping_items"]
    assert list_spec.is_durable_write is False
    assert add_spec.is_durable_write is True


# ---------------------------------------------------------------------------
# Order determinism
# ---------------------------------------------------------------------------


def test_manifest_order_follows_all_specs_not_set_iteration() -> None:
    """specs order mirrors MIGRATED_TOOL_SPECS order regardless of set construction."""
    # Build with names as a set (non-deterministic iteration order).
    m_a = build_planner_manifest(
        all_specs=MIGRATED_TOOL_SPECS,
        enabled_names={"list_shopping", "add_shopping_items"},
    )
    m_b = build_planner_manifest(
        all_specs=MIGRATED_TOOL_SPECS,
        enabled_names={"add_shopping_items", "list_shopping"},
    )
    # Both manifests must have identical spec tuples (same object, same order).
    assert m_a.specs == m_b.specs

    # Verify order matches MIGRATED_TOOL_SPECS order.
    source_order = [s.name for s in MIGRATED_TOOL_SPECS if s.name in _ENABLED]
    manifest_order = [s.name for s in m_a.specs]
    assert manifest_order == source_order


# ---------------------------------------------------------------------------
# by_name identity — SAME ToolSpec objects as in specs
# ---------------------------------------------------------------------------


def test_by_name_returns_same_object_identity_as_specs() -> None:
    """by_name values are identical objects (not copies) to those in specs."""
    m = _build_two()
    for spec in m.specs:
        # 'is' checks object identity, not just equality.
        assert m.by_name[spec.name] is spec


# ---------------------------------------------------------------------------
# Fail-closed: empty enabled_names
# ---------------------------------------------------------------------------


def test_empty_enabled_names_raises_value_error() -> None:
    """Empty enabled_names is a config error — must raise ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        build_planner_manifest(
            all_specs=MIGRATED_TOOL_SPECS,
            enabled_names=set(),
        )


def test_empty_enabled_names_list_also_raises() -> None:
    """Empty list form also raises (not just empty set)."""
    with pytest.raises(ValueError):
        build_planner_manifest(
            all_specs=MIGRATED_TOOL_SPECS,
            enabled_names=[],
        )


# ---------------------------------------------------------------------------
# Fail-closed: unknown name
# ---------------------------------------------------------------------------


def test_unknown_tool_name_raises_value_error_naming_it() -> None:
    """A typo'd name in enabled_names raises ValueError that names the offender."""
    with pytest.raises(ValueError) as exc_info:
        build_planner_manifest(
            all_specs=MIGRATED_TOOL_SPECS,
            enabled_names={"list_shopping", "no_such_tool"},
        )
    assert "no_such_tool" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Fail-closed: duplicate name in all_specs among enabled
# ---------------------------------------------------------------------------


class _DupIn(BaseModel):
    text: str


class _DupOut(BaseModel):
    status: Literal["ok"] = "ok"


def _make_read_spec(name: str) -> ToolSpec:
    """Minimal read ToolSpec for duplicate-name tests; mirrors the
    _minimal_read_spec helper in test_tool_schemas_base.py."""
    return ToolSpec(
        name=name,
        description="Показать тестовые данные",
        family="shopping",
        effect="read",
        read_domains=["shopping"],
        write_domains=[],
        input_model=_DupIn,
        output_model=_DupOut,
        side_effect_class="read_only",
    )


def test_duplicate_name_in_all_specs_raises_value_error() -> None:
    """Two ToolSpecs sharing a name in all_specs raises ValueError (ambiguous by_name)."""
    spec_a = _make_read_spec("list_shopping")
    spec_b = _make_read_spec("list_shopping")  # same name, separate object
    fake_all = [spec_a, spec_b]

    with pytest.raises(ValueError, match="duplicate"):
        build_planner_manifest(
            all_specs=fake_all,
            enabled_names={"list_shopping"},
        )


# ---------------------------------------------------------------------------
# Codex A/B #10b R1: direct construction must fail closed; by_name read-only
# ---------------------------------------------------------------------------


def test_direct_construction_empty_specs_raises() -> None:
    """PlannerManifest(specs=()) bypasses the builder but must still fail
    closed in __post_init__ (an empty manifest is a config error)."""
    with pytest.raises(ValueError, match="non-empty"):
        PlannerManifest(specs=())


def test_direct_construction_duplicate_name_raises() -> None:
    """Direct construction with two same-named specs must raise (the builder
    is not the only fail-closed gate — the type enforces it too)."""
    spec_a = _make_read_spec("dup_tool")
    spec_b = _make_read_spec("dup_tool")
    with pytest.raises(ValueError, match="duplicate"):
        PlannerManifest(specs=(spec_a, spec_b))


def test_direct_construction_coerces_list_to_immutable_tuple() -> None:
    """Codex A/B #10b R2 MAJOR: specs annotated tuple but not runtime-enforced.
    A list passed directly must be coerced to a tuple in __post_init__, so a
    later mutation of the caller's list cannot diverge the manifest."""
    spec_a = _make_read_spec("alpha_tool")
    spec_b = _make_read_spec("beta_tool")
    caller_list = [spec_a, spec_b]

    m = PlannerManifest(specs=caller_list)  # type: ignore[arg-type]  # list on purpose

    assert isinstance(m.specs, tuple)  # coerced
    # Mutating the original list must NOT affect the manifest.
    caller_list.append(_make_read_spec("gamma_tool"))
    assert len(m.specs) == 2
    assert m.names == {"alpha_tool", "beta_tool"}


def test_by_name_is_read_only_mapping_proxy() -> None:
    """by_name must be a read-only view — a runtime caller cannot mutate it
    (add/overwrite/clear) and diverge it from specs/names."""
    m = _build_two()
    with pytest.raises(TypeError):
        m.by_name["x"] = m.by_name["list_shopping"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        m.by_name.clear()  # type: ignore[attr-defined]
    # State intact after the failed mutations.
    assert m.names == _ENABLED


# ---------------------------------------------------------------------------
# #8b-3: fail-closed durable-write ⇒ idempotency adapter invariant
# ---------------------------------------------------------------------------


def test_build_allows_adapted_durable_write_tool() -> None:
    """A manifest whose only durable-write tool (add_shopping_items) HAS an
    idempotency adapter builds successfully — the #8b-3 gate passes."""
    m = _build_two()  # {list_shopping (read), add_shopping_items (adapted write)}
    assert m.durable_write_names == {"add_shopping_items"}
    assert m.by_name["add_shopping_items"].has_idempotency_adapter is True
    # Explicit gate call is a no-op (does not raise) for an all-adapted manifest.
    assert assert_durable_write_adapters(m) is None


def test_build_blocks_unadapted_durable_write_tool() -> None:
    """A durable-write tool WITHOUT an idempotency adapter (schedule_reminder)
    must make build_planner_manifest fail closed — the #8b-3 invariant raises
    at registry build, naming the offender."""
    # Sanity: the tool is durable-write and currently has NO adapter.
    assert _ALL_BY_NAME["schedule_reminder"].is_durable_write is True
    assert _ALL_BY_NAME["schedule_reminder"].has_idempotency_adapter is False

    with pytest.raises(ValueError, match="schedule_reminder") as exc_info:
        build_planner_manifest(
            all_specs=MIGRATED_TOOL_SPECS,
            enabled_names={"list_shopping", "schedule_reminder"},
        )
    # Message mentions the adapter invariant, not just the name.
    assert "adapter" in str(exc_info.value).lower()


def test_build_blocks_menu_option_b_tool_until_adapter_lands() -> None:
    """Menu / delete-recreate tools (option-(b)) stay fail-closed-blocked from
    the planner manifest until their adapter lands (plan §PR-2a.1)."""
    assert _ALL_BY_NAME["plan_week_menu"].is_durable_write is True
    assert _ALL_BY_NAME["plan_week_menu"].has_idempotency_adapter is False
    with pytest.raises(ValueError, match="adapter"):
        build_planner_manifest(
            all_specs=MIGRATED_TOOL_SPECS,
            enabled_names={"list_shopping", "plan_week_menu"},
        )


def test_build_error_lists_all_unadapted_offenders_sorted() -> None:
    """When several durable-write tools lack adapters, the error names ALL of
    them (sorted) so the misconfiguration surfaces at once."""
    with pytest.raises(ValueError) as exc_info:
        build_planner_manifest(
            all_specs=MIGRATED_TOOL_SPECS,
            enabled_names={"add_shopping_items", "schedule_reminder", "save_recipe"},
        )
    msg = str(exc_info.value)
    # The offender list is the exact sorted repr of the unadapted durable writes.
    assert "['save_recipe', 'schedule_reminder']" in msg, (
        f"expected exact sorted offender list in message, got: {msg}"
    )
    # add_shopping_items IS adapted → it must NOT be flagged as an offender
    # (Codex A/B #8b-3 R1, both MINOR: assert absence explicitly, not just order).
    assert "add_shopping_items" not in msg, (
        "adapted tool add_shopping_items must not appear in the offender list"
    )


def test_read_only_manifest_passes_gate() -> None:
    """A manifest with only read tools has no durable writes → gate is a no-op."""
    m = build_planner_manifest(
        all_specs=MIGRATED_TOOL_SPECS,
        enabled_names={"list_shopping"},
    )
    assert m.durable_write_names == frozenset()
    assert assert_durable_write_adapters(m) is None


def test_standalone_gate_raises_on_directly_constructed_unadapted_manifest() -> None:
    """The standalone reader raises on a manifest constructed directly (bypassing
    build) that contains an unadapted durable-write tool. Direct construction is
    intentionally NOT adapter-gated (it's for testing manifest mechanics); the
    explicit gate is how a manually-assembled manifest is validated."""
    m = PlannerManifest(
        specs=(_ALL_BY_NAME["list_shopping"], _ALL_BY_NAME["schedule_reminder"]),
    )
    # Direct construction succeeded (no adapter gate in __post_init__).
    assert "schedule_reminder" in m.names
    with pytest.raises(ValueError, match="adapter"):
        assert_durable_write_adapters(m)
