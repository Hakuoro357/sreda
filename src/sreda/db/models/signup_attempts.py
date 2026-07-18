"""Signup abuse-control journal — HMAC'd source IDs, retention 30d.

Phase 1 of free-tier-subscription plan
(`plans/free-tier-subscription-stalled-r7.md`).

Зачем нужно:
- Per-source rate-limit (3 attempts / 24h per (channel, source_id))
  предотвращает массовое создание fake tenants на free tier.
- Глобальный committed-liability cap (`free_tier_active_max`) считает
  active free subscriptions; signup_attempts независимый journal для
  "попытался зарегаться, но был отклонён" cases.

PII handling (Sreda обезличивание policy):
- `source_id` (TG/MAX user_id) НЕ хранится в plaintext.
- Хранится `source_id_hash` = HMAC-SHA256(source_id, SREDA_SIGNUP_HASH_KEY)
  — equality-only retrievable, plaintext не восстановим.
- Retention: rows >30 дней удаляются daily cron
  (`scripts/cron/cleanup_signup_attempts.sh` — Phase 2 deploy).

Использование (Phase 2):
- `services/signup_abuse.py:SignupAbuseGuard.check_inside_tx()` —
  единый entry point для all signup paths (TG webhook, MAX webhook,
  mini-app lazy-provision).
- Caller owns transaction, guard acquires PG advisory lock на
  'sreda_signup_lock', does check + record + release lock на caller's
  commit (race-free).

Schema:
- `id` autoincrement int PK.
- `channel` 'telegram' | 'max'.
- `source_id_hash` HMAC-SHA256 (64-char hex).
- `attempted_at` timestamp (server default now).

Index: `(channel, source_id_hash, attempted_at DESC)` — оптимально
для "сколько попыток за 24h на этом source_id" query.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Channel keys.
CHANNEL_TELEGRAM = "telegram"
CHANNEL_MAX = "max"


class SignupAttempt(Base):
    __tablename__ = "signup_attempts"

    # Parity с миграцией 0041 (`ix_signup_attempts_lookup`) — rate-limit
    # COUNT `SignupAbuseGuard` идёт по (channel, source_id_hash) с окном по
    # attempted_at; без индекса в create_all-окружениях это полный скан
    # (дрейф модель↔миграция — audit 2026-07-18 db-migrations #3).
    __table_args__ = (
        Index(
            "ix_signup_attempts_lookup",
            "channel", "source_id_hash", "attempted_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # HMAC-SHA256(source_id, SREDA_SIGNUP_HASH_KEY) — 64 hex chars.
    source_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
