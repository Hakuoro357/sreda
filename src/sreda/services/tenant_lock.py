"""Per-tenant async lock — sequentializes inbound turn processing.

Shared by ``services.telegram_inbound`` and ``services.max_inbound``.
Originally lived inside ``telegram_inbound`` but был выделен 2026-05-05
после code review (R1 MAJOR — fragile cross-module private import).

Why this exists (incident user_tg_755682022, 2026-04-30):
Юзер шлёт 4 voice messages подряд (4 update'а за 150мс). Без lock'а
4 background-task'а параллельно конкурируют за SQLite/PG write на одну
tenant row → 30-90с залипы на add_checklist_items / create_checklist.
Lock обеспечивает последовательную обработку: первый turn run, 2-4 ждут
в очереди.

Cross-channel implication: если юзер связал TG+MAX через
``channel_link_tokens`` (sprint 2026-05-04) и шлёт одновременно в обе
channel'а — оба inbound'а обрабатываются sequentially т.к. берут lock
по одному ``tenant_id``. Это намеренно: conversation context coherency
важнее latency parallel'ной обработки.

Threading note: dict access без `asyncio.Lock` обёртки — допустимо
потому что dict[set]/get в Python 3.12 atomic для primitive keys, и мы
работаем в single event loop'е. Если в будущем добавим multi-loop
deployment — потребуется `_LOCK_REGISTRY_GUARD = asyncio.Lock()`.
"""

from __future__ import annotations

import asyncio


_TENANT_LOCKS: dict[str, asyncio.Lock] = {}


def get_tenant_lock(tenant_id: str) -> asyncio.Lock:
    """Return the singleton lock for ``tenant_id``. Creates lazily.

    Locks live for process lifetime (no GC) — bounded by tenant count,
    в практике ~1k tenants × ~64-byte Lock = ~64KB, negligible.
    """
    lock = _TENANT_LOCKS.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _TENANT_LOCKS[tenant_id] = lock
    return lock


def reset_for_tests() -> None:
    """Clear the lock registry. Use ONLY in pytest fixtures.

    Bare `_TENANT_LOCKS = {}` re-binding не работает потому что callers
    держат ссылку на старый dict через `from .tenant_lock import _TENANT_LOCKS`.
    Делаем clear() чтобы те же ссылки видели пустой состояние.
    """
    _TENANT_LOCKS.clear()
