from __future__ import annotations

from typing import Protocol, runtime_checkable


class SpeechRecognitionError(Exception):
    """Raised when speech recognition fails (API error, bad audio, etc.)."""


@runtime_checkable
class SpeechRecognizer(Protocol):
    async def recognize(self, audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
        """Transcribe OGG/Opus audio bytes to text.

        Raises SpeechRecognitionError on failure.
        """
        ...
