"""R-32 (2026-05-15): intra-turn dispatch dedup tests.

Root cause prod-evidence: mimo иногда эмитит 3× add_task с identical args
в одном LLM response → ThreadPoolExecutor запускает все три параллельно →
DB duplicates (tg_755682022 «У Никиты аттестация»×3 и т.п.).

Fix: `_dispatch_tool_calls_batch` collapses byte-equal duplicates BEFORE
dispatch, выполняет tool ONCE per unique canonical key, replicate result
для каждого original tool_call_id чтобы preserve LangChain protocol.
Return 4-tuple `(tc_id, name, result_str, is_physical_execution)`.

Tests:
1. Identical calls collapsed (3 input → 1 physical → 3 output).
2. Distinct args preserved (different titles → both dispatched).
3. Order preserved (first occurrence is_physical=True).
4. Args canonicalization order-independent ({a,b} vs {b,a} → same key).
5. Different tool names not collapsed.
6. Empty batch → empty result.
7. **Protocol cardinality**: len(results)==len(originals), все tc_id'ы
   присутствуют, sum(is_physical) == unique_canonical_keys.
8. Args с datetime / None / nested dict — canonical key стабилен.
9. Logging: WARN log fires on dedup (no raw args в log).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from sreda.runtime.handlers import (
    _canonical_tool_call_key,
    _dispatch_tool_calls_batch,
)


# ── _canonical_tool_call_key ────────────────────────────────────────


def test_canonical_key_same_args_same_key() -> None:
    """Same (name, args dict) → same key."""
    tc1 = {"id": "id1", "name": "add_task", "args": {"title": "A", "date": "2026-05-16"}}
    tc2 = {"id": "id2", "name": "add_task", "args": {"title": "A", "date": "2026-05-16"}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_ignores_tool_call_id() -> None:
    """`id` field НЕ участвует в canonical key."""
    tc1 = {"id": "call_X", "name": "add_task", "args": {}}
    tc2 = {"id": "call_Y", "name": "add_task", "args": {}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_args_order_independent() -> None:
    """{a:1, b:2} и {b:2, a:1} produce same key (sort_keys=True)."""
    tc1 = {"id": "x", "name": "T", "args": {"a": 1, "b": 2}}
    tc2 = {"id": "x", "name": "T", "args": {"b": 2, "a": 1}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_different_name_different_key() -> None:
    tc1 = {"id": "x", "name": "add_task", "args": {}}
    tc2 = {"id": "x", "name": "save_recipe", "args": {}}
    assert _canonical_tool_call_key(tc1) != _canonical_tool_call_key(tc2)


def test_canonical_key_different_args_different_key() -> None:
    tc1 = {"id": "x", "name": "T", "args": {"title": "A"}}
    tc2 = {"id": "x", "name": "T", "args": {"title": "B"}}
    assert _canonical_tool_call_key(tc1) != _canonical_tool_call_key(tc2)


def test_canonical_key_none_args_becomes_empty() -> None:
    """args=None handled как {}."""
    tc1 = {"id": "x", "name": "T", "args": None}
    tc2 = {"id": "x", "name": "T", "args": {}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_missing_args_handled() -> None:
    """No 'args' field → treated как {}."""
    tc1 = {"id": "x", "name": "T"}
    tc2 = {"id": "x", "name": "T", "args": {}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_unicode_normalized() -> None:
    """Кириллица → \\uXXXX через ensure_ascii=True. Stable."""
    tc1 = {"id": "x", "name": "T", "args": {"title": "Крой"}}
    tc2 = {"id": "x", "name": "T", "args": {"title": "Крой"}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_datetime_coerced() -> None:
    """datetime в args coerced via default=str — stable."""
    now = datetime(2026, 5, 16, 19, 0, tzinfo=timezone.utc)
    tc1 = {"id": "x", "name": "T", "args": {"when": now}}
    tc2 = {"id": "x", "name": "T", "args": {"when": now}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


def test_canonical_key_nested_dict_recursive_sort() -> None:
    """Nested dict keys also sorted (recursive)."""
    tc1 = {"id": "x", "name": "T", "args": {"a": {"y": 2, "x": 1}}}
    tc2 = {"id": "x", "name": "T", "args": {"a": {"x": 1, "y": 2}}}
    assert _canonical_tool_call_key(tc1) == _canonical_tool_call_key(tc2)


# ── _dispatch_tool_calls_batch — dedup behavior ─────────────────────


def _make_tools_by_name(*tool_names: str) -> dict:
    """Build a tools_by_name dict с mock callables."""
    tools = {}
    for n in tool_names:
        m = MagicMock()
        m.invoke.return_value = f"ok:done:{n}"
        m.name = n
        tools[n] = m
    return tools


def test_dispatch_empty_batch_returns_empty() -> None:
    """Empty list → empty result, no crash."""
    result = _dispatch_tool_calls_batch([], {})
    assert result == []


def test_dispatch_single_call_returns_one_physical() -> None:
    """Single tool_call → 1 result, is_physical=True."""
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "A"}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        result = _dispatch_tool_calls_batch(calls, tools)
    assert len(result) == 1
    tc_id, name, result_str, is_physical = result[0]
    assert tc_id == "id1"
    assert name == "add_task"
    assert is_physical is True


def test_dispatch_identical_calls_collapsed_to_one_execution() -> None:
    """3 identical calls → tool invoked 1 time → 3 results с одинаковым result_str,
    one is_physical=True, two is_physical=False."""
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "A", "date": "2026-05-16"}},
        {"id": "id2", "name": "add_task", "args": {"title": "A", "date": "2026-05-16"}},
        {"id": "id3", "name": "add_task", "args": {"title": "A", "date": "2026-05-16"}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        result = _dispatch_tool_calls_batch(calls, tools)
    # Protocol cardinality: 3 inputs → 3 outputs
    assert len(result) == 3
    # All tc_id'ы preserved в same order
    assert [r[0] for r in result] == ["id1", "id2", "id3"]
    # All same name + result_str (replicated)
    assert all(r[1] == "add_task" for r in result)
    assert len({r[2] for r in result}) == 1  # все result_str одинаковые
    # First → physical, rest → replicated
    assert result[0][3] is True
    assert result[1][3] is False
    assert result[2][3] is False
    # Tool invoked ONCE (not 3 times)
    assert tools["add_task"].invoke.call_count == 1


def test_dispatch_distinct_args_preserved() -> None:
    """add_task(A) + add_task(B) → both dispatched как separate physical."""
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "A"}},
        {"id": "id2", "name": "add_task", "args": {"title": "B"}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        result = _dispatch_tool_calls_batch(calls, tools)
    assert len(result) == 2
    # Both physical
    assert all(r[3] is True for r in result)
    # Tool invoked twice
    assert tools["add_task"].invoke.call_count == 2


def test_dispatch_different_tool_names_not_collapsed() -> None:
    """add_task + save_recipe → both dispatched (different names → different keys)."""
    tools = _make_tools_by_name("add_task", "save_recipe")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "A"}},
        {"id": "id2", "name": "save_recipe", "args": {"title": "A"}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        result = _dispatch_tool_calls_batch(calls, tools)
    assert len(result) == 2
    assert all(r[3] is True for r in result)
    assert tools["add_task"].invoke.call_count == 1
    assert tools["save_recipe"].invoke.call_count == 1


def test_dispatch_protocol_cardinality_invariants() -> None:
    """Core protocol guarantees:
    - len(results) == len(originals)
    - все original tc_id'ы present exactly once
    - sum(is_physical) == unique_canonical_keys
    """
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "a", "name": "add_task", "args": {"title": "X"}},
        {"id": "b", "name": "add_task", "args": {"title": "X"}},  # dup
        {"id": "c", "name": "add_task", "args": {"title": "Y"}},  # distinct
        {"id": "d", "name": "add_task", "args": {"title": "X"}},  # dup of a
        {"id": "e", "name": "add_task", "args": {"title": "Y"}},  # dup of c
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        result = _dispatch_tool_calls_batch(calls, tools)

    # cardinality
    assert len(result) == len(calls) == 5
    # all original tc_id'ы present exactly once
    assert sorted(r[0] for r in result) == sorted(["a", "b", "c", "d", "e"])
    # sum(is_physical) == 2 (X и Y — две unique canonical keys)
    assert sum(1 for r in result if r[3]) == 2
    # First X → physical, first Y → physical
    by_id = {r[0]: r for r in result}
    assert by_id["a"][3] is True   # first X
    assert by_id["b"][3] is False  # dup X
    assert by_id["c"][3] is True   # first Y
    assert by_id["d"][3] is False  # dup X
    assert by_id["e"][3] is False  # dup Y
    # Tool invoked 2 times
    assert tools["add_task"].invoke.call_count == 2


def test_dispatch_canonical_args_order_invariance() -> None:
    """Identical args in different key order → collapsed."""
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"a": 1, "b": 2}},
        {"id": "id2", "name": "add_task", "args": {"b": 2, "a": 1}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        result = _dispatch_tool_calls_batch(calls, tools)
    assert result[0][3] is True
    assert result[1][3] is False
    assert tools["add_task"].invoke.call_count == 1


def test_dispatch_logs_warn_on_dedup(caplog) -> None:
    """When collapse detected → WARN log fires (без raw args)."""
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "secret_name"}},
        {"id": "id2", "name": "add_task", "args": {"title": "secret_name"}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert"):
        with caplog.at_level(logging.WARNING, logger="sreda.runtime.handlers"):
            _dispatch_tool_calls_batch(calls, tools)
    # WARN fires
    assert any(
        "tool_call_dedup_intra_turn" in r.message
        for r in caplog.records
    )
    # Raw args НЕ в log (privacy)
    full_log = "\n".join(r.message for r in caplog.records)
    assert "secret_name" not in full_log


def test_dispatch_admin_alert_fires_on_dedup() -> None:
    """When n_collapsed > 0 → send_admin_alert(INFO) called.

    Boris addition 2026-05-15: admin alert на duplicate LLM tool_calls.
    """
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "A"}},
        {"id": "id2", "name": "add_task", "args": {"title": "A"}},
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert") as mock_alert:
        _dispatch_tool_calls_batch(calls, tools)
    mock_alert.assert_called_once()
    kwargs = mock_alert.call_args.kwargs
    assert kwargs["severity"] == "INFO"
    assert "duplicate" in kwargs["title"].lower()
    assert kwargs["extra_context"]["collapsed_count"] == 1


def test_dispatch_no_admin_alert_when_no_dedup() -> None:
    """No duplicates → no admin alert (only fires on actual collapse)."""
    tools = _make_tools_by_name("add_task")
    calls = [
        {"id": "id1", "name": "add_task", "args": {"title": "A"}},
        {"id": "id2", "name": "add_task", "args": {"title": "B"}},  # distinct
    ]
    with patch("sreda.services.admin_alerts.send_admin_alert") as mock_alert:
        _dispatch_tool_calls_batch(calls, tools)
    mock_alert.assert_not_called()
