from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(slots=True)
class SensitiveEntity:
    entity_type: str
    match_text: str
    replacement: str


@dataclass(slots=True)
class TextSanitizationResult:
    original_text: str
    sanitized_text: str
    entities: list[SensitiveEntity]

    @property
    def contains_sensitive_data(self) -> bool:
        return bool(self.entities)


@dataclass(slots=True)
class StructureSanitizationResult:
    sanitized_value: Any
    entities: list[SensitiveEntity]

    @property
    def contains_sensitive_data(self) -> bool:
        return bool(self.entities)


class RegexPrivacyGuard:
    def sanitize_text(self, text: str | None) -> TextSanitizationResult | None:
        if text is None:
            return None

        sanitized = str(text)
        entities: list[SensitiveEntity] = []
        for pattern, replacer in _RULES:
            sanitized = pattern.sub(lambda match: replacer(match, entities), sanitized)

        return TextSanitizationResult(
            original_text=str(text),
            sanitized_text=sanitized,
            entities=entities,
        )

    def sanitize_structure(self, value: Any) -> StructureSanitizationResult:
        entities: list[SensitiveEntity] = []
        sanitized_value = self._sanitize_structure_inner(value, entities)
        return StructureSanitizationResult(
            sanitized_value=sanitized_value,
            entities=entities,
        )

    def _sanitize_structure_inner(self, value: Any, entities: list[SensitiveEntity]) -> Any:
        if isinstance(value, str):
            result = self.sanitize_text(value)
            if result is None:
                return value
            entities.extend(result.entities)
            return result.sanitized_text
        if isinstance(value, list):
            return [self._sanitize_structure_inner(item, entities) for item in value]
        if isinstance(value, dict):
            return {
                key: self._sanitize_structure_inner(item, entities)
                for key, item in value.items()
            }
        return value


def _replace_full(entity_type: str, replacement: str):
    def replacer(match: re.Match[str], entities: list[SensitiveEntity]) -> str:
        entities.append(
            SensitiveEntity(
                entity_type=entity_type,
                match_text=match.group(0),
                replacement=replacement,
            )
        )
        return replacement

    return replacer


def _replace_group_value(entity_type: str, replacement: str, *, prefix_group: int = 1, value_group: int = 2):
    def replacer(match: re.Match[str], entities: list[SensitiveEntity]) -> str:
        entities.append(
            SensitiveEntity(
                entity_type=entity_type,
                match_text=match.group(value_group),
                replacement=replacement,
            )
        )
        return f"{match.group(prefix_group)}{replacement}"

    return replacer


_RULES: list[tuple[re.Pattern[str], Any]] = [
    # NOTE: the credential/label rules require the "value" group to be
    # at least 3 non-space characters. Without this guard the regex
    # happily eats Russian conjunctions and sentence terminators — e.g.
    # "Проверь логин и пароль." becomes "Проверь логин [login][password]",
    # corrupting legitimate natural-language error messages that happen
    # to mention the words ``логин``/``пароль``. Real credentials are
    # always longer than 2 characters, so this threshold is safe.
    (
        re.compile(r"(?i)(\b(?:пароль|password)\b\s*[:=]?\s*)([^\s,;]{3,})"),
        _replace_group_value("password", "[password]"),
    ),
    (
        re.compile(r"(?i)(\b(?:логин|login)\b\s*[:=]?\s*)([^\s,;]{3,})"),
        _replace_group_value("login", "[login]"),
    ),
    (
        re.compile(r"(?i)(\b(?:код\s+подтверждения|verification\s+code|код)\b\s*[:=]?\s*)([^\s,;]{3,})"),
        _replace_group_value("verification_code", "[verification_code]"),
    ),
    (
        re.compile(r"(?i)(\b(?:номер\s+лицевого\s+сч[её]та)\b\s*[:=]?\s*)([^\s,;]{3,})"),
        _replace_group_value("account_number", "[account_number]"),
    ),
    (
        re.compile(r"(?i)(\b(?:лицевой\s+сч[её]т|лицевого\s+сч[её]та|сч[её]т)\b\s*[:=]?\s*)(\d{4,})"),
        _replace_group_value("account_number", "[account_number]"),
    ),
    (
        re.compile(r"(?i)(\b(?:bearer|token|api[_ -]?key|secret)\b\s*[:=]?\s*)([^\s,;]{3,})"),
        _replace_group_value("secret", "[secret]"),
    ),
    (
        re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b", re.IGNORECASE),
        _replace_full("email", "[email]"),
    ),
    (
        re.compile(r"\+?\d[\d\s()\-]{8,}\d"),
        _replace_full("phone", "[phone]"),
    ),
    (
        re.compile(r"https?://[^\s]+", re.IGNORECASE),
        lambda match, entities: _replace_url(match, entities),
    ),
    (
        re.compile(r"\b\d{10,}\b"),
        _replace_full("number", "[number]"),
    ),
]


def _replace_url(match: re.Match[str], entities: list[SensitiveEntity]) -> str:
    value = match.group(0)
    lowered = value.lower()
    if "?" not in value and not any(marker in lowered for marker in ("token", "key", "secret", "sig", "password")):
        return value
    entities.append(
        SensitiveEntity(
            entity_type="url",
            match_text=value,
            replacement="[url]",
        )
    )
    return "[url]"


@lru_cache(maxsize=1)
def get_default_privacy_guard() -> RegexPrivacyGuard:
    return RegexPrivacyGuard()
