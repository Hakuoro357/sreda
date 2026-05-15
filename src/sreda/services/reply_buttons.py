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

from sqlalchemy import update
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

        При успехе — атомарно помечает ``used_at`` через single conditional
        UPDATE RETURNING. Concurrent callbacks для one token → only first
        gets label, остальные None (idempotent — same button нельзя
        обработать дважды).

        R-22 (2026-05-15): R-18 r2 fixed `flush()` → `commit()` row-lock leak,
        но оставил TOCTOU race window между SELECT и UPDATE:
          1. Thread A SELECT: used_at=NULL → проходит check
          2. Thread B SELECT: used_at=NULL → проходит check (parallel)
          3. Thread A UPDATE used_at=now → commit
          4. Thread B UPDATE used_at=now → commit (override) → returns label
        Both calls fire downstream action (duplicate save / mutation).
        Fix: single conditional UPDATE WHERE used_at IS NULL RETURNING label.
        DB-level atomic — exactly one caller gets label.

        Tradeoff: lost wrong-owner WARN log (нет SELECT step). Mitigation:
        emit debug log on None result with raw existence check (read-only,
        no lock). Frequency низкая (нормальный flow юзер жмёт свою кнопку),
        debug visibility достаточна.
        """
        now = _utcnow()
        ttl_cutoff = now - timedelta(seconds=TOKEN_TTL_SECONDS)
        stmt = (
            update(ReplyButtonCache)
            .where(ReplyButtonCache.token == token)
            .where(ReplyButtonCache.tenant_id == tenant_id)
            .where(ReplyButtonCache.user_id == user_id)
            .where(ReplyButtonCache.used_at.is_(None))
            .where(ReplyButtonCache.created_at >= ttl_cutoff)
            .values(used_at=now)
            .returning(ReplyButtonCache.label)
        )
        result = self.session.execute(stmt)
        row = result.first()
        # COMMIT обязательно (R-18 fix не утрачен — UPDATE держит row lock
        # до commit'а; без commit'а row lock leak return'нётся).
        self.session.commit()
        if row is None:
            # Either: token doesn't exist / wrong owner / already used / expired.
            # All — return None. Optional diagnostic (debug-level, single SELECT,
            # read-only, no lock) для wrong-owner pattern detection.
            self._log_resolve_miss(tenant_id, user_id, token)
            return None
        return row.label

    def _log_resolve_miss(self, tenant_id: str, user_id: str, token: str) -> None:
        """R-22: read-only diagnostic для resolve_token miss. Logs why
        a token didn't match: missing / wrong-owner / already used / expired.

        Debug-level — wrong-owner случаи редкие в normal flow. Не делает
        UPDATE, не держит row lock. Best-effort: exceptions swallowed.

        Codex+Xiaomi R1 MAJOR/LOW: ``session.get()`` после commit'а открывает
        new implicit transaction. Без явного ``rollback()`` connection
        остаётся "idle in transaction" — R-18 incident class. Wrap в
        try/finally с rollback для гарантированного transaction closure.
        """
        try:
            existing = self.session.get(ReplyButtonCache, token)
            if existing is None:
                logger.debug(
                    "btn_reply resolve miss: token %s does not exist", token,
                )
            elif existing.tenant_id != tenant_id or existing.user_id != user_id:
                # Codex+Xiaomi R1 MINOR: log convention — expected = row's
                # actual owner (from DB), got = caller's claim (intruder).
                logger.warning(
                    "btn_reply token %s accessed by wrong owner "
                    "(expected tenant=%s user=%s, got tenant=%s user=%s)",
                    token,
                    existing.tenant_id, existing.user_id,
                    tenant_id, user_id,
                )
            elif existing.used_at is not None:
                logger.debug(
                    "btn_reply resolve miss: token %s already used at %s",
                    token, existing.used_at,
                )
            else:
                logger.debug(
                    "btn_reply resolve miss: token %s expired (age=%ds)",
                    token,
                    int((_utcnow() - _coerce_utc(existing.created_at)).total_seconds()),
                )
        except Exception:  # noqa: BLE001 — diagnostic must not crash caller
            logger.exception("btn_reply diagnostic lookup failed (token=%s)", token)
        finally:
            # Codex R1 MAJOR / Xiaomi R1 #2: implicit read-only tx opened by
            # session.get() must be closed. Rollback (not commit — nothing to
            # write) releases connection without R-18-style idle-in-tx leak.
            try:
                self.session.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("btn_reply diagnostic rollback failed")

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
