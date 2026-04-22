"""Tests for GroqWhisperRecognizer + factory wiring + fallback chain.

These are fast unit tests — no real Groq calls. httpx is monkey-patched
per-test so we can assert request shape (multipart content, model
name, language code) and response behaviour (empty transcript →
SpeechRecognitionError, 5xx → SpeechRecognitionError, fallback
activation on primary failure).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sreda.services.speech.base import SpeechRecognitionError
from sreda.services.speech.fallback import FallbackSpeechRecognizer
from sreda.services.speech.groq import GroqWhisperRecognizer


# ---------------------------------------------------------------------------
# httpx.AsyncClient monkey-patch scaffolding. Each test can plug in a
# FakeClient that returns the response it wants and captures the
# request args so we can assert on them.
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | str) -> None:
        self.status_code = status_code
        self._body = body
        # httpx.HTTPStatusError reads response.text for the preview log.
        self.text = body if isinstance(body, str) else str(body)

    def json(self) -> dict[str, Any]:
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


class FakeAsyncClient:
    last_kwargs: dict[str, Any] | None = None

    def __init__(self, response: FakeResponse, *, raise_request: bool = False) -> None:
        self._response = response
        self._raise_request = raise_request

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc) -> None:  # noqa: D401
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        FakeAsyncClient.last_kwargs = {"url": url, **kwargs}
        if self._raise_request:
            raise httpx.ConnectError("simulated network error")
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, response: FakeResponse, *, raise_request: bool = False) -> None:
    def factory(*args: Any, **kwargs: Any) -> FakeAsyncClient:
        return FakeAsyncClient(response, raise_request=raise_request)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    FakeAsyncClient.last_kwargs = None


# ---------------------------------------------------------------------------
# GroqWhisperRecognizer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groq_sends_ogg_bytes_and_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, FakeResponse(200, {"text": "привет как дела"}))
    rec = GroqWhisperRecognizer(api_key="test-key")
    text = await rec.recognize(b"FAKE_OGG_BYTES", lang="ru-RU")
    assert text == "привет как дела"
    kwargs = FakeAsyncClient.last_kwargs
    assert kwargs is not None
    assert kwargs["url"].endswith("/audio/transcriptions")
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    # file must carry OGG content and mime-type
    files = kwargs["files"]
    assert files["file"][1] == b"FAKE_OGG_BYTES"
    assert files["file"][2] == "audio/ogg"
    # language must be normalised to 2-letter "ru" for Whisper
    assert kwargs["data"]["language"] == "ru"
    # default model is full whisper-large-v3 (not turbo) —
    # better WER on foreign/code-switched Russian speech
    assert kwargs["data"]["model"] == "whisper-large-v3"


@pytest.mark.asyncio
async def test_groq_strips_result_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, FakeResponse(200, {"text": "  кажется, всё\n\n"}))
    rec = GroqWhisperRecognizer(api_key="k")
    assert await rec.recognize(b"a") == "кажется, всё"


@pytest.mark.asyncio
async def test_groq_empty_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, FakeResponse(200, {"text": "   "}))
    rec = GroqWhisperRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="empty"):
        await rec.recognize(b"a")


@pytest.mark.asyncio
async def test_groq_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, FakeResponse(429, "rate limited"))
    rec = GroqWhisperRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="429"):
        await rec.recognize(b"a")


@pytest.mark.asyncio
async def test_groq_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, FakeResponse(200, {"text": "ok"}), raise_request=True)
    rec = GroqWhisperRecognizer(api_key="k")
    with pytest.raises(SpeechRecognitionError, match="request failed"):
        await rec.recognize(b"a")


# ---------------------------------------------------------------------------
# FallbackSpeechRecognizer
# ---------------------------------------------------------------------------


class _StubRec:
    def __init__(self, result: str | Exception) -> None:
        self._result = result
        self.calls = 0

    async def recognize(self, audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.asyncio
async def test_fallback_primary_success_skips_fallback() -> None:
    primary = _StubRec("primary-text")
    fallback = _StubRec("fallback-text")
    chain = FallbackSpeechRecognizer(primary, fallback)
    assert await chain.recognize(b"x") == "primary-text"
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_fallback_on_primary_error() -> None:
    primary = _StubRec(SpeechRecognitionError("primary boom"))
    fallback = _StubRec("fallback-text")
    chain = FallbackSpeechRecognizer(primary, fallback)
    assert await chain.recognize(b"x") == "fallback-text"
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_both_fail_raises_primary_error() -> None:
    """When both fail, we re-raise the PRIMARY error — that's the
    fresh signal we care about; fallback is the safety net."""
    primary = _StubRec(SpeechRecognitionError("primary boom"))
    fallback = _StubRec(SpeechRecognitionError("fallback boom"))
    chain = FallbackSpeechRecognizer(primary, fallback)
    with pytest.raises(SpeechRecognitionError, match="primary boom"):
        await chain.recognize(b"x")


# ---------------------------------------------------------------------------
# factory.get_speech_recognizer wiring
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(
        self,
        *,
        provider: str | None = None,
        yandex_key: str | None = None,
        groq_key: str | None = None,
    ) -> None:
        self.speech_provider = provider
        self.yandex_speechkit_api_key = yandex_key
        self._groq_key = groq_key

    def resolve_groq_api_key(self) -> str | None:
        return self._groq_key


def test_factory_returns_none_for_unknown_provider() -> None:
    from sreda.services.speech.factory import get_speech_recognizer

    assert get_speech_recognizer(_FakeSettings(provider=None)) is None  # type: ignore[arg-type]
    assert get_speech_recognizer(_FakeSettings(provider="faster_whisper")) is None  # type: ignore[arg-type]


def test_factory_yandex_requires_key() -> None:
    from sreda.services.speech.factory import get_speech_recognizer

    assert get_speech_recognizer(_FakeSettings(provider="yandex")) is None  # type: ignore[arg-type]
    rec = get_speech_recognizer(_FakeSettings(provider="yandex", yandex_key="y"))  # type: ignore[arg-type]
    assert rec is not None


def test_factory_groq_requires_key() -> None:
    from sreda.services.speech.factory import get_speech_recognizer

    assert get_speech_recognizer(_FakeSettings(provider="groq")) is None  # type: ignore[arg-type]
    rec = get_speech_recognizer(_FakeSettings(provider="groq", groq_key="g"))  # type: ignore[arg-type]
    assert rec is not None
    assert rec.__class__.__name__ == "GroqWhisperRecognizer"


def test_factory_groq_plus_yandex_builds_fallback_chain() -> None:
    from sreda.services.speech.factory import get_speech_recognizer

    rec = get_speech_recognizer(
        _FakeSettings(provider="groq+yandex", groq_key="g", yandex_key="y")  # type: ignore[arg-type]
    )
    assert isinstance(rec, FallbackSpeechRecognizer)


def test_factory_groq_plus_yandex_degrades_gracefully_when_one_key_missing() -> None:
    """If only one of the two keys is configured, return the available
    provider standalone — never None. Silent STT disable would mean
    voice messages are ignored with no admin signal."""
    from sreda.services.speech.factory import get_speech_recognizer

    only_groq = get_speech_recognizer(
        _FakeSettings(provider="groq+yandex", groq_key="g", yandex_key=None)  # type: ignore[arg-type]
    )
    assert only_groq is not None
    assert only_groq.__class__.__name__ == "GroqWhisperRecognizer"

    only_yandex = get_speech_recognizer(
        _FakeSettings(provider="groq+yandex", groq_key=None, yandex_key="y")  # type: ignore[arg-type]
    )
    assert only_yandex is not None
    assert only_yandex.__class__.__name__ == "YandexSpeechKitRecognizer"
