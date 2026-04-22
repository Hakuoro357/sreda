from __future__ import annotations

from typing import TYPE_CHECKING

from sreda.services.speech.base import SpeechRecognizer

if TYPE_CHECKING:
    from sreda.config.settings import Settings


def _build_yandex(settings: Settings) -> SpeechRecognizer | None:
    from sreda.services.speech.yandex import YandexSpeechKitRecognizer

    api_key = settings.yandex_speechkit_api_key
    if not api_key:
        return None
    return YandexSpeechKitRecognizer(api_key=api_key)


def _build_groq(settings: Settings) -> SpeechRecognizer | None:
    from sreda.services.speech.groq import GroqWhisperRecognizer

    api_key = settings.resolve_groq_api_key()
    if not api_key:
        return None
    return GroqWhisperRecognizer(api_key=api_key)


def get_speech_recognizer(settings: Settings) -> SpeechRecognizer | None:
    """Return configured SpeechRecognizer or None if not configured.

    Providers:
      * ``yandex``          — Yandex SpeechKit only.
      * ``groq``            — Groq Whisper only (fast, cheap, same
        Whisper large-v3 weights as OpenAI). Returns None if the Groq
        key isn't configured — callers treat None as "STT disabled",
        so we don't want to silently fall back to a different provider
        when admin explicitly asked for groq.
      * ``groq+yandex``     — Groq primary, Yandex as a safety net
        (fallback on any SpeechRecognitionError). Requires BOTH keys
        configured; if only one is present, degrades to whichever is
        available rather than returning None.
      * Anything else       — None (STT disabled).
    """
    provider = settings.speech_provider
    if provider == "yandex":
        return _build_yandex(settings)
    if provider == "groq":
        return _build_groq(settings)
    if provider == "groq+yandex":
        primary = _build_groq(settings)
        fallback = _build_yandex(settings)
        if primary and fallback:
            from sreda.services.speech.fallback import FallbackSpeechRecognizer

            return FallbackSpeechRecognizer(
                primary=primary,
                fallback=fallback,
                primary_label="groq",
                fallback_label="yandex",
            )
        # Degrade gracefully: whichever provider is actually
        # reachable with the keys we have.
        return primary or fallback
    return None
