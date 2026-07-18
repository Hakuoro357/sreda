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
            # trust_env=False: Yandex — РОССИЙСКИЙ сервис, VDS в Москве → идём НАПРЯМУЮ, а не через
            # глобальный HTTPS_PROXY=socks5://:1080 (зарубежный NL-туннель). Замер 2026-07-03: прямой
            # путь ~40мс vs туннель ~435мс (10×), плюс аудио (до 188КБ) грузилось крюком RU→NL→RU →
            # спайки/30с-таймаут-отказы STT. Тот же паттерн RU-direct, что MAX-клиент/admin_alerts.
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
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
        except ValueError as exc:
            # json.JSONDecodeError ⊂ ValueError: не-JSON тело с HTTP 200.
            # Контракт SpeechRecognitionError обязателен — иначе исключение
            # улетает мимо voice-хендлера и ветки refund квот
            # (аудит 2026-07-18, llm-core MAJOR).
            logger.warning("YandexSpeechKit invalid JSON body: %s", exc)
            raise SpeechRecognitionError(
                "SpeechKit returned invalid JSON"
            ) from exc

        if not isinstance(data, dict):
            # JSON-список/скаляр вместо объекта — .get() бросил бы
            # AttributeError вне контракта SpeechRecognitionError.
            logger.warning(
                "YandexSpeechKit unexpected payload type: %s",
                type(data).__name__,
            )
            raise SpeechRecognitionError(
                "SpeechKit returned unexpected payload shape"
            )
        result = data.get("result")
        if not isinstance(result, str) or not result.strip():
            raise SpeechRecognitionError("SpeechKit returned empty result")
        return result.strip()
