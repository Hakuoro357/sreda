"""Unit tests for ReplayWriteRecorder in scripts/replay/mock_tools.py (Phase A, issue #87).

Tests cover:
- ReplayWriteRecorder records writes without side effects.
- The fail-fast guard raises ReplayWriteAttempted on un-mocked write tools.
- WriteRecord fields are correct.
- reset() clears records.
- build_mock_tools_by_name() (existing function) still works.
- build_replay_actual_tools() returns correct historical outputs.

All fixtures use synthetic placeholder data (tenant_tg_<SMOKE>).
No prod data, no real chat ids, no DB, no network.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest

from mock_tools import (
    ReplayWriteAttempted,
    ReplayWriteRecorder,
    WriteRecord,
    build_mock_tools_by_name,
    build_replay_actual_tools,
    build_replay_tools_by_name,
)


# ---------------------------------------------------------------------------
# Minimal write-tool taxonomy for isolated tests (avoids sreda import)
# ---------------------------------------------------------------------------

_TEST_WRITE_DOMAINS: dict[str, list[str]] = {
    "save_core_fact": ["memory"],
    "save_episode": ["memory"],
    "add_shopping_items": ["shopping"],
    "schedule_reminder": ["reminders"],
    "add_task": ["tasks"],
}

_TEST_READ_TOOLS = {"list_shopping", "list_reminders", "recall_memory"}


def _make_recorder() -> ReplayWriteRecorder:
    """Recorder backed by the minimal test taxonomy."""
    return ReplayWriteRecorder(write_tool_domains=_TEST_WRITE_DOMAINS)


# ---------------------------------------------------------------------------
# ReplayWriteRecorder — records writes without side effects
# ---------------------------------------------------------------------------


class TestReplayWriteRecorderRecords:
    def test_invoke_records_write(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()

        tools["save_core_fact"].invoke({"content": "Boris loves chess"})

        records = recorder.get_records()
        assert len(records) == 1
        rec = records[0]
        assert rec.tool_name == "save_core_fact"
        # write_target is still set for backward compatibility
        assert rec.write_target == "memory"
        # write_targets is the new canonical tuple
        assert rec.write_targets == ("memory",)
        assert rec.payload == {"content": "Boris loves chess"}
        # authorized is NOT hardcoded True — set to False by recorder;
        # grading computes authorization from frozen gold expectations.
        assert rec.authorized is False

    def test_ainvoke_records_write(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()

        asyncio.run(
            tools["save_episode"].ainvoke({"summary": "User asked about chess"})
        )

        records = recorder.get_records()
        assert len(records) == 1
        assert records[0].tool_name == "save_episode"
        assert records[0].write_target == "memory"

    def test_multiple_writes_recorded_in_order(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()

        tools["add_shopping_items"].invoke({"items": ["milk", "bread"]})
        tools["schedule_reminder"].invoke({"when": "2026-06-05T10:00:00Z", "text": "test"})

        records = recorder.get_records()
        assert len(records) == 2
        assert records[0].tool_name == "add_shopping_items"
        assert records[0].write_target == "shopping"
        assert records[1].tool_name == "schedule_reminder"
        assert records[1].write_target == "reminders"

    def test_invoke_returns_schema_valid_string(self):
        """The recorder must return a NON-EMPTY raw string (not raise, not None)."""
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()

        result = tools["add_task"].invoke({"title": "test task"})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_no_side_effect_on_second_recorder(self):
        """Two independent recorders do not share state."""
        r1 = _make_recorder()
        r2 = _make_recorder()
        t1 = r1.build_tools_by_name()
        t2 = r2.build_tools_by_name()

        t1["save_core_fact"].invoke({"content": "fact A"})

        assert len(r1.get_records()) == 1
        assert len(r2.get_records()) == 0

    def test_get_records_returns_snapshot(self):
        """get_records() returns a copy — mutating it does not affect the recorder."""
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()
        tools["save_core_fact"].invoke({"content": "fact"})

        snapshot = recorder.get_records()
        snapshot.clear()  # mutate the returned list

        assert len(recorder.get_records()) == 1  # original unchanged

    def test_reset_clears_records(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()
        tools["save_core_fact"].invoke({"content": "fact"})
        assert len(recorder.get_records()) == 1

        recorder.reset()
        assert len(recorder.get_records()) == 0

    def test_has_write_to_true(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()
        tools["save_core_fact"].invoke({})
        assert recorder.has_write_to("memory") is True

    def test_has_write_to_false(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()
        tools["save_core_fact"].invoke({})
        assert recorder.has_write_to("shopping") is False

    def test_get_write_targets(self):
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()
        tools["save_core_fact"].invoke({})
        tools["add_shopping_items"].invoke({})
        assert recorder.get_write_targets() == ["memory", "shopping"]

    def test_empty_args_recorded(self):
        """Args can be an empty dict — recorder must handle that."""
        recorder = _make_recorder()
        tools = recorder.build_tools_by_name()
        tools["save_core_fact"].invoke({})
        rec = recorder.get_records()[0]
        assert rec.payload == {}


# ---------------------------------------------------------------------------
# CRITICAL: fail-fast guard raises ReplayWriteAttempted (plan §16)
# ---------------------------------------------------------------------------


class TestFailFastGuard:
    """The most important safety test: an un-mocked write tool raises.

    This is explicitly required by plan §2 and §16 («write-tool-raise
    fail-fast test»).  If this test fails, the safety guard is broken.
    """

    def test_fail_fast_guard_raises_replay_write_attempted(self):
        recorder = _make_recorder()
        guards = recorder.build_fail_fast_guards()

        with pytest.raises(ReplayWriteAttempted) as exc_info:
            guards["save_core_fact"].invoke({"content": "should not be saved"})

        assert "save_core_fact" in str(exc_info.value)
        assert exc_info.value.tool_name == "save_core_fact"

    def test_fail_fast_guard_raises_on_ainvoke(self):
        recorder = _make_recorder()
        guards = recorder.build_fail_fast_guards()

        with pytest.raises(ReplayWriteAttempted):
            asyncio.run(guards["add_shopping_items"].ainvoke({}))

    def test_fail_fast_guard_error_message_mentions_tool_name(self):
        recorder = _make_recorder()
        guards = recorder.build_fail_fast_guards()

        try:
            guards["schedule_reminder"].invoke({})
            pytest.fail("ReplayWriteAttempted was not raised")
        except ReplayWriteAttempted as exc:
            assert "schedule_reminder" in str(exc)

    def test_replay_write_attempted_is_runtime_error(self):
        """ReplayWriteAttempted must be a RuntimeError subclass."""
        exc = ReplayWriteAttempted("some_tool")
        assert isinstance(exc, RuntimeError)
        assert exc.tool_name == "some_tool"


# ---------------------------------------------------------------------------
# WriteRecord
# ---------------------------------------------------------------------------


class TestWriteRecord:
    def test_write_record_fields(self):
        rec = WriteRecord(
            tool_name="save_core_fact",
            write_targets=("memory",),
            payload={"content": "test"},
        )
        assert rec.tool_name == "save_core_fact"
        assert rec.write_target == "memory"   # backward-compat primary
        assert rec.write_targets == ("memory",)
        assert rec.payload == {"content": "test"}
        assert rec.authorized is False  # default — computed from gold, not hardcoded

    def test_write_record_payload_is_a_copy(self):
        """Mutation of the original payload dict must not affect the record."""
        original = {"content": "test"}
        rec = WriteRecord(
            tool_name="save_core_fact",
            write_targets=("memory",),
            payload=original,
        )
        original["content"] = "mutated"
        assert rec.payload["content"] == "test"

    def test_write_record_repr(self):
        rec = WriteRecord(
            tool_name="add_shopping_items",
            write_targets=("shopping",),
            payload={"items": ["milk"]},
        )
        r = repr(rec)
        assert "add_shopping_items" in r
        assert "shopping" in r

    def test_write_record_multi_domain(self):
        """Multi-domain write_targets are stored correctly."""
        rec = WriteRecord(
            tool_name="attach_reminder",
            write_targets=("tasks", "reminders"),
            payload={"task_id": "t1", "when": "10:00"},
        )
        assert rec.write_targets == ("tasks", "reminders")
        assert rec.write_target == "tasks"   # primary = first

    def test_write_record_authorized_explicit_true(self):
        """Callers can set authorized=True explicitly (e.g. for test fixtures)."""
        rec = WriteRecord(
            tool_name="save_core_fact",
            write_targets=("memory",),
            payload={"content": "x"},
            authorized=True,
        )
        assert rec.authorized is True


# ---------------------------------------------------------------------------
# build_mock_tools_by_name — existing function must still work
# ---------------------------------------------------------------------------


class TestExistingBuildMockTools:
    def test_returns_dict_with_all_55_tools(self):
        tools = build_mock_tools_by_name()
        # Verify some key tools are present.
        assert "save_core_fact" in tools
        assert "add_shopping_items" in tools
        assert "list_shopping" in tools
        assert "recall_memory" in tools
        assert "fetch_url" in tools

    def test_tool_invoke_returns_string(self):
        tools = build_mock_tools_by_name()
        result = tools["list_shopping"].invoke({})
        assert isinstance(result, str)

    def test_existing_tools_not_affected_by_recorder(self):
        """Creating a recorder does not modify the existing mock tool dict."""
        tools_before = set(build_mock_tools_by_name().keys())
        _make_recorder()
        tools_after = set(build_mock_tools_by_name().keys())
        assert tools_before == tools_after


# ---------------------------------------------------------------------------
# build_replay_actual_tools
# ---------------------------------------------------------------------------


class TestBuildReplayActualTools:
    def test_returns_tool_with_historical_output(self):
        tools = build_replay_actual_tools(
            step_to_raw={"s1": "ok:added:3"},
            step_to_tool_name={"s1": "add_shopping_items"},
        )
        assert "add_shopping_items" in tools
        result = tools["add_shopping_items"].invoke({})
        assert result == "ok:added:3"

    def test_ainvoke_returns_historical_output(self):
        tools = build_replay_actual_tools(
            step_to_raw={"s1": "no shopping items"},
            step_to_tool_name={"s1": "list_shopping"},
        )
        result = asyncio.run(tools["list_shopping"].ainvoke({}))
        assert result == "no shopping items"

    def test_unknown_step_id_skipped(self):
        tools = build_replay_actual_tools(
            step_to_raw={"s99": "some output"},
            step_to_tool_name={},  # s99 not in the tool-name map
        )
        assert len(tools) == 0

    def test_multiple_steps_different_tools(self):
        tools = build_replay_actual_tools(
            step_to_raw={"s1": "saved_core:mem_001", "s2": "no active reminders"},
            step_to_tool_name={
                "s1": "save_core_fact",
                "s2": "list_reminders",
            },
        )
        assert tools["save_core_fact"].invoke({}) == "saved_core:mem_001"
        assert tools["list_reminders"].invoke({}) == "no active reminders"
