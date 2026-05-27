"""Tests for ``services/tool_schemas/registry_quality.py`` — production
quality policy linter (Sub-A-77 item #6 R1 redesign).

Separates SCHEMA-level checks (covered by ``test_tool_schemas_base.py``,
applied at ToolSpec construction) from POLICY-level checks (this file,
applied via ``validate_tool_registry_quality``).

Policy bounds:
- description ≥ 60 chars, starts with Russian infinitive verb
- trigger_examples count ∈ [3, 10], each ≤ 120 chars, each has Cyrillic
- mutex_notes count ∈ [0, 3], each ≤ 200 chars
- family populated (no None)
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from sreda.services.tool_schemas.base import ToolOutput, ToolSpec
from sreda.services.tool_schemas.registry_quality import (
    DESCRIPTION_MIN_CHARS,
    InvalidRegistryError,
    MUTEX_NOTE_MAX_CHARS,
    MUTEX_NOTES_MAX,
    RegistryQualityViolation,
    TRIGGER_EXAMPLES_MAX,
    TRIGGER_EXAMPLES_MIN,
    TRIGGER_EXAMPLE_MAX_CHARS,
    validate_tool_registry_quality,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _InOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class _OutOk(ToolOutput):
    status: Literal["ok"] = "ok"


def _make_spec(**overrides) -> ToolSpec:
    """Build a production-shape ToolSpec for quality tests (defaults
    pass the strict linter)."""
    base = dict(
        name="add_shopping_items",
        description=(
            "Добавить продукты в список покупок. Используй для запросов "
            "вида «купи X», «добавь Y в покупки»."
        ),
        family="shopping",
        effect="write",
        read_domains=[],
        write_domains=["shopping"],
        input_model=_InOk,
        output_model=_OutOk,
        trigger_examples=[
            "купи молоко и хлеб",
            "добавь яйца в покупки",
            "нужно купить картошки",
        ],
    )
    base.update(overrides)
    return ToolSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_spec_passes_strict_lint() -> None:
    spec = _make_spec()
    violations = validate_tool_registry_quality([spec], strict=True)
    assert violations == []


def test_clean_spec_with_one_mutex_note_passes() -> None:
    spec = _make_spec(mutex_notes=[
        "Используй ТОЛЬКО для добавления. Для удаления — remove_shopping_items.",
    ])
    assert validate_tool_registry_quality([spec], strict=True) == []


# ---------------------------------------------------------------------------
# description: length floor + Russian infinitive start
# ---------------------------------------------------------------------------


def test_description_too_short_strict() -> None:
    spec = _make_spec(description="Добавить.")  # < 60 chars
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "description_too_short" for v in violations)


def test_description_too_short_passes_non_strict() -> None:
    spec = _make_spec(description="Добавить.")
    violations = validate_tool_registry_quality([spec], strict=False)
    # Non-strict skips length floor (and infinitive check).
    assert not any(v.code == "description_too_short" for v in violations)


@pytest.mark.parametrize("bad_desc", [
    (
        "Этот инструмент добавляет продукты в список покупок. Используй "
        "для запросов «купи X», «добавь Y»."
    ),
    (
        "Adds shopping items to the user's list. Triggered by phrases "
        "like 'buy X', 'add Y'."
    ),
    (
        "Команда для добавления продуктов. Используется для запросов "
        "«купи X», «добавь Y»."
    ),
])
def test_description_not_infinitive_strict(bad_desc: str) -> None:
    spec = _make_spec(description=bad_desc)
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "description_not_infinitive" for v in violations)


@pytest.mark.parametrize("verb", [
    "Добавить", "Найти", "Отметить", "Удалить", "Создать",
    "Записать", "Показать", "Изменить", "Беречь", "Сохранить",
])
def test_common_action_verbs_pass_infinitive_check(verb: str) -> None:
    desc = (
        f"{verb} некий объект в системе хозяйства. Используй для запросов "
        f"«X», «Y», «Z», когда юзер хочет это действие."
    )
    spec = _make_spec(description=desc)
    violations = validate_tool_registry_quality([spec], strict=True)
    assert not any(v.code == "description_not_infinitive" for v in violations)


# ---------------------------------------------------------------------------
# trigger_examples: count bounds + length cap + Cyrillic
# ---------------------------------------------------------------------------


def test_too_few_trigger_examples_strict() -> None:
    spec = _make_spec(trigger_examples=["купи молоко"])
    # ToolSpec construction allows single example (schema-level OK).
    # Policy rejects when <3.
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "too_few_trigger_examples" for v in violations)


def test_too_few_trigger_examples_zero() -> None:
    spec = _make_spec(trigger_examples=[])
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "too_few_trigger_examples" for v in violations)


def test_too_many_trigger_examples() -> None:
    examples = [f"пример номер {i}" for i in range(TRIGGER_EXAMPLES_MAX + 1)]
    spec = _make_spec(trigger_examples=examples)
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "too_many_trigger_examples" for v in violations)


def test_trigger_example_too_long() -> None:
    # Build a >120-char string with no trailing whitespace (the
    # construction edge-whitespace check would reject otherwise).
    long_example = "купи " + ("молока" * 50)  # no trailing space
    spec = _make_spec(trigger_examples=[
        "купи молоко", "добавь хлеб", long_example,
    ])
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "trigger_example_too_long" for v in violations)


def test_trigger_example_no_cyrillic_strict() -> None:
    spec = _make_spec(trigger_examples=[
        "купи молоко", "buy milk", "нужно купить",
    ])
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "trigger_example_not_russian" for v in violations)


def test_trigger_example_no_cyrillic_passes_non_strict() -> None:
    spec = _make_spec(trigger_examples=[
        "купи молоко", "buy milk", "нужно купить",
    ])
    violations = validate_tool_registry_quality([spec], strict=False)
    assert not any(
        v.code == "trigger_example_not_russian" for v in violations
    )


# ---------------------------------------------------------------------------
# mutex_notes: count cap + length cap
# ---------------------------------------------------------------------------


def test_too_many_mutex_notes() -> None:
    notes = [f"Используй для X{i} а не Y." for i in range(MUTEX_NOTES_MAX + 1)]
    spec = _make_spec(mutex_notes=notes)
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "too_many_mutex_notes" for v in violations)


def test_mutex_note_too_long() -> None:
    long_note = "Используй " + ("ТОЛЬКО" * 50)  # > 200 chars, no trailing ws
    spec = _make_spec(mutex_notes=[long_note])
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "mutex_note_too_long" for v in violations)


def test_zero_mutex_notes_is_fine() -> None:
    spec = _make_spec(mutex_notes=[])
    violations = validate_tool_registry_quality([spec], strict=True)
    assert not any("mutex" in v.code for v in violations)


# ---------------------------------------------------------------------------
# family: required in strict mode
# ---------------------------------------------------------------------------


def test_missing_family_strict() -> None:
    spec = _make_spec(family=None)
    violations = validate_tool_registry_quality([spec], strict=True)
    assert any(v.code == "missing_family" for v in violations)


def test_missing_family_passes_non_strict() -> None:
    spec = _make_spec(family=None)
    violations = validate_tool_registry_quality([spec], strict=False)
    assert not any(v.code == "missing_family" for v in violations)


# ---------------------------------------------------------------------------
# raise_on_error + multi-spec aggregation
# ---------------------------------------------------------------------------


def test_raise_on_error_raises_with_violations_list() -> None:
    spec = _make_spec(trigger_examples=["купи"])
    with pytest.raises(InvalidRegistryError) as exc_info:
        validate_tool_registry_quality(
            [spec], strict=True, raise_on_error=True
        )
    assert len(exc_info.value.violations) >= 1
    assert all(
        isinstance(v, RegistryQualityViolation)
        for v in exc_info.value.violations
    )


def test_raise_on_error_silent_on_clean_registry() -> None:
    spec = _make_spec()
    validate_tool_registry_quality(
        [spec], strict=True, raise_on_error=True
    )  # no exception


def test_aggregates_violations_across_specs() -> None:
    bad_spec_1 = _make_spec(trigger_examples=["only one"])
    bad_spec_2 = _make_spec(
        name="other_tool",
        description="Bad.",  # too short
    )
    violations = validate_tool_registry_quality(
        [bad_spec_1, bad_spec_2], strict=True
    )
    tool_names = {v.tool_name for v in violations}
    assert "add_shopping_items" in tool_names
    assert "other_tool" in tool_names


# ---------------------------------------------------------------------------
# Bounds module export
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #3 — defensive re-check catches model_copy(update=...) bypass
# ---------------------------------------------------------------------------


def test_model_copy_update_bypass_is_caught_by_linter() -> None:
    """``model_copy(update=...)`` skips pydantic validators — the
    linter's re-check is the last line of defense.

    The re-check reconstructs via ``ToolSpec.model_validate(spec.model_dump())``
    so violation field_path / message depends on whether the invariant
    is a Field-level check (loc=field_name) or a model_validator-style
    check (loc=() and the field name appears in message text)."""
    clean_spec = _make_spec()
    # Inject bad name via model_copy — caught by name pattern in
    # _validate_invariants (model_validator), so loc is empty and the
    # field name appears in the message.
    poisoned = clean_spec.model_copy(update={"name": "Bad Name With Space"})
    violations = validate_tool_registry_quality([poisoned], strict=False)
    assert any(
        v.code == "schema_safety_violation"
        and ("name" in (v.field_path or "") or "name" in v.message.lower())
        for v in violations
    )


def test_model_copy_update_newline_in_description_caught() -> None:
    clean_spec = _make_spec()
    poisoned = clean_spec.model_copy(update={
        "description": "Добавить нечто.\nВторая строка."
    })
    violations = validate_tool_registry_quality([poisoned], strict=False)
    assert any(
        v.code == "schema_safety_violation"
        and "description" in (v.field_path or "") or "description" in v.message.lower()
        for v in violations
    )


def test_model_copy_update_warning_prefix_mutex_note_caught() -> None:
    clean_spec = _make_spec()
    poisoned = clean_spec.model_copy(update={
        "mutex_notes": ["⚠ Already has the marker"]
    })
    violations = validate_tool_registry_quality([poisoned], strict=False)
    # Mutex_notes rejection lives in _validate_invariants (model_validator
    # path), so the violation surfaces in message text rather than
    # field_path. Same for any other guard that's not a Field constraint.
    assert any(
        v.code == "schema_safety_violation"
        and ("mutex_note" in v.message.lower() or "⚠" in v.message)
        for v in violations
    )


def test_assert_production_registry_quality_raises_on_bypass() -> None:
    """The CI acceptance gate (``assert_production_registry_quality``)
    raises ``InvalidRegistryError`` when any spec was constructed via
    bypass."""
    from sreda.services.tool_schemas.registry_quality import (
        assert_production_registry_quality,
    )
    clean_spec = _make_spec()
    poisoned = clean_spec.model_copy(update={"name": ""})
    with pytest.raises(InvalidRegistryError):
        assert_production_registry_quality([poisoned])


def test_assert_production_registry_quality_silent_on_clean_spec() -> None:
    from sreda.services.tool_schemas.registry_quality import (
        assert_production_registry_quality,
    )
    spec = _make_spec()
    assert_production_registry_quality([spec])  # no exception


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #1 — schema-safety violations short-circuit policy
# checks. With the bypass produced by model_copy(update={"description":
# None}) or trigger_examples=[None], policy code would crash on raw
# fields if it ran. Linter must return structured violations + stop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("update,reason", [
    ({"description": None}, "description=None"),
    ({"trigger_examples": [None]}, "trigger_examples=[None]"),
    ({"mutex_notes": [123]}, "mutex_notes=[123]"),
    ({"name": 123}, "name=123"),
    ({"description": ""}, "description=blank"),
])
def test_malformed_bypass_does_not_crash_linter(update, reason) -> None:
    from sreda.services.tool_schemas.registry_quality import (
        assert_production_registry_quality,
    )
    clean_spec = _make_spec()
    poisoned = clean_spec.model_copy(update=update)
    # Linter should return structured violations (not raise raw
    # AttributeError/TypeError). assert_production_registry_quality
    # raises InvalidRegistryError, which IS the structured outcome.
    with pytest.raises(InvalidRegistryError) as exc_info:
        assert_production_registry_quality([poisoned])
    # All violations are RegistryQualityViolation (no leaked exceptions).
    assert all(
        isinstance(v, RegistryQualityViolation)
        for v in exc_info.value.violations
    ), f"Non-violation leaked through for {reason}"
    # At least one violation is the schema_safety_violation code.
    assert any(
        v.code == "schema_safety_violation"
        for v in exc_info.value.violations
    ), f"No schema_safety_violation surfaced for {reason}"


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #2 — domain list items validated at construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_domains", [
    [""],
    ["  "],
    ["valid", ""],
    ["valid", " "],
    [" trailing"],
    ["leading "],
])
def test_blank_or_edge_whitespace_domain_rejected_at_construction(
    bad_domains,
) -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        _make_spec(write_domains=bad_domains)
    msg = str(exc.value).lower()
    assert "domain" in msg or "non-empty" in msg or "whitespace" in msg


@pytest.mark.parametrize("bad_domain", [
    "shopping\n",
    "shop\tping",
    "sho\rping",
])
def test_domain_with_control_chars_rejected_at_construction(
    bad_domain,
) -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        _make_spec(write_domains=[bad_domain])
    msg = str(exc.value).lower()
    assert "newline" in msg or "single-line" in msg or "\\n" in msg


def test_bypass_blank_domain_caught_by_linter() -> None:
    from sreda.services.tool_schemas.registry_quality import (
        assert_production_registry_quality,
    )
    clean = _make_spec()
    poisoned = clean.model_copy(update={"write_domains": ["shopping", ""]})
    with pytest.raises(InvalidRegistryError):
        assert_production_registry_quality([poisoned])


def test_policy_bounds_are_reasonable() -> None:
    assert TRIGGER_EXAMPLES_MIN == 3
    assert TRIGGER_EXAMPLES_MAX == 10
    assert TRIGGER_EXAMPLE_MAX_CHARS == 120
    assert MUTEX_NOTES_MAX == 3
    assert MUTEX_NOTE_MAX_CHARS == 200
    assert DESCRIPTION_MIN_CHARS == 60
