"""#138 Ф5-5b: резолв внешней идентичности через DEFINER-функцию (identity-роль).

Единственный законный способ identity-фазы ответить «кто это?»: EXECUTE
``sreda_resolve_external_identity`` (0078, v2 c ``match_count`` — 0083). Прямых
SELECT на users/tenants у identity-роли нет (и не будет — приёмка #3: identity
не перечисляет чужое). До флипа DSN (Ф5) privileged_session фолбэкает на общий
движок — поведение то же, инертно.

Семантика match_count:
- 0 → None (юзер неизвестен → путь провижна);
- 1 → :class:`ResolvedIdentity`;
- >1 → :class:`AmbiguousExternalIdentity` (сегодня возможно только для MAX:
  ``max_account_id`` не уникален; tg_account_hash — unique). Вызывающий обязан
  ОТКАЗАТЬ (как прежний ``MultipleResultsFound``-путь), НЕ провижнить дубль.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from sreda.services.tg_account_hash import hash_tg_account

logger = logging.getLogger(__name__)

_CHANNELS = ("telegram", "max")

_RESOLVE_SQL = text(
    "SELECT tenant_id, user_id, approved_at, tenant_deleted_at, match_count "
    "FROM sreda_resolve_external_identity(:c, :k)"
)


class AmbiguousExternalIdentity(Exception):
    """Внешний id матчит >1 юзера — привязка запрещена (fail-closed)."""

    def __init__(self, channel: str, match_count: int) -> None:
        super().__init__(f"ambiguous external identity: channel={channel} n={match_count}")
        self.channel = channel
        self.match_count = match_count


@dataclass(frozen=True)
class ResolvedIdentity:
    tenant_id: str
    user_id: str
    approved_at: datetime | None
    tenant_deleted_at: datetime | None

    @property
    def tenant_active(self) -> bool:
        """Гейт soft-delete без отдельного SELECT tenants (данные из резолва)."""
        return self.tenant_deleted_at is None


def resolve_external_identity(
    channel: str,
    external_key: str | int,
    *,
    session=None,
) -> ResolvedIdentity | None:
    """Резолв «канал + внешний id → (tenant, user)» под identity-ролью.

    ``external_key`` — СЫРОЙ идентификатор канала (tg chat_id / max user_id);
    хеширование для telegram (152-ФЗ) выполняется здесь, чтобы ни один вызывающий
    не смог случайно искать по plaintext.

    ``session`` — только для юнит-инъекции; прод-путь открывает
    ``privileged_session("identity-resolve")`` сам.
    """
    if channel not in _CHANNELS:
        raise ValueError(f"resolve_external_identity: unknown channel {channel!r}")
    key = hash_tg_account(external_key) if channel == "telegram" else str(external_key)

    if session is not None:
        return _resolve_with(session, channel, key)
    from sreda.db.session import privileged_session

    with privileged_session("identity-resolve") as s:
        return _resolve_with(s, channel, key)


def _resolve_with(session, channel: str, key: str) -> ResolvedIdentity | None:
    # SQLite (юнит-БД) не имеет DEFINER-функции (она PG-only, миграция 0083
    # guard-skip'ает не-Postgres) → прямой ORM-lookup с той же семантикой
    # (n=0 None, n=1 данные, n>1 Ambiguous). На PG — только EXECUTE функции.
    if session.bind.dialect.name != "postgresql":
        return _resolve_direct(session, channel, key)
    rows = session.execute(_RESOLVE_SQL, {"c": channel, "k": key}).all()
    if not rows:
        return None
    tenant_id, user_id, approved_at, tenant_deleted_at, match_count = rows[0]
    if match_count != 1:
        logger.warning("identity resolve ambiguous: channel=%s n=%s", channel, match_count)
        raise AmbiguousExternalIdentity(channel, int(match_count))
    return ResolvedIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        approved_at=approved_at,
        tenant_deleted_at=tenant_deleted_at,
    )


def _resolve_direct(session, channel: str, key: str) -> ResolvedIdentity | None:
    """SQLite-fallback: прямой lookup (юнит-БД, DEFINER недоступен). ``key`` уже
    захеширован для telegram вызывающим ``resolve_external_identity``."""
    from sreda.db.models.core import Tenant, User

    q = session.query(User)
    q = (
        q.filter(User.tg_account_hash == key)
        if channel == "telegram"
        else q.filter(User.max_account_id == key)
    )
    users = q.all()
    if not users:
        return None
    if len(users) > 1:
        raise AmbiguousExternalIdentity(channel, len(users))
    u = users[0]
    t = session.get(Tenant, u.tenant_id)
    return ResolvedIdentity(
        tenant_id=u.tenant_id,
        user_id=u.id,
        approved_at=t.approved_at if t is not None else None,
        tenant_deleted_at=t.deleted_at if t is not None else None,
    )
