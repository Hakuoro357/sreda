"""Groq Whisper recognizer.

Same Whisper large-v3 weights as OpenAI's hosted endpoint — Groq just
runs them on dedicated LPU hardware, so a 15-sec clip that takes 2-3s
on OpenAI comes back in 300-500ms. Pricing is also roughly 3x cheaper
($0.00185/min vs OpenAI's $0.006/min).

OGG/Opus is an accepted input format, so the Telegram voice blob goes
in unchanged — no ffmpeg transcode step.

Endpoint: https://api.groq.com/openai/v1/audio/transcriptions
Docs:     https://console.groq.com/docs/speech-to-text
"""

from __future__ import annotations

import logging
import os

import httpx

from sreda.services.speech.base import SpeechRecognitionError

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
# Full Whisper-large-v3 — the "turbo" variant drops 30% of decoder
# layers and is notably weaker on foreign words / code-switching
# ("Дьявола"→"Диабла", "latte"→"лат"). For a Russian-speaking user
# base who sprinkles in foreign dish names / brand names we trade
# ~100-200ms latency for ~5-10% WER win on those tokens.
_DEFAULT_MODEL = "whisper-large-v3"


def _resolve_outbound_proxy() -> str | None:
    """Groq blocks Russian IPs with HTTP 403. On the Mac-mini prod the
    local ``HTTPS_PROXY`` env var already points at pproxy (HTTP→SOCKS5
    to the VDS tunnel) that MiMo and Telegram use. httpx's AsyncClient
    doesn't always honour env proxies in async mode, so we read the
    var ourselves and pass it explicitly. Returns None when no proxy
    is configured — dev machines reaching Groq directly."""
    for var in ("SREDA_GROQ_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                "https_proxy", "http_proxy"):
        value = os.environ.get(var)
        if value:
            return value
    return None


class GroqWhisperRecognizer:
    """Groq-hosted Whisper transcription over an OpenAI-compatible
    ``/audio/transcriptions`` route. ``model`` defaults to
    ``whisper-large-v3-turbo`` — fastest at ~200x real-time with only
    a minor quality gap vs plain ``whisper-large-v3``.
    """

    def __init__(self, api_key: str, *, model: str = _DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self._model = model

    async def recognize(self, audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
        # Whisper expects 2-letter ISO codes ("ru"), not locale tags ("ru-RU").
        lang_iso = (lang or "").split("-")[0].lower() or "ru"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
        data = {
            "model": self._model,
            "language": lang_iso,
            "response_format": "json",
            "temperature": "0",
        }
        proxy = _resolve_outbound_proxy()
        client_kwargs: dict = {"timeout": 30.0}
        if proxy:
            client_kwargs["proxy"] = proxy
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    _ENDPOINT, headers=headers, files=files, data=data,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Groq Whisper HTTP %s: %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise SpeechRecognitionError(
                f"Groq Whisper HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            logger.warning("Groq Whisper request error: %s", exc)
            raise SpeechRecognitionError("Groq Whisper request failed") from exc

        text = (payload.get("text") or "").strip()
        if not text:
            raise SpeechRecognitionError("Groq Whisper returned empty transcript")
        return text
