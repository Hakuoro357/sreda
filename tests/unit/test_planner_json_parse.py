"""Unit tests for the strict planner JSON parser (#113 — envelope fence-strip)."""
from __future__ import annotations

import json

import pytest

from sreda.runtime.planner import json_parse
from sreda.runtime.planner.json_parse import FENCE_METRICS, parse_planner_json


@pytest.fixture(autouse=True)
def _reset_metrics():
    FENCE_METRICS["fence_stripped"] = 0
    yield
    FENCE_METRICS["fence_stripped"] = 0


# --- accepted: plain JSON (no fence, no strip) ------------------------------


def test_plain_json_parses_without_strip():
    assert parse_planner_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}
    assert FENCE_METRICS["fence_stripped"] == 0


def test_plain_json_with_surrounding_whitespace():
    assert parse_planner_json('  \n {"a": 1}\n\t ') == {"a": 1}
    assert FENCE_METRICS["fence_stripped"] == 0


def test_leading_bom_is_stripped():
    assert parse_planner_json('﻿{"a": 1}') == {"a": 1}


# --- accepted: a single clean outer fence -----------------------------------


def test_json_fence_is_stripped():
    assert parse_planner_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert FENCE_METRICS["fence_stripped"] == 1


def test_caps_JSON_fence_is_stripped():
    assert parse_planner_json('```JSON\n{"a": 1}\n```') == {"a": 1}
    assert FENCE_METRICS["fence_stripped"] == 1


def test_bare_fence_is_stripped():
    assert parse_planner_json('```\n{"a": 1}\n```') == {"a": 1}
    assert FENCE_METRICS["fence_stripped"] == 1


def test_fence_with_trailing_newline_and_bom():
    assert parse_planner_json('﻿```json\n{"a": 1}\n```\n') == {"a": 1}
    assert FENCE_METRICS["fence_stripped"] == 1


def test_multiline_plan_in_fence():
    raw = '```json\n{\n  "intent": "x",\n  "actions": []\n}\n```'
    assert parse_planner_json(raw) == {"intent": "x", "actions": []}


# --- rejected: raises JSONDecodeError, no half-strip ------------------------


def test_prose_before_fence_rejected():
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('Вот план: ```json\n{"a": 1}\n```')
    assert FENCE_METRICS["fence_stripped"] == 0


def test_prose_after_fence_rejected():
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('```json\n{"a": 1}\n```\n\nГотово!')
    assert FENCE_METRICS["fence_stripped"] == 0


def test_opening_fence_without_closing_rejected():
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('```json\n{"a": 1}')
    assert FENCE_METRICS["fence_stripped"] == 0


def test_genuinely_malformed_json_rejected():
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('{"a": 1')
    assert FENCE_METRICS["fence_stripped"] == 0


def test_not_a_greedy_first_object_extractor():
    # prose containing a {...} must NOT be salvaged by finding the first object
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('бла бла {"a": 1} бла')
    assert FENCE_METRICS["fence_stripped"] == 0


def test_fenced_but_invalid_inner_json_rejected():
    # strips the fence, then the inner is still bad → raises
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('```json\n{"a": }\n```')


def test_raises_subclass_of_valueerror_for_caller_compat():
    # orchestrator catches json.JSONDecodeError; run_replay catches Exception
    err = None
    try:
        parse_planner_json("not json")
    except json.JSONDecodeError as exc:
        err = exc
    assert err is not None and isinstance(err, ValueError)


def test_module_exposes_metric():
    assert "fence_stripped" in json_parse.FENCE_METRICS


# --- R1 strictness edges (Codex #113 R1, both A/B) --------------------------


def test_inline_fence_without_newline_rejected():
    # a fence-info line MUST be followed by a newline; ```json {...}``` on one
    # line is not a clean fence → rejected (medium MAJOR)
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('```json {"a": 1}```')
    assert FENCE_METRICS["fence_stripped"] == 0


@pytest.mark.parametrize("raw", [
    '```python\n{"a": 1}\n```',
    '```yaml\n{"a": 1}\n```',
    '```js\n{"a": 1}\n```',
])
def test_non_json_language_fence_rejected(raw):
    # only bare ``` or ```json/```JSON allowed — other langs stay visible misses
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json(raw)
    assert FENCE_METRICS["fence_stripped"] == 0


def test_whitespace_before_bom_is_stripped():
    assert parse_planner_json(' ﻿{"a": 1}') == {"a": 1}


def test_crlf_fence_is_stripped():
    assert parse_planner_json('```json\r\n{"a": 1}\r\n```') == {"a": 1}
    assert FENCE_METRICS["fence_stripped"] == 1


def test_double_nested_fence_not_misparsed():
    # a fenced body that is itself a fence must NOT yield a wrong/partial parse
    with pytest.raises(json.JSONDecodeError):
        parse_planner_json('```json\n```\n{"a": 1}\n```\n```')


def test_body_with_triple_backtick_inside_string_ok():
    # ``` inside a JSON string value is fine — anchored/non-greedy takes the
    # OUTER envelope, so the inner backticks stay part of the value
    raw = '```json\n{"a": "see ```code```"}\n```'
    assert parse_planner_json(raw) == {"a": "see ```code```"}
