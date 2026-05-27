"""Tests for ${node.field} variable interpolation in plan args/compose data.

The interpolation engine resolves references at executor visit-time. When
a string is a single reference (``"${s1.title}"``) the resolved value
keeps its original type (dict / list / int / str). When references are
mixed with text (``"Сделано: ${s1.count} штук"``) the result is always a
string. Missing nodes or missing fields raise ``InvalidReferenceError``
so the executor can fail the plan early instead of silently passing
``None`` into a tool.
"""

from __future__ import annotations

import pytest

from sreda.runtime.planner.interpolation import (
    InvalidReferenceError,
    resolve_refs,
)


# ---------------------------------------------------------------------------
# Single-reference resolution (type-preserving)
# ---------------------------------------------------------------------------


def test_resolve_simple_string_ref() -> None:
    state = {"s1": {"title": "борщ"}}
    assert resolve_refs("${s1.title}", state) == "борщ"


def test_resolve_nested_field_ref() -> None:
    state = {"s1": {"recipe": {"title": "борщ", "servings": 4}}}
    assert resolve_refs("${s1.recipe.title}", state) == "борщ"


def test_resolve_deep_nested_field_ref() -> None:
    state = {"s1": {"a": {"b": {"c": {"d": "deep"}}}}}
    assert resolve_refs("${s1.a.b.c.d}", state) == "deep"


def test_resolve_full_ref_preserves_int_type() -> None:
    state = {"s2": {"added_count": 3}}
    result = resolve_refs("${s2.added_count}", state)
    assert result == 3
    assert isinstance(result, int)


def test_resolve_full_ref_preserves_list_type() -> None:
    state = {"s1": {"items": ["молоко", "хлеб"]}}
    result = resolve_refs("${s1.items}", state)
    assert result == ["молоко", "хлеб"]
    assert isinstance(result, list)


def test_resolve_full_ref_preserves_dict_type() -> None:
    state = {"s1": {"recipe": {"title": "x", "servings": 4}}}
    result = resolve_refs("${s1.recipe}", state)
    assert result == {"title": "x", "servings": 4}
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Text + reference interpolation (always returns string)
# ---------------------------------------------------------------------------


def test_resolve_string_with_one_inline_ref() -> None:
    state = {"s1": {"count": 5}}
    assert resolve_refs("Сделано: ${s1.count}", state) == "Сделано: 5"


def test_resolve_string_with_multiple_inline_refs() -> None:
    state = {"s1": {"a": "X"}, "s2": {"b": "Y"}}
    assert resolve_refs("${s1.a} и ${s2.b}", state) == "X и Y"


def test_resolve_string_no_refs_unchanged() -> None:
    state = {"s1": {"x": "value"}}
    assert resolve_refs("plain text", state) == "plain text"


# ---------------------------------------------------------------------------
# Nested structure walking (dict / list)
# ---------------------------------------------------------------------------


def test_resolve_in_dict_args() -> None:
    state = {"s1": {"title": "хлеб", "qty": 2}}
    args = {"name": "${s1.title}", "count": "${s1.qty}"}
    result = resolve_refs(args, state)
    assert result == {"name": "хлеб", "count": 2}


def test_resolve_in_list_args() -> None:
    state = {"s1": {"a": "x"}, "s2": {"b": "y"}}
    args = ["${s1.a}", "${s2.b}", "literal"]
    assert resolve_refs(args, state) == ["x", "y", "literal"]


def test_resolve_in_nested_dict_with_list() -> None:
    state = {"s1": {"items": ["a", "b"]}}
    args = {"shopping": {"new_items": "${s1.items}", "category": "food"}}
    assert resolve_refs(args, state) == {
        "shopping": {"new_items": ["a", "b"], "category": "food"}
    }


# ---------------------------------------------------------------------------
# Scalar passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [42, 3.14, True, False, None])
def test_resolve_scalar_passthrough(value: object) -> None:
    state = {"s1": {"x": "ignored"}}
    assert resolve_refs(value, state) == value


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_resolve_unknown_node_raises() -> None:
    state = {"s1": {"x": 1}}
    with pytest.raises(InvalidReferenceError) as exc:
        resolve_refs("${s_ghost.field}", state)
    msg = str(exc.value)
    assert "s_ghost" in msg


def test_resolve_unknown_field_raises() -> None:
    state = {"s1": {"x": 1}}
    with pytest.raises(InvalidReferenceError) as exc:
        resolve_refs("${s1.nonexistent}", state)
    msg = str(exc.value)
    assert "nonexistent" in msg


def test_resolve_field_on_non_dict_raises() -> None:
    state = {"s1": {"count": 5}}
    # Trying to access .anything on integer 5
    with pytest.raises(InvalidReferenceError):
        resolve_refs("${s1.count.something}", state)


def test_resolve_unknown_node_inside_dict_args_raises() -> None:
    state = {"s1": {"x": 1}}
    args = {"valid": "${s1.x}", "broken": "${s_ghost.y}"}
    with pytest.raises(InvalidReferenceError) as exc:
        resolve_refs(args, state)
    assert "s_ghost" in str(exc.value)


# ---------------------------------------------------------------------------
# Edge cases — empty refs / malformed
# ---------------------------------------------------------------------------


def test_resolve_literal_dollar_brace_without_close_is_left_alone() -> None:
    # ${ without closing } is just literal text — no ref pattern matches
    state = {"s1": {"x": 1}}
    s = "literal ${not.a.ref"
    assert resolve_refs(s, state) == s


def test_resolve_empty_dict_args() -> None:
    state = {"s1": {"x": 1}}
    assert resolve_refs({}, state) == {}


def test_resolve_empty_list_args() -> None:
    state = {"s1": {"x": 1}}
    assert resolve_refs([], state) == []


# ---------------------------------------------------------------------------
# Code-review 2026-05-25 follow-ups
# ---------------------------------------------------------------------------


def test_resolve_bare_node_ref_returns_full_output() -> None:
    """``${s1}`` with no field returns the entire action output dict.

    Intentional behaviour — useful when a compose template wants the
    whole structured result from a step without naming every field.
    Documented here so the regex/loop interaction stays load-bearing
    rather than accidental (per code-reviewer MINOR #6).
    """
    state = {"s1": {"title": "x", "items": ["a", "b"]}}
    result = resolve_refs("${s1}", state)
    assert result == {"title": "x", "items": ["a", "b"]}


def test_resolve_dunder_field_rejected_class() -> None:
    """LLM-hallucinated ``${s1.__class__.__name__}`` must NOT leak Python type.

    Code-reviewer MAJOR #1 — defense-in-depth against planner drift
    accidentally surfacing internal Python structure to user-facing
    compose output.
    """
    state = {"s1": {"x": 1}}
    with pytest.raises(InvalidReferenceError) as exc:
        resolve_refs("${s1.__class__}", state)
    msg = str(exc.value)
    assert "__class__" in msg
    assert "underscore" in msg or "private" in msg or "forbidden" in msg


def test_resolve_dunder_field_rejected_dict() -> None:
    state = {"s1": {"x": 1}}
    with pytest.raises(InvalidReferenceError):
        resolve_refs("${s1.__dict__}", state)


def test_resolve_single_underscore_field_rejected() -> None:
    # Private attribute convention — also blocked
    state = {"s1": {"x": 1}}
    with pytest.raises(InvalidReferenceError):
        resolve_refs("${s1._private}", state)


# ---------------------------------------------------------------------------
# Public ref-introspection helpers (Sub-A-77 item #4 R1 MINOR #10)
# ---------------------------------------------------------------------------


from sreda.runtime.planner.interpolation import (  # noqa: E402
    contains_ref,
    extract_step_id,
    is_full_ref_string,
    iter_refs,
)


@pytest.mark.parametrize("value,expected", [
    ("plain text", False),
    ("${s1.x}", True),
    ("prefix ${s1.x} suffix", True),
    ("$not-a-ref", False),
    ("${123_bad}", False),       # leading digit — regex rejects
    (42, False),
    (None, False),
    ([], False),
    ([1, 2, "a"], False),
    (["a", "${s1.x}"], True),
    ({"k": "v"}, False),
    ({"k": "${s1.x}"}, True),
    ({"k": ["nested", "${s1.y}"]}, True),
    ({"k": {"deep": "${s1.z}"}}, True),
    ((1, "${s1.x}"), True),
])
def test_contains_ref(value: object, expected: bool) -> None:
    assert contains_ref(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("${s1.x}", True),
    ("${s1.x.y}", True),
    ("${s1}", True),
    (" ${s1.x}", False),           # leading whitespace — not full ref
    ("${s1.x} ", False),           # trailing whitespace
    ("prefix${s1.x}", False),
    ("${s1.x}${s2.y}", False),     # two refs
    ("plain", False),
    ("$not-a-ref", False),
    (42, False),
    (None, False),
    (["${s1.x}"], False),          # not a string
])
def test_is_full_ref_string(value: object, expected: bool) -> None:
    assert is_full_ref_string(value) == expected


def test_iter_refs_string() -> None:
    assert list(iter_refs("${s1.x}")) == ["s1.x"]


def test_iter_refs_mixed_string() -> None:
    assert list(iter_refs("a ${s1.x} b ${s2.y}")) == ["s1.x", "s2.y"]


def test_iter_refs_nested_dict() -> None:
    value = {"k": ["${s1.x}", {"deep": "${s2.y.z}"}]}
    paths = list(iter_refs(value))
    assert "s1.x" in paths
    assert "s2.y.z" in paths
    assert len(paths) == 2


def test_iter_refs_no_refs_yields_empty() -> None:
    assert list(iter_refs("plain text")) == []
    assert list(iter_refs([1, 2, 3])) == []
    assert list(iter_refs({"k": "v"})) == []


def test_extract_step_id_simple() -> None:
    assert extract_step_id("s1.field") == "s1"


def test_extract_step_id_deep_path() -> None:
    assert extract_step_id("s1.field.sub.deep") == "s1"


def test_extract_step_id_no_dot() -> None:
    assert extract_step_id("s1") == "s1"


def test_extract_step_id_empty_raises() -> None:
    with pytest.raises(ValueError):
        extract_step_id("")
