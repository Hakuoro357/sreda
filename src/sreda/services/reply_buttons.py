"""ReplyButtonService — инлайн-кнопки для LLM-ответов.

Когда LLM отвечает через ``reply_with_buttons(text, buttons)``,
обработчик tool-call'а вызывает ``create_tokens`` чтобы получить массив
коротких токенов (по токену на кнопку), и строит inline_keyboard с
``callback_data="btn_reply:<token>"``.

Юзер жмёт кнопку → Telegram присылает callback_query → callback-handler
достаёт label через ``resolve_token`` и пробрасывает его как обычный
text-message в следующий chat-turn.

TTL: 1 час. Токены старше — протухли (``resolve_token`` вернёт None).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from sreda.db.models.reply_buttons import ReplyButtonCache

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 3600  # 1 час
TOKEN_LENGTH_HEX = 8  # 32 бита — пренебрежимо редкие коллизии на TTL 1h
MAX_BUTTONS_PER_REPLY = 4
MAX_LABEL_LENGTH = 128


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    """SQLite ``DateTime(timezone=True)`` возвращает naive datetime
    при чтении. Нормализуем к UTC-aware для корректного сравнения."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ReplyButtonService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_tokens(
        self,
        *,
        tenant_id: str,
        user_id: str,
        labels: list[str],
    ) -> list[tuple[str, str]]:
        """Выдаёт по токену на каждую label. Возвращает список пар
        ``[(token, label), ...]`` в том же порядке, что передан.

        Обрезает до ``MAX_BUTTONS_PER_REPLY``, укорачивает слишком
        длинные labels до ``MAX_LABEL_LENGTH`` (без исключения — просто
        усечение с многоточием, чтобы не падал турн из-за перегруза).
        """
        if not labels:
            return []

        now = _utcnow()
        clean: list[str] = []
        for label in labels[:MAX_BUTTONS_PER_REPLY]:
            if not label:
                continue
            stripped = label.strip()
            if not stripped:
                continue
            if len(stripped) > MAX_LABEL_LENGTH:
                stripped = stripped[: MAX_LABEL_LENGTH - 1] + "…"
            clean.append(stripped)

        result: list[tuple[str, str]] = []
        for label in clean:
            token = secrets.token_hex(TOKEN_LENGTH_HEX // 2)  # 8 hex chars
            row = ReplyButtonCache(
                token=token,
                tenant_id=tenant_id,
                user_id=user_id,
                label=label,
                created_at=now,
                used_at=None,
            )
            self.session.add(row)
            result.append((token, label))

        if result:
            # COMMIT, не просто flush. Без commit'а токены пропадают
            # если caller-route закрывается без своего commit'а
            # (наблюдалось 2026-04-25: «Может, позже» в welcome после
            # approval → callback → resolve_token=None → "Выбор устарел").
            self.session.commit()
        return result

    def resolve_token(
        self,
        *,
        tenant_id: str,
        user_id: str,
        token: str,
    ) -> str | None:
        """Возвращает label по токену, если:
        - токен существует,
        - принадлежит тому же (tenant, user),
        - младше TTL,
        - ещё не использован (used_at IS NULL).

        При успехе — помечает ``used_at`` (защита от повторного клика).
        Иначе возвращает None.
        """
        row = self.session.get(ReplyButtonCache, token)
        if row is None:
            return None

        # Tenant/user mismatch — подозрительно, тихо отказываем.
        if row.tenant_id != tenant_id or row.user_id != user_id:
            logger.warning(
                "btn_reply token %s accessed by wrong owner "
                "(expected tenant=%s user=%s, got tenant=%s user=%s)",
                token, row.tenant_id, row.user_id, tenant_id, user_id,
            )
            return None

        # Уже использован — повторный клик, no-op.
        if row.used_at is not None:
            return None

        # Проверка TTL.
        age = (_utcnow() - _coerce_utc(row.created_at)).total_seconds()
        if age > TOKEN_TTL_SECONDS:
            return None

        row.used_at = _utcnow()
        self.session.flush()
        return row.label

    def purge_expired(self, *, older_than_hours: int = 24) -> int:
        """Удаляет протухшие токены. Вызывается из scheduled-worker
        раз в сутки (не блокирует работу, просто gc). Возвращает
        кол-во удалённых строк."""
        cutoff = _utcnow() - timedelta(hours=older_than_hours)
        n = (
            self.session.query(ReplyButtonCache)
            .filter(ReplyButtonCache.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        if n:
            self.session.commit()
        return n
