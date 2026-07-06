"""Admin auth via Telegram (#305) — server-side sessions + login challenges.

Two tables:

- ``admin_sessions`` — серверная admin-сессия для Telegram-пути. Кука несёт
  raw ``session_id`` (``secrets.token_urlsafe``); в БД только SHA-256 хеш.
  Личность = ``tg_id``: audit пишет ``admin_tg:<id>``, allowlist сверяется
  на КАЖДЫЙ request (отзыв гасит выданную сессию). Fallback ``SREDA_ADMIN_TOKEN``
  использует СТАРЫЙ HMAC-cookie механизм (не эту таблицу) — ротация токена
  гасит его автоматически.

- ``admin_login_challenges`` — короткоживущий challenge входа. Публичный
  ``challenge_id`` уходит в deep-link `t.me/<bot>?start=adm_<challenge_id>`
  (НЕ секрет). Секрет привязки = ОТДЕЛЬНАЯ кука ``browser_bind`` (в БД только
  ``browser_bind_hash``). Статус-машина pending→confirmed→consumed атомарными
  guarded-UPDATE (идемпотентность без пост-onboarding дедупа). ``button_send_claimed_at``
  = отдельный claim-слот: кнопку шлём ТОЛЬКО при выигранном claim (at-most-once,
  статус остаётся pending для confirm).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ADMIN_CHALLENGE_STATUSES = frozenset({"pending", "confirmed", "consumed"})


class AdminSession(Base):
    """Серверная admin-сессия (Telegram-путь). Кука = raw session_id; в БД хеш."""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        Index("ix_admin_sessions_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # SHA-256 hex от raw session_id (кука). Raw в БД не хранится.
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tg_id: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False, default="telegram")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminLoginChallenge(Base):
    """Один challenge входа: pending → confirmed (бот) → consumed (браузер)."""

    __tablename__ = "admin_login_challenges"
    __table_args__ = (
        Index("ix_admin_login_challenges_client_ip_created", "client_ip", "created_at"),
        Index("ix_admin_login_challenges_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Публичный хендл (уходит в Telegram start-param, ≤52 симв чтобы влезть в
    # 64-байтный лимит вместе с префиксом "adm_confirm:"). НЕ секрет.
    challenge_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # SHA-256 от секрет-куки browser_bind браузера-инициатора.
    browser_bind_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Короткий человекочитаемый код (анти-фишинг визуал, показывается в web И в Telegram).
    human_code: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Claim-слот отправки кнопки (at-most-once): кнопку шлём только при первом
    # выигранном UPDATE ... WHERE status='pending' AND button_send_claimed_at IS NULL.
    button_send_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    tg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
