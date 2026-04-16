from __future__ import annotations

import logging

import httpx

from sreda.services.speech.base import SpeechRecognitionError

logger = logging.getLogger(__name__)

_ENDPOINT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class YandexSpeechKitRecognizer:
    """Yandex SpeechKit Sync REST API v1 (OGG/Opus, up to 30 sec)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def recognize(self, audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
        params = {
            "lang": lang,
            "format": "oggopus",
            "sampleRateHertz": "48000",
        }
        headers = {"Authorization": f"Api-Key {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    _ENDPOINT,
                    params=params,
                    headers=headers,
                    content=audio_bytes,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("YandexSpeechKit HTTP error: %s", exc)
            raise SpeechRecognitionError(f"SpeechKit HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            logger.warning("YandexSpeechKit request error: %s", exc)
            raise SpeechRecognitionError("SpeechKit request failed") from exc

        result = data.get("result")
        if not isinstance(result, str) or not result.strip():
            raise SpeechRecognitionError("SpeechKit returned empty result")
        return result.strip()
