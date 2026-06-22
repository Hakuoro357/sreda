"""#192: HMAC аргументов инструмента для трейса ReAct (`react_turn_trace.tool_calls_json`).

Сырые аргументы инструмента — ПД (текст напоминания, адреса, даты). В трейс кладём ТОЛЬКО HMAC:
(а) видеть «тот же вызов / другой» для отладки, (б) не хранить сырьё. HMAC, а НЕ голый sha256 —
иначе низкоэнтропийные аргументы («молоко», даты, имена) перебираются словарём. Ключ деривится из
существующего секрета шифрования (`settings.encryption_key`) с domain-separation — нового env НЕ
вводим (решение по плану #192). `key_id`/`version` — для безопасной ротации.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

ARGS_HASH_VERSION = 1
# domain-separation: дериват ключа для ИМЕННО этого назначения (не реюз ключа шифрования напрямую)
_KDF_INFO = b"sreda.react_trace.args_hmac.v1"


def _derive_key() -> tuple[str, bytes]:
    """(key_id, derived_key). Дериват из settings.encryption_key + encryption_key_id (для ротации)."""
    from sreda.config.settings import get_settings

    s = get_settings()
    secret = (s.encryption_key or "").encode("utf-8")
    key_id = s.encryption_key_id or "0"
    # HKDF-expand-подобно: HMAC(secret, info) — domain-separated 32-байтный ключ
    derived = hmac.new(secret, _KDF_INFO, hashlib.sha256).digest()
    return key_id, derived


def canonical_json(obj: Any) -> str:
    """Стабильная сериализация для хэша: ключи отсортированы, без пробелов."""
    import json

    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def args_hmac(*, tenant_id: str, tool_name: str, args: Any,
              schema_version: int = ARGS_HASH_VERSION) -> str:
    """HMAC-SHA256 по canonical JSON `{tenant_id, tool_name, schema_version, args}` с domain-separation.

    Формат: `v{version}:{key_id}:{hexdigest}` — версия/ключ для ротации. НЕ равен голому sha256(args)."""
    _key_id, key = _derive_key()
    payload = canonical_json({
        "tenant_id": tenant_id,
        "tool_name": tool_name,
        "schema_version": schema_version,
        "args": args,
    })
    digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v{schema_version}:{_key_id}:{digest}"
