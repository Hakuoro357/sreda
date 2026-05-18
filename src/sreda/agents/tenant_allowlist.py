"""R-39: helpers для канарейки по тенантам.

Два механизма rollout'а — комбинируются caller-ом:

1. **Точный allowlist** ``r39_pilot_tenant_ids`` — список tenant_id
   через запятую. Используется на старте: «только pilot tenant».
   Простой lookup в frozenset, любой формат от админа понимаем.

2. **Хеш-канарейка** ``r39_canary_percent`` — целое 0..100. Используем
   стабильный sha256 от tenant_id чтобы попадание было детерминированным
   (один и тот же тенант всегда в одной когорте — не «прыгает»). Когда
   точный allowlist стабилизируется на пилотном тенанте, открываем 5% →
   25% → 100% хеш-канарейкой.

Pure-функции — не лезут в БД, runtime_config-полей сами не читают.
Caller вытаскивает строки и передаёт сюда.

R-39 R4 fix: реальный ``ActionEnvelope.tenant_id`` — это **str** (max 64),
не int (`Tenant.id: String(64)`). Поэтому все API принимают строки и
не пытаются `int(token)`.
"""

from __future__ import annotations

import hashlib
import logging
import re


logger = logging.getLogger(__name__)


# Любой не-разделитель — считаем токеном tenant_id (строки могут быть
# числовыми типа "352612382" или UUID-like).
_TOKEN_SEP_RE = re.compile(r"[,;\s\[\]]+")


# ─── Точный allowlist ────────────────────────────────────────────────


def parse_pilot_tenants(raw: str | None) -> frozenset[str]:
    """Парсит строку с tenant_id'ами в frozenset[str].

    Поддерживаемые форматы (разделитель — запятая, пробел или
    точка-с-запятой):
      - ``"352612382"``
      - ``"352612382,42"``
      - ``"[352612382, 42]"`` (JSON-подобное)
      - ``""`` или ``None`` → пустой frozenset

    Пустые токены пропускаются. Tenant id — строки любого формата
    (числовые, UUID, alphanumeric).
    """
    if not raw:
        return frozenset()
    out: set[str] = set()
    for token in _TOKEN_SEP_RE.split(raw):
        token = token.strip()
        if not token:
            continue
        out.add(token)
    return frozenset(out)


def is_in_pilot(tenant_id: str, raw_allowlist: str | None) -> bool:
    """Тенант в точном allowlist'е?"""
    if not raw_allowlist:
        return False
    return str(tenant_id) in parse_pilot_tenants(raw_allowlist)


# ─── Хеш-канарейка ────────────────────────────────────────────────────


def parse_canary_percent(raw: str | None) -> int:
    """Парсит процент канарейки в целое 0..100.

    На любую ошибку — 0 (no canary). Лучше плохой парсинг чем
    случайная 100% выкатка из-за typo.
    """
    if not raw:
        return 0
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        logger.warning("tenant_allowlist: невалидный canary percent %r → 0", raw)
        return 0
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


def tenant_in_canary(tenant_id: str, percent: int) -> bool:
    """Тенант попадает в канарейку N%?

    Стабильный хеш: sha256(tenant_id_str) % 100 < percent. Один и тот же
    тенант всегда в одной когорте — при расширении 5→25 точно
    остаётся внутри.
    """
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.sha256(str(tenant_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    return bucket < percent


# ─── Финальное решение ───────────────────────────────────────────────


def is_r39_enabled(
    tenant_id: str,
    *,
    pilot_allowlist: str | None,
    canary_percent: str | None = None,
) -> bool:
    """Включён ли R-39 стек для этого тенанта?

    Логика:
      1. Если тенант в точном allowlist'е — да.
      2. Иначе если canary_percent > 0 и хеш попадает в когорту — да.
      3. Иначе — нет (старый стек).
    """
    if is_in_pilot(tenant_id, pilot_allowlist):
        return True
    percent = parse_canary_percent(canary_percent)
    return tenant_in_canary(tenant_id, percent)
