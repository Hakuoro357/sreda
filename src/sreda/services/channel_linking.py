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
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from sreda.db.models.audit import AuditLog
from sreda.db.models.channel_linking import LINK_CHANNELS, ChannelLinkToken
from sreda.db.models.core import User
from sreda.services.tg_account_hash import hash_tg_account

logger = logging.getLogger(__name__)


TOKEN_TTL_MINUTES = 5
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_MINUTES = 30
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


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


def _validate_token_format(raw_token: str) -> None:
    """Raise ValueError if token не matches alphabet/length."""
    if not isinstance(raw_token, str) or not TOKEN_PATTERN.fullmatch(raw_token):
        raise ValueError("invalid token format")


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


def _build_deep_link(
    target_channel: str,
    raw_token: str,
    *,
    tg_bot_username: str | None,
    tg_miniapp_shortname: str | None,
) -> str:
    """Build platform-specific deep-link with start_param.

    For MAX target: use `?start=lnk_X` (chat-based) instead of
    `?startapp=lnk_X` (mini-app). Bot's mini-app registration in MAX
    causes mini-app launch when `?startapp=` used, but mini-app initData
    delivery of start_param is unreliable. Chat-based: bot receives
    /start command with payload, sends inline confirm button. Mirror
    TG-style chat flow."""
    if target_channel == "max":
        return f"https://max.ru/{_MAX_BOT_USERNAME}?start=lnk_{raw_token}"
    if target_channel == "telegram":
        if not tg_bot_username or not tg_miniapp_shortname:
            raise ValueError(
                "tg_bot_username + tg_miniapp_shortname required for target=telegram"
            )
        return (
            f"https://t.me/{tg_bot_username}/{tg_miniapp_shortname}"
            f"?startapp=lnk_{raw_token}"
        )
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
    source_user_id: str,
    tg_bot_username: str | None = None,
    tg_miniapp_shortname: str | None = None,
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
    deep_link = _build_deep_link(
        target_channel,
        raw_token,
        tg_bot_username=tg_bot_username,
        tg_miniapp_shortname=tg_miniapp_shortname,
    )

    row = ChannelLinkToken(
        id=_id(),
        tenant_id=tenant_id,
        source_channel=source_channel,
        target_channel=target_channel,
        source_user_id=source_user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(row)
    session.commit()

    return StartLinkResult(
        id=row.id,
        raw_token=raw_token,
        deep_link=deep_link,
        target_channel=target_channel,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    """Result of consume_link().

    Errors:
        'not_found_or_expired' | 'missing_chat_id' |
        'source_user_not_found' | 'already_linked_other_account' |
        'account_already_registered_separately' |
        'account_belongs_to_other_family_member' | 'invalid_token_format'
    """

    success: bool
    error: str | None = None
    tenant_id: str | None = None    # set on success
    source_channel: str | None = None
    target_channel: str | None = None
    idempotent: bool = False


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
    target_chat_id: str | None = None,
) -> ConsumeOutcome:
    """MVP happy-path. No destructive merge."""
    _validate_token_format(raw_token)
    token_hash = _hash_token(raw_token)
    now = _utcnow()

    stmt = (
        update(ChannelLinkToken)
        .where(
            ChannelLinkToken.token_hash == token_hash,
            ChannelLinkToken.used_at.is_(None),
            ChannelLinkToken.expires_at > now,
            ChannelLinkToken.target_channel == target_channel,
            ChannelLinkToken.source_user_id.is_not(None),
        )
        .values(used_at=now)
        .returning(ChannelLinkToken)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return ConsumeOutcome(success=False, error="not_found_or_expired")

    if target_channel == "max" and not target_chat_id:
        session.rollback()
        return ConsumeOutcome(success=False, error="missing_chat_id")

    source_user = session.execute(
        select(User)
        .where(User.id == row.source_user_id)
        .with_for_update()
    ).scalar_one_or_none()
    if source_user is None:
        session.rollback()
        return ConsumeOutcome(success=False, error="source_user_not_found")

    source_tenant = source_user.tenant_id
    incoming_target = str(target_account_id)
    existing_target = (
        source_user.max_account_id
        if target_channel == "max"
        else source_user.telegram_account_id
    )
    if existing_target:
        if str(existing_target) == incoming_target:
            session.commit()
            return ConsumeOutcome(
                success=True,
                tenant_id=source_tenant,
                source_channel=row.source_channel,
                target_channel=row.target_channel,
                idempotent=True,
            )
        session.rollback()
        return ConsumeOutcome(success=False, error="already_linked_other_account")

    if target_channel == "max":
        collision_filter = User.max_account_id == incoming_target
    elif target_channel == "telegram":
        collision_filter = User.tg_account_hash == hash_tg_account(incoming_target)
    else:
        session.rollback()
        return ConsumeOutcome(success=False, error="not_found_or_expired")

    other_user = session.execute(
        select(User).where(
            User.id != source_user.id,
            collision_filter,
        )
    ).scalar_one_or_none()

    if other_user is not None and other_user.tenant_id != source_tenant:
        session.rollback()
        return ConsumeOutcome(
            success=False,
            error="account_already_registered_separately",
            tenant_id=source_tenant,
        )

    if other_user is not None and other_user.tenant_id == source_tenant:
        session.rollback()
        return ConsumeOutcome(
            success=False,
            error="account_belongs_to_other_family_member",
        )

    if target_channel == "max":
        source_user.max_account_id = incoming_target
        source_user.max_chat_id = str(target_chat_id)
    else:
        source_user.telegram_account_id = incoming_target

    session.add(AuditLog(
        id=f"al_{uuid4().hex[:24]}",
        actor_type="user",
        actor_id=row.source_user_id,
        action="channel_link.attached",
        resource_type="tenant",
        resource_id=source_user.tenant_id,
        metadata_json=json.dumps({
            "target_channel": target_channel,
            "target_account_id_present": True,
            "target_chat_id_present": target_chat_id is not None,
            "token_id": row.id,
        }),
    ))

    session.commit()
    return ConsumeOutcome(
        success=True,
        tenant_id=source_tenant,
        source_channel=row.source_channel,
        target_channel=row.target_channel,
    )


def is_account_already_linked(
    session: Session,
    *,
    tenant_id: str,
    target_channel: str,
) -> bool:
    """Check if any user in this tenant already has the target channel linked.

    Server-side guard for /start — UI hide is insufficient; the backend
    must enforce to prevent redundant linking attempts.

    Args:
        session: SQLAlchemy session.
        tenant_id: The tenant to check.
        target_channel: ``"max"`` or ``"telegram"``.

    Returns:
        True if at least one user in the tenant has the target account
        already linked.
    """
    if target_channel not in LINK_CHANNELS:
        raise ValueError(f"unknown target_channel: {target_channel!r}")
    col = User.max_account_id if target_channel == "max" else User.telegram_account_id
    exists = session.execute(
        select(User.id)
        .where(User.tenant_id == tenant_id, col.is_not(None))
        .limit(1)
    ).first()
    return exists is not None


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
