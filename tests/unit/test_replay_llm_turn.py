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


def _write_synthetic_jsonl_with_tool_calls(
    root: Path, trace_id: str = "trace_pii",
) -> Path:
    """Variant of helper that captures a tool_call с PII args в response.
    Used by --diff PII gating regression test."""
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
                {"type": "HumanMessage", "content": "запомни",
                 "additional_kwargs": {}, "response_metadata": {},
                 "id": None, "name": None},
            ],
            "tool_schemas": [],
            "provider": "mimo-flash",
            "model": "mimo-v2-flash",
            "client_kwargs": {"temperature": 0.3},
            "bound_layers": [],
            "bound_kwargs": {},
            "invocation_kwargs": {},
        },
    }
    SENSITIVE_PII = "Иванов Иван 1985-03-15 паспорт 4509 №123456"
    response_row = {
        "schema_version": 1,
        "phase": "response", "attempt": "primary",
        "trace_id": trace_id, "iter": 0,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "response": {
            "content": "сохранила",
            "tool_calls": [
                {"name": "save_memory", "id": "call_x",
                 "args": {"text": SENSITIVE_PII}},
            ],
            "invalid_tool_calls": [],
            "additional_kwargs": {}, "response_metadata": {},
            "id": None, "name": None, "latency_ms": 100,
        },
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read": 0},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(request_row, ensure_ascii=False) + "\n")
        f.write(json.dumps(response_row, ensure_ascii=False) + "\n")
    return path


def test_diff_without_show_content_suppresses_tool_args(
    tmp_path, monkeypatch, capsys,
):
    """Regression: Codex PR-48 MAJOR (replay_llm_turn.py:164-176).
    --diff WITHOUT --show-content must NOT print tool_call args
    (decrypted PII surface)."""
    _write_synthetic_jsonl_with_tool_calls(tmp_path)
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages, **kw):
            from langchain_core.messages import AIMessage
            # Replay generates its own (different) tool call — also sensitive
            return AIMessage(
                content="ok",
                tool_calls=[
                    {"name": "save_memory", "id": "call_y",
                     "args": {"text": "также PII 12345"}},
                ],
            )

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_pii", "--root", str(tmp_path),
        "--diff",  # no --show-content
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # Tool names ARE visible (non-PII metadata)
    assert "save_memory" in captured.out
    # But args MUST NOT leak в stdout
    assert "Иванов Иван" not in captured.out, (
        "PII (captured tool args) leaked в stdout без --show-content; "
        "Codex PR-48 MAJOR finding"
    )
    assert "1985-03-15" not in captured.out
    assert "4509" not in captured.out
    assert "также PII 12345" not in captured.out, (
        "PII (replay tool args) leaked в stdout без --show-content"
    )
    # Explicit suppress message visible
    assert "tool args suppressed" in captured.out
    assert "content diff suppressed" in captured.out


def test_diff_with_show_content_includes_tool_args(
    tmp_path, monkeypatch, capsys,
):
    """Positive control: --diff --show-content печатает PII (opt-in)."""
    _write_synthetic_jsonl_with_tool_calls(tmp_path)
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages, **kw):
            from langchain_core.messages import AIMessage
            return AIMessage(
                content="ok",
                tool_calls=[
                    {"name": "save_memory", "id": "call_y",
                     "args": {"text": "также PII 12345"}},
                ],
            )

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_pii", "--root", str(tmp_path),
        "--diff", "--show-content",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # With opt-in — args ARE printed
    assert "Иванов Иван" in captured.out
    assert "также PII 12345" in captured.out


def test_no_confirm_refused_without_pytest_env(
    tmp_path, monkeypatch, capsys,
):
    """Regression: Codex PR-48 MAJOR (replay_llm_turn.py:224,269-275).
    --no-confirm + cross-provider must REFUSE если PYTEST_CURRENT_TEST
    env not set (production deployment защита)."""
    _write_synthetic_jsonl(tmp_path)
    # Strip pytest's auto-set env to simulate production CLI invocation
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages, **kw):
            from langchain_core.messages import AIMessage
            return AIMessage(content="should not get here", tool_calls=[])

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test", "--root", str(tmp_path),
        "--provider", "mimo-v2.5", "--allow-cross-provider",
        "--no-confirm",
    ])
    assert rc == 8, "Production CLI must refuse --no-confirm bypass"


def test_no_confirm_allowed_under_pytest_env(tmp_path, monkeypatch, capsys):
    """Positive control: pytest auto-sets PYTEST_CURRENT_TEST so the
    flag works in test runs (without needing an interactive operator)."""
    _write_synthetic_jsonl(tmp_path)
    # pytest already sets PYTEST_CURRENT_TEST; ensure it stays set
    assert "PYTEST_CURRENT_TEST" in __import__("os").environ
    import replay_llm_turn

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages, **kw):
            from langchain_core.messages import AIMessage
            return AIMessage(content="cross provider ok", tool_calls=[])

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm",
                        lambda **kw: _FakeLLM())

    rc = replay_llm_turn.main([
        "--trace-id", "trace_test", "--root", str(tmp_path),
        "--provider", "mimo-v2.5", "--allow-cross-provider",
        "--no-confirm",
    ])
    assert rc == 0


def test_replay_passes_captured_kwargs_to_get_chat_llm(
    tmp_path, monkeypatch, capsys,
):
    """Regression: Codex PR-48 MAJOR (replay не передавал top_p/extra_body/
    invocation_kwargs — только temperature). Replay must reconstruct
    LLM с full captured config иначе output diverges from causes
    unrelated to the model itself."""
    day = "2026-05-23"
    folder = tmp_path / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "trace_rich.jsonl"
    request_row = {
        "schema_version": 1,
        "phase": "request", "attempt": "primary",
        "trace_id": "trace_rich", "iter": 0,
        "ts": "2026-05-23T12:00:00.000Z", "trace_started_at_utc": day,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "request": {
            "messages": [
                {"type": "HumanMessage", "content": "hi",
                 "additional_kwargs": {}, "response_metadata": {},
                 "id": None, "name": None},
            ],
            "tool_schemas": [],
            "provider": "mimo-flash", "model": "mimo-v2-flash",
            "client_kwargs": {
                "temperature": 0.7,
                "top_p": 0.85,
                "seed": 42,
                "max_tokens": 4096,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
            "bound_layers": [],
            "bound_kwargs": {},
            # invocation_kwargs — should be forwarded as ainvoke kwargs
            "invocation_kwargs": {"temperature": 0.1},  # overrides
        },
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(request_row, ensure_ascii=False) + "\n")

    import replay_llm_turn
    captured_constructor_kwargs = {}
    captured_invoke_kwargs = {}

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages, **kw):
            captured_invoke_kwargs.update(kw)
            from langchain_core.messages import AIMessage
            return AIMessage(content="ok", tool_calls=[])

    def fake_get_chat_llm(**kw):
        captured_constructor_kwargs.update(kw)
        return _FakeLLM()

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm", fake_get_chat_llm)

    rc = replay_llm_turn.main([
        "--trace-id", "trace_rich", "--root", str(tmp_path),
    ])
    assert rc == 0
    # All client-level kwargs forwarded to get_chat_llm
    assert captured_constructor_kwargs.get("top_p") == 0.85, (
        f"top_p not forwarded: {captured_constructor_kwargs}"
    )
    assert captured_constructor_kwargs.get("seed") == 42
    assert captured_constructor_kwargs.get("max_tokens") == 4096
    # extra_body — provider-specific structured data
    assert captured_constructor_kwargs.get("extra_body") == {
        "thinking": {"type": "disabled"}
    }
    # invocation_kwargs override (LangChain semantics) → temperature=0.1
    # was passed as invocation kwarg in original call
    assert captured_invoke_kwargs.get("temperature") == 0.1, (
        f"invocation_kwargs not forwarded to invoke: {captured_invoke_kwargs}"
    )
    # Constructor still has the base temperature (0.7) merged with the
    # invocation override (0.1) — _merge_call_kwargs picks invocation.
    # We assert на invoke side что invocation wins.


def test_replay_falls_back_when_constructor_rejects_kwargs(
    tmp_path, monkeypatch, capsys,
):
    """get_chat_llm может не принимать какой-то captured kwarg
    (LangChain API change) — replay должен gracefully fall back to
    temperature-only + warn."""
    day = "2026-05-23"
    folder = tmp_path / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "trace_unknown_kw.jsonl"
    request_row = {
        "schema_version": 1,
        "phase": "request", "attempt": "primary",
        "trace_id": "trace_unknown_kw", "iter": 0,
        "ts": "2026-05-23T12:00:00.000Z", "trace_started_at_utc": day,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "request": {
            "messages": [
                {"type": "HumanMessage", "content": "x",
                 "additional_kwargs": {}, "response_metadata": {},
                 "id": None, "name": None},
            ],
            "tool_schemas": [],
            "provider": "mimo-flash", "model": "mimo-v2-flash",
            "client_kwargs": {"temperature": 0.4, "tool_choice": "auto"},
            "bound_layers": [], "bound_kwargs": {}, "invocation_kwargs": {},
        },
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(request_row, ensure_ascii=False) + "\n")

    import replay_llm_turn
    call_count = {"n": 0}

    class _FakeLLM:
        def bind_tools(self, *a, **kw): return self
        def invoke(self, messages, **kw):
            from langchain_core.messages import AIMessage
            return AIMessage(content="ok", tool_calls=[])

    def fake_get_chat_llm(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: simulate API rejection
            raise TypeError("unexpected keyword 'tool_choice'")
        return _FakeLLM()

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm", fake_get_chat_llm)

    rc = replay_llm_turn.main([
        "--trace-id", "trace_unknown_kw", "--root", str(tmp_path),
    ])
    assert rc == 0
    assert call_count["n"] == 2, "Expected fallback to temperature-only call"


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
