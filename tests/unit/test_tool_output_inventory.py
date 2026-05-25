"""Tests for ``scripts/tool_output_inventory.py`` (Sub-A3, Epic #74).

We test the pure-Python pieces (classifier, message extraction,
matrix building, markdown rendering) against synthetic JSONL data.
The script reads real LLM traces on the VDS — that side is
inspected manually, the tests here lock the parsing/grouping
behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load the script as a module (it's under ``scripts/`` which isn't on
# the package path). The standalone-script style is intentional —
# this is a one-off operator tool, not part of the runtime package.
# The module MUST be registered in sys.modules before exec_module
# because @dataclass looks up cls.__module__ at decoration time.
_INVENTORY_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "tool_output_inventory.py"
)
spec = importlib.util.spec_from_file_location(
    "tool_output_inventory", _INVENTORY_PATH
)
assert spec is not None and spec.loader is not None
inventory = importlib.util.module_from_spec(spec)
sys.modules["tool_output_inventory"] = inventory
spec.loader.exec_module(inventory)


# ---------------------------------------------------------------------------
# classify_prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ok:added:3 items: молоко, хлеб", "ok:added"),
        ("ok:duplicate:2 existed: молоко", "ok:duplicate"),
        ("error:validation_failed: too long", "error:validation_failed"),
        ("skipped:past:reminder at 2020-01-01", "skipped:past"),
        ("ok:scheduled:rem_xyz:2026-05-25T18:00:00+00:00", "ok:scheduled"),
    ],
)
def test_classify_prefix_two_segment_outputs(raw: str, expected: str) -> None:
    assert inventory.classify_prefix(raw) == expected


def test_classify_prefix_single_colon_keeps_after_segment() -> None:
    """Single ``:`` (no further colons) keeps the after-segment as detail.

    Useful for variants like ``error: cannot parse X='Y'`` where the
    detail varies per call — they group separately, but the human
    reviewer can see they're variants of the same `error:` family.
    """
    assert (
        inventory.classify_prefix("error: cannot parse trigger_iso='завтра'")
        == "error:cannot parse trigger_iso='завтра'"
    )


def test_classify_prefix_empty_returns_marker() -> None:
    assert inventory.classify_prefix("") == "<empty>"
    assert inventory.classify_prefix("   ") == "<empty>"


def test_classify_prefix_plain_text_returns_first_50_chars() -> None:
    plain = "This is just a plain freeform reply from the tool"
    assert inventory.classify_prefix(plain) == plain[:50]


def test_classify_prefix_long_freeform_truncated() -> None:
    long = "x" * 200
    result = inventory.classify_prefix(long)
    assert len(result) == 50


# ---------------------------------------------------------------------------
# iter_tool_messages
# ---------------------------------------------------------------------------


def _make_envelope(messages: list[dict]) -> dict:
    return {"kind": "request", "messages": messages}


def test_iter_tool_messages_picks_up_tool_outputs() -> None:
    envelope = _make_envelope(
        [
            {"type": "HumanMessage", "content": "купи молоко"},
            {
                "type": "AIMessage",
                "content": "",
                "tool_calls": [
                    {"name": "add_shopping_items", "args": {"items": ["молоко"]}}
                ],
            },
            {
                "type": "ToolMessage",
                "name": "add_shopping_items",
                "content": "ok:added:1 items: молоко",
                "tool_call_id": "call_1",
            },
        ]
    )
    records = list(inventory.iter_tool_messages(envelope))
    assert len(records) == 1
    record = records[0]
    assert record.tool_name == "add_shopping_items"
    assert record.raw_output == "ok:added:1 items: молоко"
    assert record.prefix == "ok:added"


def test_iter_tool_messages_skips_other_message_types() -> None:
    envelope = _make_envelope(
        [
            {"type": "HumanMessage", "content": "..."},
            {"type": "SystemMessage", "content": "..."},
            {"type": "AIMessage", "content": "reply"},
        ]
    )
    assert list(inventory.iter_tool_messages(envelope)) == []


def test_iter_tool_messages_handles_multipart_content() -> None:
    envelope = _make_envelope(
        [
            {
                "type": "ToolMessage",
                "name": "get_recipe",
                "tool_call_id": "call_1",
                "content": [
                    {"type": "text", "text": "ok:found:Борщ"},
                    {"type": "text", "text": "Ингредиенты: свёкла, капуста"},
                ],
            }
        ]
    )
    records = list(inventory.iter_tool_messages(envelope))
    assert len(records) == 1
    assert records[0].tool_name == "get_recipe"
    # multipart text segments joined with newline
    assert "ok:found:Борщ" in records[0].raw_output
    assert "свёкла" in records[0].raw_output


def test_iter_tool_messages_skips_unnamed_tool_messages() -> None:
    # Defensive — if a serializer ever drops `name`, we just skip
    envelope = _make_envelope(
        [
            {"type": "ToolMessage", "name": "", "content": "ok:x", "tool_call_id": "c1"},
            {"type": "ToolMessage", "content": "ok:y", "tool_call_id": "c2"},
        ]
    )
    assert list(inventory.iter_tool_messages(envelope)) == []


def test_iter_tool_messages_handles_missing_messages_field() -> None:
    assert list(inventory.iter_tool_messages({"kind": "request"})) == []
    assert list(inventory.iter_tool_messages({})) == []


# ---------------------------------------------------------------------------
# scan_directory + build_matrix
# ---------------------------------------------------------------------------


def _write_trace(path: Path, envelopes: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in envelopes) + "\n",
        encoding="utf-8",
    )


def test_scan_directory_aggregates_across_files(tmp_path: Path) -> None:
    day_a = tmp_path / "2026-05-23"
    day_b = tmp_path / "2026-05-24"
    day_a.mkdir()
    day_b.mkdir()

    _write_trace(
        day_a / "trace_1.jsonl",
        [
            _make_envelope(
                [
                    {"type": "ToolMessage", "name": "add_shopping_items",
                     "content": "ok:added:2 items: молоко, хлеб",
                     "tool_call_id": "c1"},
                ]
            ),
            _make_envelope(
                [
                    {"type": "ToolMessage", "name": "add_shopping_items",
                     "content": "ok:duplicate:1 existed: молоко",
                     "tool_call_id": "c2"},
                ]
            ),
        ],
    )
    _write_trace(
        day_b / "trace_2.jsonl",
        [
            _make_envelope(
                [
                    {"type": "ToolMessage", "name": "schedule_reminder",
                     "content": "ok:scheduled:rem_x:2026-05-25T18:00:00",
                     "tool_call_id": "c3"},
                ]
            ),
        ],
    )
    records = inventory.scan_directory(tmp_path)
    by_tool = {r.tool_name for r in records}
    assert by_tool == {"add_shopping_items", "schedule_reminder"}
    assert len(records) == 3


def test_scan_directory_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inventory.scan_directory(tmp_path / "does_not_exist")


def test_scan_directory_skips_malformed_jsonl(tmp_path: Path) -> None:
    f = tmp_path / "bad.jsonl"
    f.write_text(
        "this is not json\n"
        + json.dumps(_make_envelope(
            [{"type": "ToolMessage", "name": "list_shopping",
              "content": "ok:empty", "tool_call_id": "c1"}]
        ))
        + "\nalso not json\n",
        encoding="utf-8",
    )
    records = inventory.scan_directory(tmp_path)
    assert len(records) == 1
    assert records[0].tool_name == "list_shopping"


# ---------------------------------------------------------------------------
# build_matrix + render_matrix
# ---------------------------------------------------------------------------


def _record(name: str, output: str) -> "inventory._ToolOutputRecord":  # type: ignore[name-defined]
    return inventory._ToolOutputRecord(
        tool_name=name,
        raw_output=output,
        prefix=inventory.classify_prefix(output),
    )


def test_build_matrix_groups_by_tool_and_prefix() -> None:
    records = [
        _record("add_shopping_items", "ok:added:1 items: молоко"),
        _record("add_shopping_items", "ok:added:3 items: хлеб, сыр, яблоки"),
        _record("add_shopping_items", "ok:duplicate:1 existed: молоко"),
        _record("schedule_reminder", "ok:scheduled:rem_1:2026-05-25T18:00"),
    ]
    matrix = inventory.build_matrix(records)
    assert set(matrix.keys()) == {"add_shopping_items", "schedule_reminder"}

    add_rows = matrix["add_shopping_items"]
    # Sorted by count desc — ok:added has 2, ok:duplicate has 1
    assert add_rows[0][0] == "ok:added"
    assert add_rows[0][1] == 2
    assert add_rows[1][0] == "ok:duplicate"
    assert add_rows[1][1] == 1


def test_render_matrix_produces_per_tool_sections() -> None:
    records = [
        _record("add_shopping_items", "ok:added:1 items"),
        _record("add_shopping_items", "ok:added:2 items"),
        _record("schedule_reminder", "ok:scheduled:rem_x"),
    ]
    matrix = inventory.build_matrix(records)
    md = inventory.render_matrix(matrix)
    assert "## Tool: `add_shopping_items` (count=2)" in md
    assert "## Tool: `schedule_reminder` (count=1)" in md
    assert "| Prefix | Count | Sample |" in md
    # add_shopping_items (count=2) should come before schedule_reminder (count=1)
    assert md.index("add_shopping_items") < md.index("schedule_reminder")


def test_render_matrix_empty_handles_gracefully() -> None:
    md = inventory.render_matrix({})
    assert "No tool outputs found" in md


def test_render_matrix_escapes_pipes_and_newlines() -> None:
    records = [_record("weird_tool", "ok:found:row1|row2\nnewline_here")]
    matrix = inventory.build_matrix(records)
    md = inventory.render_matrix(matrix)
    # Pipe inside cell should be escaped
    assert "\\|" in md
    # No literal newline inside table cell (would break markdown)
    lines = md.splitlines()
    sample_lines = [line for line in lines if "weird_tool" not in line and "|" in line]
    # No double-newline orphans in row data
    for line in sample_lines:
        assert "\nnewline_here" not in line
