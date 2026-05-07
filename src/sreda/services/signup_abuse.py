"""SignupAbuseGuard — abuse control gate перед tenant creation.

Phase 2 of free-tier-subscription plan. Защищает от:
- (kill-switch) Manual freeze новых регистраций через RuntimeConfig.
- (free_tier_full) Глобальный committed-liability cap — сколько
  active free subscriptions может быть на проде. При hit-cap новые
  signups blocked.
- (rate_limit) Per-source rate limit: max 3 signups / 24h на (channel,
  HMAC(source_id)). Защищает от brute-force fake-account creation.

Использование (caller-owned tx):
    with session.begin():
        existing = find_tenant_by_telegram(session, source_id)
        if existing:
            return existing  # known tenant — no signup guard

        guard = SignupAbuseGuard(settings)
        allowed, reason = guard.check_inside_tx(session, "telegram", source_id)
        if not allowed:
            raise SignupBlocked(reason)
        tenant = create_tenant(session, ...)
        sub = create_sreda_free_subscription(session, tenant.id)
    return tenant

Дизайн (per Codex review round 4-5):
- **Caller owns transaction** — guard НЕ owns own session.begin().
  Caller's tx wraps guard + tenant + sub creation. Advisory lock
  держится до commit caller'а — race-free.
- **PG advisory_xact_lock(hashtext('sreda_signup_lock'))** —
  serializes ALL concurrent signups. Lock released на commit.
- **HMAC source_id** — raw TG/MAX user_id не stored (152-ФЗ).
- **`free_tier_active_max`** counts via subscription_plans.plan_key='sreda_free'
  AND status='active'. Defaults to 70 (Boris adjusts через
  RuntimeConfig).

Reasons (mapped к UPGRADE_COPY keys):
- 'signups_closed' — kill-switch активирован
- 'free_tier_full' — committed liability cap hit
- 'rate_limited' — 3+ attempts за 24h на этом source_id
- 'ok' — passed all checks, attempt recorded
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from sreda.config.settings import get_settings


logger = logging.getLogger(__name__)


# Per-source rate limit window
_RATE_WINDOW_HOURS = 24
_RATE_LIMIT_MAX = 3  # 3 attempts per source_id per 24h


class SignupBlocked(Exception):
    """Caller получает на rejection. Reason mapped к UPGRADE_COPY keys."""
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def hmac_signup_source(source_id: str | int) -> str:
    """HMAC-SHA256 hex digest от raw source_id под salt'ом из settings.

    Reuses `tg_account_salt` env var (per-deployment secret). Future
    upgrade: separate `SREDA_SIGNUP_HASH_KEY` для cryptographic isolation
    between tg_account_hash и signup_attempts. MVP shares the salt
    (one secret, simpler ops).
    """
    settings = get_settings()
    salt = (settings.tg_account_salt or "").strip()
    if not salt:
        raise RuntimeError(
            "SREDA_TG_ACCOUNT_SALT не задан — signup hash не сконфигурирован"
        )
    return hmac.new(
        salt.encode("utf-8"),
        str(source_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class SignupAbuseGuard:
    """Caller's responsibility: open tx wrapping guard + tenant +
    subscription creation. Guard acquires advisory lock и does
    checks/inserts inside caller's tx — lock released только on
    caller's commit, после tenant + sub inserted.
    """

    # Defaults — overridden via RuntimeConfig в production.
    DEFAULT_FREE_TIER_ACTIVE_MAX = 70
    DEFAULT_SIGNUP_OPEN = True

    def __init__(
        self,
        free_tier_active_max: int | None = None,
        signup_open: bool | None = None,
    ) -> None:
        """Args fall back to defaults; production caller pulls from
        RuntimeConfig service.
        """
        self.free_tier_active_max = (
            free_tier_active_max
            if free_tier_active_max is not None
            else self.DEFAULT_FREE_TIER_ACTIVE_MAX
        )
        self.signup_open = (
            signup_open if signup_open is not None
            else self.DEFAULT_SIGNUP_OPEN
        )

    def check_inside_tx(
        self,
        session: Session,
        channel: str,
        source_id: str | int,
    ) -> tuple[bool, str]:
        """Atomic checks + record под caller's tx.

        Returns:
            (True, 'ok') — passed all checks, signup_attempts row inserted.
            (False, reason) — rejected. Reason ∈ {'signups_closed',
            'free_tier_full', 'rate_limited'}.

        Acquires PG advisory_xact_lock (released on caller's commit).
        Caller responsible for raising SignupBlocked если reason != 'ok'.
        """
        # 1. Acquire advisory lock — serializes ALL concurrent signups.
        # PG only: SQLite single-writer model serializes by default.
        if session.bind.dialect.name == "postgresql":
            session.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('sreda_signup_lock'))"
            ))

        # 2. Kill-switch
        if not self.signup_open:
            return (False, "signups_closed")

        # 3. Committed liability cap (active free subscriptions count)
        active_count = session.execute(text("""
            SELECT COUNT(*) FROM tenant_subscriptions ts
            JOIN subscription_plans sp ON ts.plan_id = sp.id
            WHERE sp.plan_key = 'sreda_free'
              AND ts.status = 'active'
        """)).scalar()
        if (active_count or 0) >= self.free_tier_active_max:
            return (False, "free_tier_full")

        # 4. Per-source rate limit. Python-side timestamp avoids
        # PG INTERVAL vs SQLite datetime() syntax mismatch.
        source_id_hash = hmac_signup_source(source_id)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_RATE_WINDOW_HOURS)
        recent = session.execute(text("""
            SELECT COUNT(*) FROM signup_attempts
            WHERE channel = :c
              AND source_id_hash = :h
              AND attempted_at > :cutoff
        """), {"c": channel, "h": source_id_hash, "cutoff": cutoff}).scalar()
        if (recent or 0) >= _RATE_LIMIT_MAX:
            return (False, "rate_limited")

        # 5. Record attempt. Python-side timestamp ensures recorded
        # value matches что rate-limit query expects.
        session.execute(text("""
            INSERT INTO signup_attempts (channel, source_id_hash, attempted_at)
            VALUES (:c, :h, :ts)
        """), {
            "c": channel, "h": source_id_hash,
            "ts": datetime.now(timezone.utc),
        })

        return (True, "ok")
