"""Two-tier speech recognizer — primary, then fallback on failure.

Lets us promote a fast-but-new provider (Groq Whisper) to production
while keeping the battle-tested one (Yandex SpeechKit) as a safety
net. If the primary throws ``SpeechRecognitionError`` we try the
fallback; if both fail, the primary's error is re-raised so admin
dashboards still surface the fresh failure mode.
"""

from __future__ import annotations

import logging

from sreda.services.speech.base import SpeechRecognitionError, SpeechRecognizer

logger = logging.getLogger(__name__)


class FallbackSpeechRecognizer:
    """Primary → fallback chain. One level deep on purpose — three
    tiers would hide which provider's issue we're actually debugging."""

    def __init__(
        self, primary: SpeechRecognizer, fallback: SpeechRecognizer, *,
        primary_label: str = "primary",
        fallback_label: str = "fallback",
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_label = primary_label
        self._fallback_label = fallback_label

    async def recognize(self, audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
        try:
            return await self._primary.recognize(audio_bytes, lang=lang)
        except SpeechRecognitionError as primary_exc:
            logger.warning(
                "STT primary (%s) failed — falling back to %s: %s",
                self._primary_label,
                self._fallback_label,
                primary_exc,
            )
            try:
                return await self._fallback.recognize(audio_bytes, lang=lang)
            except SpeechRecognitionError as fallback_exc:
                logger.error(
                    "STT fallback (%s) also failed: %s",
                    self._fallback_label,
                    fallback_exc,
                )
                # Re-raise the PRIMARY error — that's the freshly
                # interesting signal. Fallback is the safety net; its
                # failure is secondary for debugging.
                raise primary_exc
