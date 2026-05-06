"""Channel linking tokens — cross-channel account linking flow.

Phase 7 of MAX integration. Юзер уже зарегистрирован в одном канале
(TG или MAX) и хочет связать второй канал → mini-app генерирует opaque
256-bit token, кладёт хешированный (SHA-256) в эту таблицу + URL-deep-link
с raw token. Юзер открывает deep-link в target мессенджере → бот видит
``start_param=lnk_<token>`` → подтверждает inline-кнопкой → сервер делает
atomic UPDATE used_at и линкует account_id к existing tenant.

Security:
- Raw token: ``secrets.token_urlsafe(32)`` = 256-bit. Только в URL и
  response к mini-app frontend; в БД — only hash.
- Single-use: atomic consume через UPDATE WHERE used_at IS NULL RETURNING.
- TTL: 5 min (Risks #11, R5 hardening).
- Rate-limit: 5 successful starts / 30 min per tenant — реализуется
  COUNT-query на этой таблице, не отдельная инфра.
- Audit: все start/consume events идут в admin Telegram chat через
  ``alert_admin_async``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Допустимые значения source/target_channel.
LINK_CHANNELS = frozenset({"telegram", "max"})


class ChannelLinkToken(Base):
    """One pending or consumed channel-linking attempt.

    Lifecycle:
      * ``start_link()`` создаёт row, ``used_at=None``, ``expires_at=now+5min``
      * Mini-app получает raw token (1 раз), показывает deep-link
      * ``consume_link()`` атомарно UPDATE ``used_at=now()`` WHERE
        ``used_at IS NULL`` — успех если RETURNING вернул row
      * Cleanup job (Phase 9) DELETE'ит rows where ``expires_at <
        now() - 1 day``
    """

    __tablename__ = "channel_link_tokens"
    __table_args__ = (
        Index(
            "ix_channel_link_tokens_active",
            "tenant_id", "expires_at", "used_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    source_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    target_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    source_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )

    # SHA-256 hex digest от raw token. Raw token уходит ТОЛЬКО в URL +
    # response, в БД не кладётся — даже при leak'е DB атакующий не
    # сможет hijack pending links.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
