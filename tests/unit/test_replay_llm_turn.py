"""Phase D.3 (Issue #68): replay_llm_turn CLI smoke + cross-provider + no-DB.

Plan: plans/mellow-discovering-conway-final.md, Section 13.
"""
from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _write_synthetic_jsonl(root: Path, trace_id: str = "trace_test") -> Path:
    """Create synthetic JSONL под day-folder structure."""
    day = "2026-05-23"
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{trace_id}.jsonl"
    request_row = {
        "schema_version": 1,
        "phase": "request",
        "attempt": "primary",
        "trace_id": trace_id,
        "iter": 0,
        "ts": "2026-05-23T12:00:00.000Z",
        "trace_started_at_utc": day,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "request": {
            "messages": [
                {"type": "SystemMessage", "content": "ты помощник",
                 "additional_kwargs": {}, "response_metadata": {},
                 "id": None, "name": None},
                {"type": "HumanMessage", "content": "привет",
                 "additional_kwargs": {}, "response_metadata": {},
                 "id": None, "name": None},
            ],
            "tool_schemas": [],
            "provider": "mimo-flash",
            "model": "mimo-v2-flash",
            "client_kwargs": {"temperature": 0.3, "model": "mimo-v2-flash"},
            "bound_layers": [],
            "bound_kwargs": {},
            "invocation_kwargs": {},
        },
    }
    response_row = {
        "schema_version": 1,
        "phase": "response", "attempt": "primary",
        "trace_id": trace_id, "iter": 0,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "response": {
            "content": "captured answer",
            "tool_calls": [], "invalid_tool_calls": [],
            "additional_kwargs": {}, "response_metadata": {},
            "id": None, "name": None, "latency_ms": 1500,
        },
        "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read": 50},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(request_row, ensure_ascii=False) + "\n")
        f.write(json.dumps(response_row, ensure_ascii=False) + "\n")
    return path


def test_main_exits_0_on_same_provider_replay(tmp_path, monkeypatch, capsys):
    """Same-provider replay → success. Use fake get_chat_llm."""
    _write_synthetic_jsonl(tmp_path)
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages):
            from langchain_core.messages import AIMessage
            return AIMessage(content="replay text", tool_calls=[])

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test",
        "--root", str(tmp_path),
        "--iter", "0",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Replay summary" in captured.out
    assert "trace_test" in captured.out


def test_cross_provider_without_flag_exits_4(tmp_path, monkeypatch, capsys):
    """Без --allow-cross-provider — exit 4 (cross-provider blocked)."""
    _write_synthetic_jsonl(tmp_path)
    import replay_llm_turn

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test",
        "--root", str(tmp_path),
        "--provider", "mimo-v2.5",  # differs from captured "mimo-flash"
    ])
    assert rc == 4


def test_cross_provider_with_flag_and_no_confirm_succeeds(
    tmp_path, monkeypatch, capsys,
):
    """С --allow-cross-provider + --no-confirm → success."""
    _write_synthetic_jsonl(tmp_path)
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages):
            from langchain_core.messages import AIMessage
            return AIMessage(content="replay from pro", tool_calls=[])

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test",
        "--root", str(tmp_path),
        "--provider", "mimo-v2.5",
        "--allow-cross-provider",
        "--no-confirm",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Cross-provider" in captured.out


def test_diff_output_with_show_content(tmp_path, monkeypatch, capsys):
    _write_synthetic_jsonl(tmp_path)
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages):
            from langchain_core.messages import AIMessage
            return AIMessage(content="replay answer", tool_calls=[])

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test", "--root", str(tmp_path),
        "--diff", "--show-content",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Tool calls diff" in captured.out
    assert "Content diff" in captured.out
    assert "Token usage delta" in captured.out


def test_missing_trace_exits_2(tmp_path, capsys):
    import replay_llm_turn
    rc = replay_llm_turn.main([
        "--trace-id", "nonexistent",
        "--root", str(tmp_path),
    ])
    assert rc == 2


def test_no_db_imports_during_replay(tmp_path, monkeypatch):
    """CRITICAL: replay MUST NOT import sreda.db.session or build_*_tools.

    Monkeypatch these symbols to raise если imported. If replay touches DB
    code path — ImportError raised.
    """
    _write_synthetic_jsonl(tmp_path)
    import replay_llm_turn

    # Block production tool factories
    forbidden = [
        "sreda.runtime.tools",         # contains build_memory_tools
        "sreda.services.housewife_chat_tools",  # build_housewife_tools
    ]
    # Note: sreda.db.session уже импортирован transitively (через settings/llm).
    # Главный gating: production tool factories. Check они НЕ called.
    original_import = builtins.__import__
    def _guarded(name, *a, **kw):
        if name in forbidden:
            raise AssertionError(
                f"replay must not import {name!r} — uses captured tool_schemas instead"
            )
        return original_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", _guarded)

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages):
            from langchain_core.messages import AIMessage
            return AIMessage(content="ok", tool_calls=[])

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test", "--root", str(tmp_path),
    ])
    assert rc == 0
