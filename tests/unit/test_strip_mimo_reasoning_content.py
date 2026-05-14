"""R-27 regression: strip mimo's reasoning_content from AIMessage history.

Production data-loss class active 2026-05-12 → 2026-05-14:
mimo-v2.5-pro returns chain-of-thought as `reasoning_content` field in
response. LangChain ChatOpenAI puts non-standard field в
`AIMessage.additional_kwargs["reasoning_content"]`. На iter.1 этот же
ai_msg отправляется обратно в messages history → LangChain serializes
additional_kwargs → mimo's own API rejects 400 «Param Incorrect: The
reasoning_content in the thinking…».

Result: fallback engaged → fallback model emits empty tool_calls + confab
text «Поставила напоминания…» БЕЗ actual tool dispatch in iter.1 → data
loss.

Affected ≥6 tenants since 2026-05-12 в prod logs.

Fix: после каждого invoke, до `messages.append(ai_msg)`, очистить
`ai_msg.additional_kwargs["reasoning_content"]` если присутствует.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from sreda.runtime.handlers import _strip_mimo_reasoning_content


def test_strips_reasoning_content_when_present() -> None:
    """Mimo response with reasoning_content → key removed after strip."""
    ai_msg = AIMessage(
        content="Поставила напоминание",
        additional_kwargs={
            "reasoning_content": "Думаю что user хочет 13:00 MSK = 10:00 UTC...",
            "refusal": None,
        },
    )
    _strip_mimo_reasoning_content(ai_msg)
    assert "reasoning_content" not in ai_msg.additional_kwargs
    # Other keys preserved
    assert "refusal" in ai_msg.additional_kwargs


def test_no_op_when_reasoning_content_absent() -> None:
    """Non-mimo provider response (no reasoning_content) → unchanged."""
    ai_msg = AIMessage(
        content="Hello",
        additional_kwargs={"refusal": None},
    )
    _strip_mimo_reasoning_content(ai_msg)
    assert "refusal" in ai_msg.additional_kwargs
    assert len(ai_msg.additional_kwargs) == 1


def test_handles_message_without_additional_kwargs() -> None:
    """Defensive: AIMessage subclass без additional_kwargs не должен crash.

    Standard AIMessage всегда имеет additional_kwargs={} default.
    Тест проверяет defensive guard на случай custom subclass.
    """
    class _BareAIMessage:
        content = "Test"
        # No additional_kwargs attribute at all

    bare = _BareAIMessage()
    # Should not raise
    _strip_mimo_reasoning_content(bare)  # type: ignore[arg-type]


def test_handles_none_additional_kwargs() -> None:
    """Defensive: additional_kwargs=None edge case."""
    ai_msg = AIMessage(content="Test")
    # Manually break invariant
    ai_msg.additional_kwargs = None  # type: ignore[assignment]
    # Should not raise
    _strip_mimo_reasoning_content(ai_msg)


def test_preserves_tool_calls() -> None:
    """tool_calls must NOT be stripped — only reasoning_content."""
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "schedule_reminder",
                "args": {"title": "Концерт", "trigger_iso": "2026-05-14T10:00:00+00:00"},
                "id": "call_1",
            }
        ],
        additional_kwargs={
            "reasoning_content": "User asked to remind about concert at 13:00 MSK",
        },
    )
    _strip_mimo_reasoning_content(ai_msg)
    assert "reasoning_content" not in ai_msg.additional_kwargs
    # tool_calls preserved
    assert len(ai_msg.tool_calls) == 1
    assert ai_msg.tool_calls[0]["name"] == "schedule_reminder"
    assert ai_msg.tool_calls[0]["args"]["title"] == "Концерт"


def test_multiple_calls_idempotent() -> None:
    """Calling strip multiple times on same message is safe."""
    ai_msg = AIMessage(
        content="x",
        additional_kwargs={"reasoning_content": "first"},
    )
    _strip_mimo_reasoning_content(ai_msg)
    _strip_mimo_reasoning_content(ai_msg)  # second call no-op
    assert "reasoning_content" not in ai_msg.additional_kwargs


def test_strips_from_response_metadata() -> None:
    """Layer 2: response_metadata['reasoning_content'] also cleaned.

    Defensive — обычно response_metadata НЕ serializes back в request,
    но LangChain version drift или custom subclass могут менять
    contract. Чистим на всякий случай.
    """
    ai_msg = AIMessage(
        content="Test",
        additional_kwargs={},
        response_metadata={
            "model_name": "mimo-v2.5-pro",
            "reasoning_content": "some thinking",
            "finish_reason": "stop",
        },
    )
    _strip_mimo_reasoning_content(ai_msg)
    assert "reasoning_content" not in ai_msg.response_metadata
    # Other response_metadata fields preserved
    assert ai_msg.response_metadata.get("model_name") == "mimo-v2.5-pro"
    assert ai_msg.response_metadata.get("finish_reason") == "stop"


def test_strips_direct_attribute() -> None:
    """Layer 3: direct attribute ai_msg.reasoning_content also cleaned.

    Defensive — LangChain version с typed accessor мог set
    ``ai_msg.reasoning_content`` напрямую как Python attribute. Strip
    через delattr.
    """
    class _AIMessageWithReasoning:
        """Mock LangChain AIMessage variant с direct attribute."""
        def __init__(self) -> None:
            self.content = "Test"
            self.additional_kwargs = {}
            self.response_metadata = {}
            self.reasoning_content = "some thinking"

    msg = _AIMessageWithReasoning()
    _strip_mimo_reasoning_content(msg)  # type: ignore[arg-type]
    assert not hasattr(msg, "reasoning_content")


def test_all_layers_together() -> None:
    """Sanity: reasoning_content в ВСЕХ трёх местах одновременно → all cleaned."""
    class _AIMessageMultiLayer:
        def __init__(self) -> None:
            self.content = "Test"
            self.additional_kwargs = {"reasoning_content": "thinking-1"}
            self.response_metadata = {"reasoning_content": "thinking-2"}
            self.reasoning_content = "thinking-3"

    msg = _AIMessageMultiLayer()
    _strip_mimo_reasoning_content(msg)  # type: ignore[arg-type]
    assert "reasoning_content" not in msg.additional_kwargs
    assert "reasoning_content" not in msg.response_metadata
    assert not hasattr(msg, "reasoning_content")
