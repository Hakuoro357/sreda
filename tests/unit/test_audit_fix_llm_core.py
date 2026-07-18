# -*- coding: utf-8 -*-
"""Регрессионные тесты аудита 2026-07-18 (worker llm-core).

Покрытие находок `plans/audit-2026-07-18/llm-core-review.md`:

- MAJOR #1  — speech/groq.py + yandex.py: ошибки парсинга ответа STT
  (невалидный JSON / JSON-список) остаются в контракте
  ``SpeechRecognitionError`` → fallback groq→yandex срабатывает.
- MAJOR #2  — llm.py ``ainvoke_with_streaming_timeout``: delegated
  re-invoke после streaming tool-call только для mimo-провайдеров
  (reasoning_content round-trip контракт), не для остальных.
- MINOR #4  — ``_resolve_provider_overrides``: TTL-кэш без свежей
  DB-сессии на каждый вызов + invalidate.
- MINOR #7  — composer output проходит ``strip_reasoning_prefix``
  ДО blank-check (planner-путь больше не видит сырых утечек).
- MINOR #8  — embeddings: fail-fast при несовпадении dim вектора.
- MINOR #9  — embeddings: обрезка тела ошибки + shared httpx.Client.
- MINOR #10 — llm_trace: envelope с отменённым caller-future (TIMEOUT)
  не пишется на диск.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

import sreda.services.embeddings as embeddings_mod
import sreda.services.llm as llm_service
import sreda.services.llm_trace as llm_trace
from sreda.services.embeddings import OpenAICompatEmbeddingClient
from sreda.services.llm import (
    _provider_requires_reasoning_passthrough,
    ainvoke_with_streaming_timeout,
    invalidate_provider_overrides_cache,
)
from sreda.services.speech.base import SpeechRecognitionError
from sreda.services.speech.fallback import FallbackSpeechRecognizer
from sreda.services.speech.groq import GroqWhisperRecognizer
from sreda.services.speech.yandex import YandexSpeechKitRecognizer


# ---------------------------------------------------------------------------
# MAJOR #1 — STT parse errors stay inside SpeechRecognitionError contract
# ---------------------------------------------------------------------------


class _SttFakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else str(body)

    def json(self) -> Any:
        # Воспроизводим поведение httpx.Response.json() на не-JSON теле.
        if self._body == "RAISE_DECODE":
            import json as _json

            raise _json.JSONDecodeError("Expecting value", "<garbage>", 0)
        return self._body

    def raise_for_status(self) -> None:
        return None


class _SttFakeAsyncClient:
    def __init__(self, response: _SttFakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "_SttFakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(self, *_a: Any, **_k: Any) -> _SttFakeResponse:
        return self._response


def _patch_stt_httpx(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    import httpx

    def factory(*_a: Any, **_k: Any) -> _SttFakeAsyncClient:
        return _SttFakeAsyncClient(_SttFakeResponse(200, body))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_groq_invalid_json_raises_speech_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stt_httpx(monkeypatch, "RAISE_DECODE")
    rec = GroqWhisperRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="invalid JSON"):
        asyncio.run(rec.recognize(b"audio"))


def test_groq_json_list_payload_raises_speech_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stt_httpx(monkeypatch, ["не", "объект"])
    rec = GroqWhisperRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="unexpected payload"):
        asyncio.run(rec.recognize(b"audio"))


def test_groq_garbage_falls_back_to_yandex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключевая регрессия MAJOR #1: «провайдер ответил мусором с 200»
    обязан активировать fallback groq→yandex, а не улетать вверх."""
    _patch_stt_httpx(monkeypatch, "RAISE_DECODE")

    class _YandexStub:
        calls = 0

        async def recognize(self, audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
            self.calls += 1
            return "yandex-text"

    yandex = _YandexStub()
    chain = FallbackSpeechRecognizer(GroqWhisperRecognizer(api_key="k"), yandex)
    assert asyncio.run(chain.recognize(b"audio")) == "yandex-text"
    assert yandex.calls == 1


def test_yandex_invalid_json_raises_speech_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stt_httpx(monkeypatch, "RAISE_DECODE")
    rec = YandexSpeechKitRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="invalid JSON"):
        asyncio.run(rec.recognize(b"audio"))


def test_yandex_json_list_payload_raises_speech_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stt_httpx(monkeypatch, [1, 2, 3])
    rec = YandexSpeechKitRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="unexpected payload"):
        asyncio.run(rec.recognize(b"audio"))


# ---------------------------------------------------------------------------
# MAJOR #2 — re-invoke после streaming tool-call только для mimo-провайдеров
# ---------------------------------------------------------------------------


class _ToolCallStreamNoReasoning:
    """Стримит tool-call chunk БЕЗ reasoning_content; invoke возвращает
    полное сообщение с reasoning_content (mimo thinking-mode сценарий)."""

    def __init__(self) -> None:
        self.invoke_calls = 0

    def stream(self, _messages: list) -> Any:
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "add_item",
                "args": '{"item":"молоко"}',
                "id": "call_1",
                "index": 0,
            }],
        )

    def invoke(self, _messages: list) -> AIMessage:
        self.invoke_calls += 1
        return AIMessage(
            content="",
            tool_calls=[{"name": "add_item", "args": {"item": "молоко"}, "id": "call_1"}],
            additional_kwargs={"reasoning_content": "thinking trace"},
        )


def _run_stream(runnable: Any, provider: str | None) -> Any:
    return asyncio.run(
        ainvoke_with_streaming_timeout(
            runnable,
            [],
            timeout_seconds=5.0,
            on_text_update=lambda _t: None,
            provider=provider,
        )
    )


def test_streaming_reinvoke_gate_matrix() -> None:
    assert _provider_requires_reasoning_passthrough(None) is True  # legacy/консервативно
    assert _provider_requires_reasoning_passthrough("mimo") is True
    assert _provider_requires_reasoning_passthrough("mimo-v2.5-pro") is True
    assert _provider_requires_reasoning_passthrough("openrouter-qwen-flash") is False
    assert _provider_requires_reasoning_passthrough("openrouter-gemini-2.5-flash") is False
    assert _provider_requires_reasoning_passthrough("groq-gpt-oss-120b") is False
    assert _provider_requires_reasoning_passthrough("inception-mercury2") is False


def test_streaming_tool_call_reinvoke_skipped_for_non_mimo() -> None:
    """Находка MAJOR #2: для провайдера без reasoning_content контракта
    НЕ должно быть второго полного invoke (двойная стоимость + латентность)."""
    runnable = _ToolCallStreamNoReasoning()
    result = _run_stream(runnable, provider="openrouter-gemini-2.5-flash")
    assert runnable.invoke_calls == 0
    # tool_calls собраны из стрима и доступны без re-invoke
    assert getattr(result, "tool_calls", None) or getattr(result, "tool_call_chunks", None)


def test_streaming_tool_call_reinvoke_kept_for_mimo() -> None:
    """Mimo thinking-mode: re-invoke сохранён — иначе iter.1+ mimo 400
    «reasoning_content must be passed back» (инцидент 2026-05-12→14)."""
    runnable = _ToolCallStreamNoReasoning()
    result = _run_stream(runnable, provider="mimo")
    assert runnable.invoke_calls == 1
    assert result.additional_kwargs["reasoning_content"] == "thinking trace"


def test_streaming_tool_call_reinvoke_kept_for_unknown_provider() -> None:
    """provider=None (bench/legacy) — консервативно сохраняем re-invoke."""
    runnable = _ToolCallStreamNoReasoning()
    _run_stream(runnable, provider=None)
    assert runnable.invoke_calls == 1


# ---------------------------------------------------------------------------
# MINOR #4 — TTL-кэш _resolve_provider_overrides (без свежей DB-сессии на вызов)
# ---------------------------------------------------------------------------


class _FakeDbSession:
    def close(self) -> None:
        return None


def _patch_runtime_config(monkeypatch: pytest.MonkeyPatch, calls: dict) -> None:
    import sreda.db.session as db_session
    import sreda.services.runtime_config as rc

    def _factory() -> _FakeDbSession:
        calls["sessions"] += 1
        return _FakeDbSession()

    def _get_config(_session: Any, key: str) -> str | None:
        calls["reads"] += 1
        return {
            rc.KEY_CHAT_PROVIDER: "mimo",
            rc.KEY_CHAT_FALLBACK_PROVIDER: "",  # явное «без fallback»
        }.get(key)

    monkeypatch.setattr(db_session, "get_session_factory", lambda: _factory)
    monkeypatch.setattr(rc, "get_config", _get_config)
    monkeypatch.setattr(llm_service, "_provider_overrides_cache", None)


def test_provider_overrides_cached_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"sessions": 0, "reads": 0}
    _patch_runtime_config(monkeypatch, calls)
    settings = SimpleNamespace(chat_provider="env-primary", chat_fallback_provider="env-fb")

    assert llm_service._resolve_provider_overrides(settings) == ("mimo", None)
    assert llm_service._resolve_provider_overrides(settings) == ("mimo", None)
    # Второй вызов — из кэша: ни новой сессии, ни новых чтений.
    assert calls["sessions"] == 1
    assert calls["reads"] == 2


def test_provider_overrides_invalidate_forces_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"sessions": 0, "reads": 0}
    _patch_runtime_config(monkeypatch, calls)
    settings = SimpleNamespace(chat_provider="env-primary", chat_fallback_provider="env-fb")

    llm_service._resolve_provider_overrides(settings)
    invalidate_provider_overrides_cache()
    assert llm_service._resolve_provider_overrides(settings) == ("mimo", None)
    assert calls["sessions"] == 2


def test_provider_overrides_cache_keyed_by_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Иной Settings-объект с другими env-значениями идёт мимо кэша —
    иначе тесты/переопределения получили бы чужие overrides."""
    calls = {"sessions": 0, "reads": 0}
    _patch_runtime_config(monkeypatch, calls)
    first = SimpleNamespace(chat_provider="env-primary", chat_fallback_provider="env-fb")
    other = SimpleNamespace(chat_provider="other-provider", chat_fallback_provider=None)

    llm_service._resolve_provider_overrides(first)
    llm_service._resolve_provider_overrides(other)
    assert calls["sessions"] == 2


# ---------------------------------------------------------------------------
# MINOR #7 — composer output скрабится strip_reasoning_prefix
# ---------------------------------------------------------------------------


class _FakeComposerAIMessage:
    def __init__(self, content: Any) -> None:
        self.content = content
        self.usage_metadata = None


def _make_composer(reply: Any) -> Any:
    from sreda.services.composer.llm_composer import make_llm_composer
    from sreda.services.composer.llm_prompts_housewife import LLMPromptSpec
    from sreda.services.composer.prompts_registry import LLMPromptRegistry

    reg = LLMPromptRegistry()
    reg.register("k", LLMPromptSpec(
        system_prompt="SYS", required_keys=frozenset(), description="d",
    ))

    def _invoke(*_a: Any, **_k: Any) -> Any:
        return reply

    return make_llm_composer(
        registry=reg,
        settings_factory=lambda: SimpleNamespace(
            composer_provider="mimo-flash", composer_timeout_sec=30.0,
        ),
        chat_llm_factory=lambda *, settings, provider: SimpleNamespace(model="flash"),
        invoke=_invoke,
    )


def _composer_inputs() -> dict:
    from sreda.runtime.planner.executor import ExecutionLog, StepResult
    from sreda.services.composer.compose import ComposerContext

    return {
        "llm_prompt_key": "k",
        "template_data": {},
        "execution_log": ExecutionLog(
            steps=(
                StepResult(step_id="s1", tool="get_recipe_any_source",  # type: ignore[arg-type]
                           status="ok", parsed_output={"status": "found"}),
            ),
            outcome="completed",  # type: ignore[arg-type]
        ),
        "ctx": ComposerContext(tenant_id="t", run_id="r", user_message="борщ"),
    }


def test_composer_scrubs_reasoning_prefix() -> None:
    composer = _make_composer(_FakeComposerAIMessage("thought\nБорщ готов, приятного!"))
    out = composer(**_composer_inputs())
    assert out.text == "Борщ готов, приятного!"


def test_composer_full_leak_becomes_empty_output() -> None:
    """Скраб идёт ДО blank-check: ответ, целиком состоящий из утечки,
    уходит в per-key fallback, а не пользователю."""
    from sreda.services.composer.llm_composer import ComposerEmptyOutput

    composer = _make_composer(_FakeComposerAIMessage("thought\n"))
    with pytest.raises(ComposerEmptyOutput):
        composer(**_composer_inputs())


# ---------------------------------------------------------------------------
# MINOR #8/#9 — embeddings: dim fail-fast, обрезка тела ошибки, shared client
# ---------------------------------------------------------------------------


class _FakeEmbeddingsResponse:
    def __init__(self, data: Any) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._data


class _FakeEmbeddingsHttp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def post(self, *_a: Any, **_k: Any) -> _FakeEmbeddingsResponse:
        return _FakeEmbeddingsResponse(self._data)


def _emb_client(dim: int = 3) -> OpenAICompatEmbeddingClient:
    return OpenAICompatEmbeddingClient(
        base_url="http://emb.local/v1", api_key="k", model="bge-m3", dim=dim,
    )


def test_embeddings_dim_mismatch_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeEmbeddingsHttp({"data": [{"embedding": [0.1, 0.2]}]})
    monkeypatch.setattr(embeddings_mod, "_get_shared_http_client", lambda: fake)
    with pytest.raises(RuntimeError, match=r"dim=2.*expected dim=3"):
        _emb_client(dim=3).embed_query("что приготовить")


def test_embeddings_matching_dim_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeEmbeddingsHttp({"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    monkeypatch.setattr(embeddings_mod, "_get_shared_http_client", lambda: fake)
    assert _emb_client(dim=3).embed_query("q") == [0.1, 0.2, 0.3]


def test_embeddings_unexpected_body_error_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    big = {"junk": "x" * 5000}
    fake = _FakeEmbeddingsHttp(big)
    monkeypatch.setattr(embeddings_mod, "_get_shared_http_client", lambda: fake)
    with pytest.raises(RuntimeError, match="unexpected body") as exc_info:
        _emb_client().embed_query("q")
    # Всё тело (5000+ символов) НЕ должно попасть в exception/логи.
    assert len(str(exc_info.value)) < 1000


def test_embeddings_shared_http_client_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_mod, "_SHARED_HTTP_CLIENT", None)
    first = embeddings_mod._get_shared_http_client()
    second = embeddings_mod._get_shared_http_client()
    assert first is second
    first.close()


# ---------------------------------------------------------------------------
# MINOR #10 — llm_trace: TIMEOUT-envelope не пишется на диск
# ---------------------------------------------------------------------------


def test_trace_writer_skips_envelope_with_cancelled_caller_future(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """По контракту «WRITTEN ⇔ можно звонить» (require_persist) envelope,
    чей caller получил TIMEOUT (asyncio.wait_for отменил future), не
    должен оставлять request-строку для несостоявшегося LLM-вызова."""
    monkeypatch.setattr(llm_trace, "_TRACE_ROOT", tmp_path)

    async def _scenario() -> None:
        await llm_trace.startup_writer()
        try:
            loop = asyncio.get_running_loop()
            assert llm_trace._WRITE_QUEUE is not None

            # 1) «Медленный» envelope занимает writer/executor, чтобы
            #    timed-out envelope гарантированно ждал в очереди.
            gate = threading.Event()
            llm_trace._WRITER_EXECUTOR.submit(lambda: gate.wait(timeout=10))
            holder = loop.create_future()
            llm_trace._WRITE_QUEUE.put_nowait(
                ({"trace_id": "t_holder", "phase": "request"}, holder)
            )
            # 2) Envelope, чей caller «получил TIMEOUT» (wait_for cancel).
            timed_out = loop.create_future()
            timed_out.cancel()
            llm_trace._WRITE_QUEUE.put_nowait(
                ({"trace_id": "t_timed_out", "phase": "request"}, timed_out)
            )
            # 3) Sentinel: его запись ⇒ оба предыдущих уже обработаны (FIFO).
            sentinel = loop.create_future()
            llm_trace._WRITE_QUEUE.put_nowait(
                ({"trace_id": "t_sentinel", "phase": "request"}, sentinel)
            )
            gate.set()
            await asyncio.wait_for(sentinel, timeout=5.0)
        finally:
            await llm_trace.shutdown_drain(timeout_seconds=5.0)

    asyncio.run(_scenario())

    written = {p.stem for p in tmp_path.rglob("*.jsonl")}
    assert "t_holder" in written
    assert "t_sentinel" in written
    assert "t_timed_out" not in written
