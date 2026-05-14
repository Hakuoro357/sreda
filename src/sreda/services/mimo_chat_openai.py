"""ChatOpenAI subclass for mimo thinking-mode reasoning_content preservation (R-29).

Mimo-v2.5-pro thinking mode (default ON) returns `reasoning_content` field
в response message. Standard LangChain `ChatOpenAI` parser ignores
non-standard fields → `reasoning_content` never reaches `AIMessage`.
On subsequent calls с этим AIMessage в history, mimo's API rejects:

    400 «Param Incorrect: The reasoning_content in the thinking mode
         must be passed back to the API.»

This forces `RunnableWithFallbacks` to engage fallback provider, which
composes user-facing text without proper tool dispatch context — causes
data-loss class affecting every tool-use turn (production 2026-05-12 →
2026-05-14, ≥6 tenants affected, see incident `trace_a19f5cc370124aa7`).

Solution: subclass ChatOpenAI и override two stable internal methods:
  - `_create_chat_result` — extract `reasoning_content` from response
    (openai SDK preserves через pydantic v2 `extra="allow"`) → store в
    `AIMessage.additional_kwargs["reasoning_content"]`.
  - `_get_request_payload` — inject `reasoning_content` from
    `AIMessage.additional_kwargs` обратно в request body assistant
    message dict on subsequent calls.

Defensive against LangChain version drift: see
`tests/unit/test_mimo_chat_openai.py::test_langchain_openai_signature_compat`.

Production constraints:
  - Sreda uses `stream=False` exclusively — streaming path not covered.
  - `pyproject.toml` pins `langchain-openai>=1.1,<2.0` matching installed
    1.1.13 на момент implementation (2026-05-14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

if TYPE_CHECKING:
    from langchain_core.outputs import ChatResult
    from langchain_core.prompt_values import PromptValue


class MimoChatOpenAI(ChatOpenAI):
    """ChatOpenAI variant что preserves mimo's `reasoning_content`.

    Mimo thinking mode (default ON) returns `reasoning_content` field
    в response. Standard LangChain ChatOpenAI ignores it → iter.1+ mimo
    rejects 400 «reasoning_content must be passed back».

    Use вместо плейного `ChatOpenAI` для mimo providers. Other providers
    (OpenRouter, Anthropic) НЕ нуждаются в этом subclass — у них нет
    такого contract'а.

    Implementation: subclass overrides two ChatOpenAI internal methods:
      - `_create_chat_result(response, ...)` — extract reasoning_content
        from response (via openai SDK's pydantic-preserved field).
      - `_get_request_payload(input_, ...)` — inject reasoning_content
        from AIMessage.additional_kwargs обратно в request body.

    No-op для:
      - AIMessages без reasoning_content в additional_kwargs (incoming
        history loaded from DB не contains stored CoT — by design).
      - Responses без reasoning_content (chitchat без thinking).
    """

    # ───── extract from response ─────

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict | None = None,
    ) -> "ChatResult":
        """Extract `reasoning_content` from mimo response, attach to AIMessage.

        Parent's `_create_chat_result` constructs the ChatResult from parsed
        openai SDK response. После него мы пробегаем по response choices и
        копируем `reasoning_content` (если присутствует) в соответствующий
        `generation.message.additional_kwargs["reasoning_content"]`.

        Three-tier fallback для поиска reasoning_content:
          1. ``msg.reasoning_content`` — pydantic attribute access (openai
             SDK ChatCompletionMessage с pydantic v2 ``extra="allow"``).
          2. ``msg.get("reasoning_content")`` — dict-shaped response (legacy
             paths, response_format edge cases).
          3. ``msg.model_extra["reasoning_content"]`` — pydantic v2 extras
             bucket fallback (ensures we catch field even если future SDK
             stops exposing as attribute но keeps в extras).
        """
        result = super()._create_chat_result(response, generation_info)
        choices: list[Any] = []
        if hasattr(response, "choices"):
            choices = response.choices or []
        elif isinstance(response, dict):
            choices = response.get("choices") or []

        for idx, choice in enumerate(choices):
            if idx >= len(result.generations):
                break
            msg_obj = getattr(choice, "message", None)
            if msg_obj is None and isinstance(choice, dict):
                msg_obj = choice.get("message", {})

            rc = None
            # Tier 1: attribute access (pydantic v2 SDK)
            if hasattr(msg_obj, "reasoning_content"):
                rc = msg_obj.reasoning_content
            # Tier 2: dict access (legacy)
            elif isinstance(msg_obj, dict):
                rc = msg_obj.get("reasoning_content")
            # Tier 3: model_extra fallback (pydantic v2 extras bucket)
            if not rc and hasattr(msg_obj, "model_extra"):
                extra = msg_obj.model_extra or {}
                rc = extra.get("reasoning_content")

            if rc:
                gen_msg = result.generations[idx].message
                if hasattr(gen_msg, "additional_kwargs"):
                    if gen_msg.additional_kwargs is None:
                        # Defensive: LangChain defaults to {}, но safe.
                        gen_msg.additional_kwargs = {}
                    gen_msg.additional_kwargs["reasoning_content"] = rc

        return result

    # ───── inject into next request ─────

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Inject `reasoning_content` from AIMessage history back into request.

        Per mimo's thinking-mode API contract, assistant messages в subsequent
        requests **must** carry the original `reasoning_content` field. Otherwise
        mimo rejects 400 «must be passed back to the API».

        Mutates the request body dict in-place (mutation propagates через
        `payload["messages"]` reference).

        Защищается length-mismatch guard'ом против ChatOpenAI's internal
        message synthesis (e.g. system-message addition). Если lengths
        disagree — skip injection silently (super() output uses-as-is).
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # Normalize input_ to flat list of BaseMessage — three cases handled:
        # PromptValue / list / single BaseMessage. Matches LangChain's own
        # `_convert_input(...).to_messages()` semantics для consistency.
        messages_list: list[Any]
        if hasattr(input_, "to_messages"):  # PromptValue
            messages_list = input_.to_messages()
        elif isinstance(input_, list):
            messages_list = input_
        else:
            # Single BaseMessage path (rare, but supported by LangChain).
            messages_list = [input_]

        payload_msgs: list[dict] = payload.get("messages") or []
        if len(messages_list) != len(payload_msgs):
            # Length disagreement → super() did its own thing (e.g. synthesized
            # extra system message). Safe-skip injection — без guarantee 1:1
            # mapping мы можем inject в wrong dict.
            return payload

        for orig, payload_msg in zip(messages_list, payload_msgs):
            if isinstance(orig, AIMessage):
                rc = (orig.additional_kwargs or {}).get("reasoning_content")
                if rc:
                    # Top-level field on assistant message, как mimo expects.
                    payload_msg["reasoning_content"] = rc

        return payload
