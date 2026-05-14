"""R-29: tests for MimoChatOpenAI subclass.

Production data-loss class active 2026-05-12 → 2026-05-14: mimo thinking
mode requires `reasoning_content` echoed back. LangChain ChatOpenAI
drops это field. Subclass MimoChatOpenAI extracts на response и injects
обратно в next request.

Tests cover:
  1-3. Extract — three input shapes (attribute / dict / model_extra)
  4. No-op when reasoning_content absent
  5. Inject в request body на assistant messages
  6. No inject когда reasoning_content отсутствует
  7. Preserves non-assistant messages
  8. Subclass compatibility (ChatOpenAI subclass + bind_tools)
  9. Single AIMessage input handling (non-list)
  10. _build_chat_llm integration returns MimoChatOpenAI for mimo providers
  11. LangChain method signature compat (fails loudly on drift)
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from sreda.services.mimo_chat_openai import MimoChatOpenAI


# ─── helpers ──────────────────────────────────────────────────────


def _build_test_llm() -> MimoChatOpenAI:
    """Build MimoChatOpenAI с dummy credentials for tests that don't
    actually call the API."""
    return MimoChatOpenAI(
        base_url="https://test.example.com/v1",
        api_key="test-key-do-not-use",
        model="mimo-v2-pro",
        temperature=0.3,
    )


# ─── (1-3) extract: three response shapes ──────────────────────────


def test_extract_reasoning_content_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 1: response.choices[0].message.reasoning_content (pydantic attr)."""
    llm = _build_test_llm()

    # Build mock response with pydantic-style message (attribute access)
    msg_obj = MagicMock(spec=["content", "role", "tool_calls", "reasoning_content"])
    msg_obj.content = "Reply text"
    msg_obj.role = "assistant"
    msg_obj.tool_calls = None
    msg_obj.reasoning_content = "User asked X, I thought about Y, answer is Z."

    # Mock response that parent's _create_chat_result will use
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=msg_obj)]

    # Stub parent to return single AIMessage generation. monkeypatch.setattr
    # auto-restores after test — no global ChatOpenAI mutation.
    parent_result = ChatResult(
        generations=[
            ChatGeneration(message=AIMessage(content="Reply text"))
        ]
    )
    monkeypatch.setattr(
        ChatOpenAI,
        "_create_chat_result",
        lambda self, r, gi=None: parent_result,
    )

    result = llm._create_chat_result(fake_response)
    ai = result.generations[0].message
    assert ai.additional_kwargs.get("reasoning_content") == (
        "User asked X, I thought about Y, answer is Z."
    )


def test_extract_reasoning_content_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: dict-shaped response with `reasoning_content` key."""
    llm = _build_test_llm()
    fake_response = {
        "choices": [
            {
                "message": {
                    "content": "Reply",
                    "role": "assistant",
                    "reasoning_content": "dict-shape thinking",
                }
            }
        ]
    }

    parent_result = ChatResult(generations=[ChatGeneration(message=AIMessage(content="Reply"))])
    monkeypatch.setattr(
        ChatOpenAI,
        "_create_chat_result",
        lambda self, r, gi=None: parent_result,
    )

    result = llm._create_chat_result(fake_response)
    ai = result.generations[0].message
    assert ai.additional_kwargs.get("reasoning_content") == "dict-shape thinking"


def test_extract_reasoning_content_model_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 3: pydantic v2 model_extra bucket (no direct attribute exposure)."""
    llm = _build_test_llm()

    # Mock message без direct attribute but with model_extra
    msg_obj = MagicMock(spec=["content", "role", "tool_calls", "model_extra"])
    msg_obj.content = "Reply"
    msg_obj.role = "assistant"
    msg_obj.tool_calls = None
    msg_obj.model_extra = {"reasoning_content": "extra-bucket thinking"}

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=msg_obj)]

    parent_result = ChatResult(generations=[ChatGeneration(message=AIMessage(content="Reply"))])
    monkeypatch.setattr(
        ChatOpenAI,
        "_create_chat_result",
        lambda self, r, gi=None: parent_result,
    )

    result = llm._create_chat_result(fake_response)
    ai = result.generations[0].message
    assert ai.additional_kwargs.get("reasoning_content") == "extra-bucket thinking"


# ─── (4) no-op when absent ─────────────────────────────────────────


def test_extract_no_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Response без reasoning_content → AIMessage.additional_kwargs unchanged."""
    llm = _build_test_llm()
    msg_obj = MagicMock(spec=["content", "role", "tool_calls"])
    msg_obj.content = "Привет"
    msg_obj.role = "assistant"
    msg_obj.tool_calls = None
    # No reasoning_content attribute, no model_extra

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=msg_obj)]

    parent_result = ChatResult(generations=[ChatGeneration(message=AIMessage(content="Привет"))])
    monkeypatch.setattr(
        ChatOpenAI,
        "_create_chat_result",
        lambda self, r, gi=None: parent_result,
    )

    result = llm._create_chat_result(fake_response)
    ai = result.generations[0].message
    assert "reasoning_content" not in ai.additional_kwargs


# ─── (5) inject reasoning_content into request payload ────────────


def test_inject_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """AIMessage с reasoning_content → injected в payload assistant message."""
    llm = _build_test_llm()

    ai_msg = AIMessage(
        content="Prior reply",
        additional_kwargs={"reasoning_content": "Stored CoT from iter.0"},
    )
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="user"),
        ai_msg,
    ]

    # Stub parent payload build to return predictable structure
    def fake_super_payload(self: Any, inp: Any, *, stop: Any = None, **kw: Any) -> dict:
        return {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "user"},
                {"role": "assistant", "content": "Prior reply"},
            ],
            "model": "mimo-v2-pro",
        }

    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", fake_super_payload)
    payload = llm._get_request_payload(messages)
    assert payload["messages"][2]["reasoning_content"] == "Stored CoT from iter.0"
    # Other messages untouched
    assert "reasoning_content" not in payload["messages"][0]
    assert "reasoning_content" not in payload["messages"][1]


# ─── (6) no inject without reasoning_content ──────────────────────


def test_inject_no_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """AIMessage без reasoning_content → no field в payload."""
    llm = _build_test_llm()

    ai_msg = AIMessage(content="Prior reply", additional_kwargs={})
    messages = [HumanMessage(content="user"), ai_msg]

    def fake_super_payload(self: Any, inp: Any, *, stop: Any = None, **kw: Any) -> dict:
        return {
            "messages": [
                {"role": "user", "content": "user"},
                {"role": "assistant", "content": "Prior reply"},
            ],
            "model": "mimo-v2-pro",
        }

    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", fake_super_payload)
    payload = llm._get_request_payload(messages)
    assert "reasoning_content" not in payload["messages"][1]


# ─── (7) only assistant messages get reasoning_content ────────────


def test_inject_preserves_other_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """system/human/tool messages не должны получать reasoning_content
    даже если по ошибке кто-то поставил его в их additional_kwargs."""
    llm = _build_test_llm()

    # Simulate weird case: HumanMessage с additional_kwargs (legal но
    # unused). MimoChatOpenAI должен skip — only AIMessage trigger inject.
    human_msg = HumanMessage(content="ask")
    # HumanMessage не allows additional_kwargs to AIMessage in same way,
    # но если subclass или future LangChain — мы не должны inject.
    ai_msg = AIMessage(
        content="reply",
        additional_kwargs={"reasoning_content": "CoT"},
    )
    tool_msg = ToolMessage(content="tool result", tool_call_id="call_1")

    messages = [SystemMessage(content="sys"), human_msg, ai_msg, tool_msg]

    def fake_super_payload(self: Any, inp: Any, *, stop: Any = None, **kw: Any) -> dict:
        return {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "ask"},
                {"role": "assistant", "content": "reply"},
                {"role": "tool", "content": "tool result", "tool_call_id": "call_1"},
            ],
            "model": "mimo-v2-pro",
        }

    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", fake_super_payload)
    payload = llm._get_request_payload(messages)
    # Only assistant message gets reasoning_content
    assert "reasoning_content" not in payload["messages"][0]  # system
    assert "reasoning_content" not in payload["messages"][1]  # user
    assert payload["messages"][2].get("reasoning_content") == "CoT"  # assistant ✓
    assert "reasoning_content" not in payload["messages"][3]  # tool


# ─── (8) subclass compatibility ───────────────────────────────────


def test_subclass_compatibility() -> None:
    """MimoChatOpenAI IS a ChatOpenAI; bind_tools works."""
    llm = _build_test_llm()
    assert isinstance(llm, ChatOpenAI)

    bound = llm.bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )
    # bind_tools returns a Runnable wrapper; just verify the call doesn't crash
    assert bound is not None


# ─── (9) single-message input handling (Xiaomi R1 MINOR) ───────────


def test_inject_single_message_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """input_ как single AIMessage (не list) — covered by isinstance(..., list) else branch."""
    llm = _build_test_llm()

    single_ai = AIMessage(
        content="Reply",
        additional_kwargs={"reasoning_content": "CoT"},
    )

    def fake_super_payload(self: Any, inp: Any, *, stop: Any = None, **kw: Any) -> dict:
        return {
            "messages": [{"role": "assistant", "content": "Reply"}],
            "model": "mimo-v2-pro",
        }

    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", fake_super_payload)
    payload = llm._get_request_payload(single_ai)
    assert payload["messages"][0]["reasoning_content"] == "CoT"


# ─── (10) _build_chat_llm integration (Codex R1 MAJOR) ────────────


def test_build_chat_llm_returns_mimo_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_chat_llm с mimo provider returns MimoChatOpenAI instance.
    OpenRouter providers возвращают plain ChatOpenAI (no subclass)."""
    from sreda.config.settings import Settings
    from sreda.services import llm as llm_module

    s = Settings(
        mimo_api_key="mk",
        mimo_base_url="https://mimo.example/v1",
        mimo_chat_model="mimo-v2-pro",
        openrouter_api_key="or-k",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_chat_model="gemma-4",
    )

    # mimo provider → MimoChatOpenAI
    got_mimo = llm_module._build_chat_llm(  # type: ignore[attr-defined]
        provider="mimo",
        settings=s,
        model=None,
        temperature=0.3,
    )
    assert isinstance(got_mimo, MimoChatOpenAI)

    # mimo-v2.5 → also MimoChatOpenAI
    got_v25 = llm_module._build_chat_llm(  # type: ignore[attr-defined]
        provider="mimo-v2.5",
        settings=s,
        model=None,
        temperature=0.3,
    )
    assert isinstance(got_v25, MimoChatOpenAI)

    # openrouter provider → plain ChatOpenAI (NOT MimoChatOpenAI)
    got_or = llm_module._build_chat_llm(  # type: ignore[attr-defined]
        provider="openrouter",
        settings=s,
        model=None,
        temperature=0.3,
    )
    assert isinstance(got_or, ChatOpenAI)
    assert not isinstance(got_or, MimoChatOpenAI)


# ─── (11) langchain signature compat (Codex R1 MAJOR) ─────────────


def test_langchain_openai_signature_compat() -> None:
    """ChatOpenAI's `_get_request_payload` и `_create_chat_result` signatures
    match what MimoChatOpenAI overrides assume. Fails loudly on SDK drift.

    Stricter contract checks (Codex R2 MINOR) — verify exact parameter
    names + kinds, not just "exists".
    """
    # _get_request_payload(self, input_, *, stop=None, **kwargs)
    sig_payload = inspect.signature(ChatOpenAI._get_request_payload)
    params_payload = sig_payload.parameters
    assert "self" in params_payload, "missing 'self' parameter"
    assert "input_" in params_payload, (
        f"ChatOpenAI._get_request_payload missing 'input_': {sig_payload}"
    )
    assert "stop" in params_payload, (
        f"ChatOpenAI._get_request_payload missing 'stop' (subclass passes it): {sig_payload}"
    )
    # **kwargs (VAR_KEYWORD) обязателен — иначе subclass.super(...**kwargs) ломается
    kinds = [p.kind for p in params_payload.values()]
    assert inspect.Parameter.VAR_KEYWORD in kinds, (
        f"ChatOpenAI._get_request_payload missing **kwargs: {sig_payload}"
    )

    # _create_chat_result(self, response, generation_info=None)
    sig_result = inspect.signature(ChatOpenAI._create_chat_result)
    params_result = sig_result.parameters
    assert "self" in params_result, "missing 'self' parameter"
    assert "response" in params_result, (
        f"ChatOpenAI._create_chat_result missing 'response': {sig_result}"
    )
    assert "generation_info" in params_result, (
        f"ChatOpenAI._create_chat_result missing 'generation_info' (subclass passes it): {sig_result}"
    )
