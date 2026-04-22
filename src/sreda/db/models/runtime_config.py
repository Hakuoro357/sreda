"""Generic key-value store for admin-toggleable runtime config.

Use cases as of 2026-04-22:
  * ``chat_primary_provider`` / ``chat_fallback_provider`` — live LLM
    switcher in /admin/llm so ops can move traffic off MiMo onto
    OpenRouter without a service restart.

Pattern is deliberately unopinionated: one row per setting key, no
schema enforcement on ``value``. Callers parse and validate. Keeps
migrations out of the loop — adding a new admin-togglable knob is
just a new key + a line in the UI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeConfig(Base):
    """Singleton-per-key config row that survives restarts and can be
    mutated live from the admin UI. Primary key is ``key`` so upserts
    are just ``session.merge(RuntimeConfig(key=..., value=...))``."""

    __tablename__ = "runtime_config"

    key = Column(String(64), primary_key=True)
    value = Column(String(256), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
