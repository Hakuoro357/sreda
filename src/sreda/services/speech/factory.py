from __future__ import annotations

from typing import TYPE_CHECKING

from sreda.services.speech.base import SpeechRecognizer

if TYPE_CHECKING:
    from sreda.config.settings import Settings


def get_speech_recognizer(settings: Settings) -> SpeechRecognizer | None:
    """Return configured SpeechRecognizer or None if not configured."""
    provider = settings.speech_provider
    if provider == "yandex":
        from sreda.services.speech.yandex import YandexSpeechKitRecognizer

        api_key = settings.yandex_speechkit_api_key
        if not api_key:
            return None
        return YandexSpeechKitRecognizer(api_key=api_key)
    return None
