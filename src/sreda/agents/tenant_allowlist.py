"""R-39: helpers для канарейки по тенантам.

Два механизма rollout'а — комбинируются caller-ом:

1. **Точный allowlist** ``r39_pilot_tenant_ids`` — список tenant_id
   через запятую. Используется на старте: «только Boris (352612382)».
   Простой lookup в frozenset, любой формат от админа понимаем.

2. **Хеш-канарейка** ``r39_canary_percent`` — целое 0..100. Используем
   стабильный sha256 от tenant_id чтобы попадание было детерминированным
   (один и тот же тенант всегда в одной когорте — не «прыгает»). Когда
   точный allowlist стабилизируется на твоём аккаунте, открываем 5% →
   25% → 100% хеш-канарейкой.

Pure-функции — не лезут в БД, runtime_config-полей сами не читают.
Caller вытаскивает строки и передаёт сюда.
"""

from __future__ import annotations

import hashlib
import logging
import re


logger = logging.getLogger(__name__)


_INT_RE = re.compile(r"-?\d+")


# ─── Точный allowlist ────────────────────────────────────────────────


def parse_pilot_tenants(raw: str | None) -> frozenset[int]:
    """Парсит строку с tenant_id'ами в frozenset.

    Поддерживаемые форматы (разделитель — запятая, пробел или
    точка-с-запятой):
      - ``"352612382"``
      - ``"352612382,42"``
      - ``"[352612382, 42]"`` (JSON-подобное)
      - ``""`` или ``None`` → пустой frozenset

    Не валидные токены пропускаются с warning'ом в лог.
    """
    if not raw:
        return frozenset()
    out: set[int] = set()
    for token in _INT_RE.findall(raw):
        try:
            value = int(token)
        except ValueError:
            logger.warning("tenant_allowlist: пропускаю невалидный токен %r", token)
            continue
        if value <= 0:
            logger.warning("tenant_allowlist: пропускаю неположительный tenant_id %d", value)
            continue
        out.add(value)
    return frozenset(out)


def is_in_pilot(tenant_id: int, raw_allowlist: str | None) -> bool:
    """Тенант в точном allowlist'е?"""
    if not raw_allowlist:
        return False
    return tenant_id in parse_pilot_tenants(raw_allowlist)


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


def tenant_in_canary(tenant_id: int, percent: int) -> bool:
    """Тенант попадает в канарейку N%?

    Стабильный хеш: sha256(tenant_id) % 100 < percent. Один и тот же
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
    tenant_id: int,
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
