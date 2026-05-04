"""Channel linking service — deep-link + mini-app consume flow.

KNOWN LIMITATION (Phase 11 deploy 2026-05-04, follow-up sprint):
Backend endpoints (`/api/v1/channel-link/{start,consume,status}`) готовы,
но **mini-app frontend UI для linking flow ещё не написан**. Юзер может
тапнуть deep-link → откроется target-side mini-app, но без UI кнопки
«Подтвердить связь» frontend не вызовет `/consume`. Линковка не
произойдёт автоматически — это **намеренная безопасная default**:
deep-link interception сам по себе не приводит к linking без явного
user-tap'а в mini-app frontend.

Mitigations в этой версии:
- TTL 5 min на токены (R5 hardening)
- Audit log в admin chat при start/consume/collision
- Atomic single-use UPDATE (race-safe)
- 256-bit token + SHA-256 hash at rest

Follow-up TODO (next sprint):
- Mini-app UI на source-side: button «Привязать MAX» / «Привязать TG»
- Mini-app UI на target-side: detect start_param=lnk_* → confirm screen
  с именем source-аккаунта → tap → POST /consume
- Frontend rate-limit ack handling (429 от backend)
Phase 7 of MAX integration plan.

Flow: User в mini-app source-side кликает «Привязать MAX» →
``start_link()`` создаёт ChannelLinkToken → mini-app получает raw token
+ deep-link для target-side → юзер тапает deep-link → MAX открывает
target-side mini-app с initData.start_param=lnk_<token> → frontend
показывает confirm UI → user-tap → POST /channel-link/consume →
``consume_link()`` атомарно UPDATE used_at + linkует target_account_id
к existing tenant.

Security (R5 hardening):
- Raw token: ``secrets.token_urlsafe(32)`` = 256-bit. Never persisted —
  только хеш через SHA-256.
- TTL 5 min.
- Single-use atomic consume (UPDATE WHERE used_at IS NULL RETURNING).
- Rate-limit 5 successful starts / 30 min per tenant — реализуется
  COUNT-query на channel_link_tokens.
- Audit log в admin chat для всех start/consume/collision events.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from sreda.db.models.channel_linking import LINK_CHANNELS, ChannelLinkToken
from sreda.db.models.core import Tenant, User

logger = logging.getLogger(__name__)


TOKEN_TTL_MINUTES = 5
RATE_LIMIT_MAX = 5  # successful starts per tenant per window
RATE_LIMIT_WINDOW_MINUTES = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    """Coerce stored datetime to UTC-aware. SQLite strips tzinfo on
    round-trip — без этого `<` / `>` сравнения с now() бросают
    TypeError. Postgres хранит timezone-aware — функция no-op."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _id() -> str:
    return f"link_{uuid4().hex[:24]}"


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StartLinkResult:
    """Outcome of ``start_link()`` — what mini-app frontend gets."""

    id: str
    raw_token: str         # only живёт в response → URL → user-side
    deep_link: str
    target_channel: str
    expires_at: datetime


# Deep-link templates per target channel.
# Phase 0 probe lock-in: bot username `id320700072280_bot` для MAX.
# TG bot username берём из настроек (для URL t.me/<name>).
_MAX_BOT_USERNAME = "id320700072280_bot"


def _build_deep_link(target_channel: str, raw_token: str, *, tg_bot_username: str | None) -> str:
    """Build platform-specific deep-link with start_param."""
    if target_channel == "max":
        return f"https://max.ru/{_MAX_BOT_USERNAME}?startapp=lnk_{raw_token}"
    if target_channel == "telegram":
        if not tg_bot_username:
            raise ValueError("tg_bot_username required for target=telegram")
        return f"https://t.me/{tg_bot_username}?start=lnk_{raw_token}"
    raise ValueError(f"unknown target_channel: {target_channel!r}")


class ChannelLinkRateLimitedError(Exception):
    """Tenant exceeded RATE_LIMIT_MAX successful starts in window."""


def _count_recent_starts(session: Session, tenant_id: str) -> int:
    """Count successful start_link calls для tenant в последнем окне.

    «Successful» = row created. Race-fails count too — mitigation против abuse.
    """
    window_start = _utcnow() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    return session.execute(
        select(func.count(ChannelLinkToken.id))
        .where(
            ChannelLinkToken.tenant_id == tenant_id,
            ChannelLinkToken.created_at >= window_start,
        )
    ).scalar_one()


def start_link(
    session: Session,
    *,
    tenant_id: str,
    source_channel: str,
    tg_bot_username: str | None = None,
) -> StartLinkResult:
    """Generate a new linking token. Source = current channel (where mini-app
    is opened); target = the opposite.

    Raises:
        ValueError on unknown source_channel
        ChannelLinkRateLimitedError если tenant превысил quota
    """
    if source_channel not in LINK_CHANNELS:
        raise ValueError(f"unknown source_channel: {source_channel!r}")

    target_channel = "max" if source_channel == "telegram" else "telegram"

    if _count_recent_starts(session, tenant_id) >= RATE_LIMIT_MAX:
        raise ChannelLinkRateLimitedError(
            f"tenant {tenant_id} exceeded {RATE_LIMIT_MAX} starts "
            f"per {RATE_LIMIT_WINDOW_MINUTES}min window"
        )

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    now = _utcnow()
    expires_at = now + timedelta(minutes=TOKEN_TTL_MINUTES)

    row = ChannelLinkToken(
        id=_id(),
        tenant_id=tenant_id,
        source_channel=source_channel,
        target_channel=target_channel,
        token_hash=token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(row)
    session.commit()

    deep_link = _build_deep_link(target_channel, raw_token, tg_bot_username=tg_bot_username)

    return StartLinkResult(
        id=row.id,
        raw_token=raw_token,
        deep_link=deep_link,
        target_channel=target_channel,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    """Result of consume_link()."""

    success: bool
    error: str | None = None        # 'expired' / 'used' / 'not_found' / 'collision' / 'wrong_channel'
    tenant_id: str | None = None    # set on success
    source_channel: str | None = None
    target_channel: str | None = None


def lookup_token(session: Session, raw_token: str) -> ChannelLinkToken | None:
    """Find row by raw token (хешим перед запросом).

    Returns None если не найден ИЛИ expired ИЛИ used. Caller использует
    это для предварительной проверки до показа confirm-button.
    """
    token_hash = _hash_token(raw_token)
    row = session.execute(
        select(ChannelLinkToken).where(ChannelLinkToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None:
        return None
    if _coerce_utc(row.expires_at) < _utcnow():
        return None
    if row.used_at is not None:
        return None
    return row


def consume_link(
    session: Session,
    *,
    raw_token: str,
    target_channel: str,
    target_account_id: str,
) -> ConsumeOutcome:
    """Atomically consume token AND link target_account_id к tenant.

    Args:
        raw_token: opaque token from deep-link or callback_data.
        target_channel: канал в котором юзер сейчас (тот же что target_channel в БД row).
        target_account_id: account_id юзера в target_channel (max или telegram id).

    Atomic SQL: ``UPDATE ... SET used_at=now() WHERE token_hash=? AND used_at
    IS NULL AND expires_at > now() RETURNING *``. Race-safe — даже если 2
    параллельных consume пришли одновременно, only один success.

    Также проверяется collision: если у юзера уже есть отдельный tenant
    с этим account_id в target канале — abort с error='collision'. Юзер
    должен через support манульно мерджить.
    """
    token_hash = _hash_token(raw_token)

    # Atomic atomic consume — RETURNING строки если matched.
    stmt = (
        update(ChannelLinkToken)
        .where(
            ChannelLinkToken.token_hash == token_hash,
            ChannelLinkToken.used_at.is_(None),
            ChannelLinkToken.expires_at > _utcnow(),
        )
        .values(used_at=_utcnow())
        .returning(ChannelLinkToken)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        # Distinguish error category для logs/admin alert.
        existing = session.execute(
            select(ChannelLinkToken).where(ChannelLinkToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if existing is None:
            return ConsumeOutcome(success=False, error="not_found")
        if existing.used_at is not None:
            return ConsumeOutcome(success=False, error="used")
        if _coerce_utc(existing.expires_at) <= _utcnow():
            return ConsumeOutcome(success=False, error="expired")
        return ConsumeOutcome(success=False, error="not_found")

    # Verify target_channel matches expected (защита от replay token из
    # другого направления).
    if row.target_channel != target_channel:
        # Atomic UPDATE уже произошёл, откатываем
        session.rollback()
        return ConsumeOutcome(success=False, error="wrong_channel")

    # Collision detection: target_account_id уже привязан к ДРУГОМУ tenant?
    other_user = session.execute(
        select(User).where(
            (User.max_account_id == target_account_id)
            if target_channel == "max"
            else (User.telegram_account_id == target_account_id)
        )
    ).scalar_one_or_none()

    if other_user is not None and other_user.tenant_id != row.tenant_id:
        # Существующий аккаунт в target канале → конфликт.
        # Откатываем UPDATE used_at чтобы юзер мог попробовать ещё раз
        # после manual support resolution.
        session.rollback()
        return ConsumeOutcome(
            success=False, error="collision",
            tenant_id=row.tenant_id,
        )

    # Найти/создать User в этом target канале для tenant'а.
    # Source-tenant уже имеет одного юзера (создан при первом сообщении в
    # source channel). Мы добавляем target account_id к этому же User row
    # (если есть user в этом tenant'е) или создаём нового User row.
    tenant_user = session.execute(
        select(User).where(
            User.tenant_id == row.tenant_id,
            (User.max_account_id.is_(None) if target_channel == "max"
             else User.telegram_account_id.is_(None)),
        )
    ).scalar_one_or_none()

    if tenant_user is not None:
        # Update existing user — добавляем target account_id рядом.
        if target_channel == "max":
            tenant_user.max_account_id = target_account_id
        else:
            tenant_user.telegram_account_id = target_account_id
    # Если other_user None и tenant_user None — strange state; пусть
    # caller (handle_*_update) создаёт нового User через
    # ensure_*_user_bundle с tenant_id. Возвращаем success — линкование
    # на уровне tokens прошло, остальное — наверху.

    session.commit()
    return ConsumeOutcome(
        success=True,
        tenant_id=row.tenant_id,
        source_channel=row.source_channel,
        target_channel=row.target_channel,
    )


def cleanup_expired_tokens(session: Session) -> int:
    """Delete tokens где expires_at < now() - 1 day. Returns count deleted.

    Запускается из 6h health-check job (Phase 9). Mandatory не optional —
    иначе аккумулируются неиспользуемые tokens.
    """
    cutoff = _utcnow() - timedelta(days=1)
    result = session.execute(
        ChannelLinkToken.__table__.delete().where(
            ChannelLinkToken.expires_at < cutoff
        )
    )
    session.commit()
    return result.rowcount or 0
