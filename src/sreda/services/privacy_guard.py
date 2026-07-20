from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final


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
            out: dict[Any, Any] = {}
            for key, item in value.items():
                # 2026-04-29 (incident user_tg_1089832184): structural
                # ID-keys passthrough untouched. Sanitize'ить надо
                # контент сообщений, не идентификаторы. Defence in depth
                # к regex word-boundary fix: даже если кто-то добавит
                # новый regex без `(?<!\w)/(?!\w)`, известные ID-поля
                # защищены на walker-уровне.
                if isinstance(key, str) and key in _STRUCTURAL_KEY_NAMES:
                    out[key] = item
                else:
                    out[key] = self._sanitize_structure_inner(item, entities)
            return out
        return value


# 2026-04-29: ключи которые `sanitize_structure` оставляет нетронутыми.
# Это структурные идентификаторы / метаданные, не пользовательский
# контент. Их значения часто содержат 10+цифровые ID (Telegram user
# IDs, например) и регулярки маскировки телефонов их ломали.
#
# Содержимое ВЛОЖЕННЫХ структур под этими ключами тоже passthrough
# — например `params: {...}` под ключом не из этого набора будет
# рекурсивно sanitize'иться (там `text` юзера и т.п.). Но
# `inbound_message_id` или `tenant_id` — простой ID, walker не
# идёт глубже.
_STRUCTURAL_KEY_NAMES: frozenset[str] = frozenset({
    # Generic identifiers
    "id", "tenant_id", "workspace_id", "user_id", "assistant_id",
    "feature_key", "skill_id",
    # Runtime / orchestration
    "action_id", "action_type", "run_id", "job_id", "thread_id",
    "trace_id", "inbound_message_id", "outbox_id", "out_id",
    "parent_id", "request_id", "correlation_id",
    # Channel / Telegram-specific
    "channel_type", "bot_key", "external_chat_id", "chat_id",
    "telegram_account_id", "callback_query_id", "update_id",
    "message_id", "from_id",
    "voice_file_id", "file_id", "file_unique_id",
})


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
        # Telegram bot token: ``123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11``
        # Appears in API URLs (``/bot<token>/sendMessage``), proxy logs,
        # httpx debug output, Sentry breadcrumbs. Must be redacted
        # before the generic URL rule runs.
        re.compile(r"\bbot(\d{8,}:[A-Za-z0-9_-]{30,})\b"),
        _replace_full("telegram_bot_token", "[telegram_bot_token]"),
    ),
    (
        re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b", re.IGNORECASE),
        _replace_full("email", "[email]"),
    ),
    # 2026-04-29: phone-маскировка снята. Телефон — обычные ПДн (не
    # спец-категория ст.10), и без plaintext'а для LLM юзеры не могут
    # сохранять номера в скилах (housewife: контакты аптек, врачей,
    # семьи). Юр. покрытие — через явное согласие в политике обработки
    # ПДн + цель «персональный AI-ассистент». Аналогично снят generic
    # `\d{10,}` rule (раньше ловил телефоны без разделителей и id'шники
    # внешних систем как `[number]`). Credentials/health-data/url-secrets
    # маскируются как раньше.
    (
        re.compile(r"https?://[^\s]+", re.IGNORECASE),
        lambda match, entities: _replace_url(match, entities),
    ),
    # 2026-04-27 (152-ФЗ обезличивание Часть 1): спец-категория ст. 10
    # — данные о состоянии здоровья. Маркируем триггерные слова
    # (аллерг*/непереносимост*/диагноз*/заболевани*/болезн*) в
    # sanitized тексте как [allergy]/[diagnosis], plaintext остаётся
    # в SecureRecord (encrypted at-rest). LLM ВИДИТ плейсхолдеры,
    # отвечает без разглашения мед-фактов.
    #
    # Безопасные слова, которые не триггерятся: «молочка», «глютен»,
    # «мясное» — это ингредиенты/диета, не медицинский диагноз.
    # «Без молочки» в меню работает как раньше.
    (
        re.compile(r"(?i)\b(аллерг\w+|непереносимост\w+)\b"),
        _replace_full("allergy", "[allergy]"),
    ),
    (
        re.compile(r"(?i)\b(диагноз\w*|заболевани\w+|болезн\w+)\b"),
        _replace_full("diagnosis", "[diagnosis]"),
    ),
]


def _replace_url(match: re.Match[str], entities: list[SensitiveEntity]) -> str:
    value = match.group(0)
    if "?" not in value and not _has_secret_url_token(value):
        return value
    entities.append(
        SensitiveEntity(
            entity_type="url",
            match_text=value,
            replacement="[url]",
        )
    )
    return "[url]"


# audit-fix 2026-07-18 (svc-security MINOR #2): маркеры — ТОЧНЫЕ токены,
# не подстроки. Раньше substring-проверка «sig»/«key» маскировала
# легитимные URL («design», «monkey», «keyboard») — over-redaction на
# разговорном hot-path деградировал контент, уходящий в LLM.
#
# 2026-07-18 (R1 C5): compound-маркеры «apikey»/«secretkey» — слитные и
# camelCase сегменты пути (``/apikey/<SECRET>``, ``/secretKey/<SECRET>``)
# не режутся токенизатором на «api»+«key» и раньше обходили redaction.
# Семантика прежняя — равенство токена, не подстрока: «monkey»/«keyboard»
# целиком не равны маркерам и по-прежнему НЕ триггерятся.
_SECRET_URL_MARKERS: Final = frozenset(
    {
        "token", "key", "secret", "sig", "signature", "password", "pwd",
        "apikey", "secretkey",
    }
)
_URL_TOKEN_SPLIT_RE: Final = re.compile(r"[^a-z0-9]+")


def _has_secret_url_token(url: str) -> bool:
    """True если в URL (без query) есть отдельный токен из markers —
    путь/хост вида ``/sig/track`` или ``/token/...``. «design»/«monkey»
    не матчатся: их токены целиком не равны маркерам."""
    tokens = {t for t in _URL_TOKEN_SPLIT_RE.split(url.lower()) if t}
    return bool(tokens & _SECRET_URL_MARKERS)


@lru_cache(maxsize=1)
def get_default_privacy_guard() -> RegexPrivacyGuard:
    return RegexPrivacyGuard()
