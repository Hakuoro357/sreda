"""R-39 Slice 6: режим R-39 для тенанта (off / shadow / live).

Читает runtime_config:
  - ``r39_pilot_tenant_ids`` — точный allowlist для live mode
  - ``r39_shadow_tenant_ids`` — точный allowlist для shadow mode

Тенант в обоих списках → ``live`` приоритетнее (debugger Boris хочет
видеть свой настоящий R-39 ответ, не shadow log).

Только для ``feature_key == "housewife_assistant"`` — другие skill'ы
не имеют R-39 contracts. Иначе ``off``.

Pure-функция: на любую ошибку runtime_config возвращает ``"off"``
(safe default — legacy продолжает работать).
"""

from __future__ import annotations

import logging
from typing import Any

from sreda.agents.tenant_allowlist import parse_pilot_tenants


logger = logging.getLogger(__name__)


_HOUSEWIFE_FEATURE_KEY = "housewife_assistant"
_CONFIG_KEY_PILOT = "r39_pilot_tenant_ids"
_CONFIG_KEY_SHADOW = "r39_shadow_tenant_ids"


def resolve_r39_mode(
    tenant_id: str,
    feature_key: str | None,
    session: Any,
) -> str:
    """Вернёт ``"off"`` | ``"shadow"`` | ``"live"`` для текущего тенанта.

    Args:
        tenant_id: tenant_id строкой (см. ActionEnvelope.tenant_id: str).
        feature_key: текущая активная skill ("housewife_assistant" чтобы
            R-39 включился; всё остальное → off).
        session: SQLAlchemy session для чтения runtime_config.

    Returns:
        "off" — legacy путь (дефолт)
        "shadow" — R-39 worker в фоне после legacy (читает реальный
            трафик, реальные tool calls не делаются)
        "live" — R-39 заменяет legacy, реальные tool calls
    """
    if feature_key != _HOUSEWIFE_FEATURE_KEY:
        return "off"

    try:
        from sreda.services import runtime_config as rc
    except Exception:
        logger.exception("R-39 mode: runtime_config import failed")
        return "off"

    tid_str = str(tenant_id)

    try:
        pilot = rc.get_config(session, _CONFIG_KEY_PILOT) or ""
    except Exception:
        logger.exception("R-39 mode: pilot allowlist read failed")
        return "off"
    if _matches_allowlist(tid_str, pilot):
        return "live"

    try:
        shadow = rc.get_config(session, _CONFIG_KEY_SHADOW) or ""
    except Exception:
        logger.exception("R-39 mode: shadow allowlist read failed")
        return "off"
    if _matches_allowlist(tid_str, shadow):
        return "shadow"

    return "off"


def _matches_allowlist(tenant_id: str, raw: str) -> bool:
    """Allowlist matching с поддержкой wildcard `"*"`.

    Если значение runtime_config содержит токен `"*"` — matches любой
    tenant (housewife users only — выше есть feature_key gate). Используется
    для широкого shadow rollout без перечисления всех tenant_ids:
        UPDATE runtime_config SET value='*' WHERE key='r39_shadow_tenant_ids';

    Пустая строка / None → False.
    """
    if not raw:
        return False
    parsed = parse_pilot_tenants(raw)
    if "*" in parsed:
        return True  # wildcard: все тенанты под housewife_assistant
    return tenant_id in parsed
