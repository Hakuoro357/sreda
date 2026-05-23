"""R3-R5 (M-R2-2, M-R3-3): _msg_to_jsonable / _msg_from_jsonable round-trip.

Plan: plans/mellow-discovering-conway-final.md — Section 8.
Issue: #68.

Round-trip contract: `_msg_from_jsonable(_msg_to_jsonable(m)) ≈ m`
structurally for all 4 BaseMessage subclasses. Preserves all base fields
(id, name, additional_kwargs, response_metadata, content) + type-specific:
- SystemMessage: multi-part content with cache_control blocks (R-29)
- HumanMessage: simple content
- AIMessage: tool_calls, invalid_tool_calls, additional_kwargs (reasoning_content)
- ToolMessage: tool_call_id, status, artifact
"""
from __future__ import annotations

import json

from langchain_core.messages import (
    AIMessage, HumanMessage, SystemMessage, ToolMessage,
)

from sreda.services.llm_trace import _msg_to_jsonable, _msg_from_jsonable


def test_system_message_simple_content_round_trip():
    msg = SystemMessage(content="ты — помощница")
    d = _msg_to_jsonable(msg)
    assert d["type"] == "SystemMessage"
    assert d["content"] == "ты — помощница"
    restored = _msg_from_jsonable(d)
    assert isinstance(restored, SystemMessage)
    assert restored.content == msg.content


def test_system_message_multipart_cache_control_round_trip():
    """R-29 R5: multi-part SystemMessage с cache_control должна survive."""
    content = [
        {"type": "text", "text": "stable prompt body", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "variable tail"},
    ]
    msg = SystemMessage(content=content)
    d = _msg_to_jsonable(msg)
    restored = _msg_from_jsonable(d)
    assert restored.content == content
    # cache_control block survives byte-for-byte
    assert restored.content[0]["cache_control"] == {"type": "ephemeral"}


def test_human_message_round_trip():
    msg = HumanMessage(content="привет", name="user", id="m1")
    d = _msg_to_jsonable(msg)
    assert d["type"] == "HumanMessage"
    assert d["name"] == "user"
    assert d["id"] == "m1"
    restored = _msg_from_jsonable(d)
    assert isinstance(restored, HumanMessage)
    assert restored.content == "привет"


def test_ai_message_with_tool_calls_round_trip():
    msg = AIMessage(
        content="вызываю tool",
        tool_calls=[{"id": "call_1", "name": "save_recipe",
                     "args": {"title": "Борщ"}, "type": "tool_call"}],
        additional_kwargs={"reasoning_content": "thinking step"},
        response_metadata={"model_name": "mimo-v2-flash"},
        id="ai1",
    )
    d = _msg_to_jsonable(msg)
    assert d["type"] == "AIMessage"
    assert d["tool_calls"][0]["name"] == "save_recipe"
    assert d["additional_kwargs"]["reasoning_content"] == "thinking step"
    assert d["response_metadata"]["model_name"] == "mimo-v2-flash"
    restored = _msg_from_jsonable(d)
    assert isinstance(restored, AIMessage)
    assert restored.tool_calls[0]["name"] == "save_recipe"
    assert restored.additional_kwargs.get("reasoning_content") == "thinking step"


def test_ai_message_invalid_tool_calls_preserved():
    msg = AIMessage(
        content="",
        tool_calls=[],
        invalid_tool_calls=[{
            "name": "broken_tool", "args": "{not-json",
            "id": "call_bad", "error": "json parse fail",
            "type": "invalid_tool_call",
        }],
    )
    d = _msg_to_jsonable(msg)
    assert d["invalid_tool_calls"][0]["name"] == "broken_tool"
    restored = _msg_from_jsonable(d)
    assert restored.invalid_tool_calls[0]["name"] == "broken_tool"


def test_tool_message_round_trip():
    msg = ToolMessage(
        content="ok:saved:rec_xxx",
        tool_call_id="call_1",
        status="success",
        name="save_recipe",
    )
    d = _msg_to_jsonable(msg)
    assert d["type"] == "ToolMessage"
    assert d["tool_call_id"] == "call_1"
    assert d["status"] == "success"
    restored = _msg_from_jsonable(d)
    assert isinstance(restored, ToolMessage)
    assert restored.tool_call_id == "call_1"
    assert restored.content == "ok:saved:rec_xxx"


def test_all_serialized_dicts_json_safe():
    """Every _msg_to_jsonable result MUST be json.dumps-able."""
    msgs = [
        SystemMessage(content=[
            {"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}
        ]),
        HumanMessage(content="привет"),
        AIMessage(content="ответ",
                  tool_calls=[{"id": "1", "name": "x", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="result", tool_call_id="1"),
    ]
    for m in msgs:
        d = _msg_to_jsonable(m)
        json.dumps(d, ensure_ascii=False)  # MUST NOT raise


def test_unknown_type_raises():
    """Unknown message type → ValueError on inverse (defensive)."""
    import pytest
    with pytest.raises(ValueError, match="unknown message type"):
        _msg_from_jsonable({"type": "WeirdMessage", "content": "x"})
