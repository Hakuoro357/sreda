"""Thin service layer over ``RuntimeConfig`` — key-value reads/writes
used by admin UIs for live-togglable settings (chat-LLM switcher,
etc). Callers parse and validate the raw string value themselves.

The module keeps a tiny in-process TTL cache because turn-time code
(``get_chat_llm``) calls ``get()`` on every chat turn — a 5-second
cache saves dozens of round-trips per busy minute while still letting
an admin edit take effect almost immediately (worst case ~5s lag).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from sqlalchemy.orm import Session

from sreda.db.models.runtime_config import RuntimeConfig


_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[str | None, float]] = {}
_cache_lock = threading.Lock()


def _read_cached(key: str, loader: Callable[[], str | None]) -> str | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and (now - hit[1]) < _CACHE_TTL_SECONDS:
            return hit[0]
    # Load outside the lock — loader hits the DB.
    value = loader()
    with _cache_lock:
        _cache[key] = (value, now)
    return value


def get_config(session: Session, key: str) -> str | None:
    """Current value for ``key`` or ``None`` if never set."""

    def _load() -> str | None:
        row = session.get(RuntimeConfig, key)
        return row.value if row else None

    return _read_cached(key, _load)


def set_config(session: Session, key: str, value: str | None) -> None:
    """Upsert ``key=value``. Passing ``None`` deletes the row so
    ``get_config`` falls back to whatever default the caller applies.
    Commits inside — admin actions are discrete."""
    row = session.get(RuntimeConfig, key)
    if value is None:
        if row is not None:
            session.delete(row)
            session.commit()
        _invalidate(key)
        return
    if row is None:
        session.add(RuntimeConfig(key=key, value=value))
    else:
        row.value = value
    session.commit()
    _invalidate(key)


def _invalidate(key: str) -> None:
    with _cache_lock:
        _cache.pop(key, None)


def invalidate_cache() -> None:
    """Drop every cached entry — test fixtures and hot reloads want
    this to avoid cross-test leakage."""
    with _cache_lock:
        _cache.clear()


# Canonical keys — keep here so callers don't typo them.
KEY_CHAT_PROVIDER = "chat_primary_provider"
KEY_CHAT_FALLBACK_PROVIDER = "chat_fallback_provider"
