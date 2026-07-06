"""Admin login via Telegram (#305) — сервис сессий и challenge-потока.

Фаза 2 (здесь сейчас): серверные admin-сессии — mint/resolve/revoke по хешу.
Фаза 3 добавит challenge-поток (start/attach/confirm/claim/cleanup).

Все секреты (session_id, browser_bind) — raw только в куке; в БД SHA-256 хеш.
Переходы challenge — атомарные guarded-UPDATE (идемпотентность).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from sreda.db.models.admin_auth import AdminSession

SESSION_TTL_HOURS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    """SQLite стрипает tzinfo на round-trip → сравнения с now() бросают TypeError.
    Postgres хранит aware — no-op."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- sessions

def mint_session(session: Session, tg_id: str, *, now: datetime | None = None) -> str:
    """Создать серверную admin-сессию для tg_id. Возвращает raw session_id
    (уходит ТОЛЬКО в куку admin_tg_session); в БД только хеш."""
    now = now or _utcnow()
    raw = secrets.token_urlsafe(32)
    session.add(AdminSession(
        id=f"as_{secrets.token_hex(12)}",
        session_hash=_hash(raw),
        tg_id=str(tg_id),
        auth_method="telegram",
        created_at=now,
        expires_at=now + timedelta(hours=SESSION_TTL_HOURS),
        last_seen_at=now,
    ))
    session.commit()
    return raw


def resolve_session(
    session: Session, raw: str, *, now: datetime | None = None
) -> AdminSession | None:
    """Вернуть живую сессию по raw session_id (не истёкшую, не отозванную),
    иначе None. Allowlist tg_id проверяет ВЫЗЫВАЮЩИЙ (per-request отзыв)."""
    if not raw:
        return None
    now = now or _utcnow()
    row = session.execute(
        select(AdminSession).where(AdminSession.session_hash == _hash(raw))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if _coerce_utc(row.expires_at) < now:
        return None
    return row


def revoke_session(session: Session, raw: str, *, now: datetime | None = None) -> bool:
    """Отозвать сессию по raw id (для logout). True если отозвали живую."""
    now = now or _utcnow()
    res = session.execute(
        update(AdminSession)
        .where(AdminSession.session_hash == _hash(raw), AdminSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    session.commit()
    return bool(res.rowcount)


def cleanup_expired_admin_sessions(session: Session, *, now: datetime | None = None) -> int:
    """GC: удалить сессии, истёкшие более суток назад. Возвращает число удалённых."""
    now = now or _utcnow()
    cutoff = now - timedelta(days=1)
    res = session.execute(
        AdminSession.__table__.delete().where(AdminSession.expires_at < cutoff)
    )
    session.commit()
    return res.rowcount or 0


# ------------------------------------------------------------- challenges

from dataclasses import dataclass  # noqa: E402

from sreda.db.models.admin_auth import AdminLoginChallenge  # noqa: E402

CHALLENGE_TTL_MINUTES = 5
_RATE_MAX = 8               # challenge'ей с одного IP
_RATE_WINDOW_MINUTES = 10
# Человекочитаемый код без неоднозначных символов (0/O, 1/I/L).
_HUMAN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class AdminLoginRateLimited(Exception):
    """С одного IP слишком много challenge'ей за окно."""


@dataclass(frozen=True, slots=True)
class StartChallengeResult:
    challenge_id: str          # публичный хендл (в deep-link)
    browser_bind_raw: str      # СЕКРЕТ, только в куку browser_bind
    human_code: str            # анти-фишинг визуал (web + Telegram)


def _gen_challenge_id() -> str:
    return secrets.token_urlsafe(24)          # ~32 симв, влезает в 64-байт start-param


def _gen_human_code(n: int = 4) -> str:
    return "".join(secrets.choice(_HUMAN_ALPHABET) for _ in range(n))


def start_challenge(
    session: Session, client_ip: str | None, *, now: datetime | None = None
) -> StartChallengeResult:
    """GET /admin/login: создать pending challenge, вернуть публичный id +
    секрет browser_bind (в куку) + human_code. Rate-limit по client_ip."""
    now = now or _utcnow()
    if client_ip:
        window_start = now - timedelta(minutes=_RATE_WINDOW_MINUTES)
        recent = session.execute(
            select(func.count(AdminLoginChallenge.id)).where(
                AdminLoginChallenge.client_ip == client_ip,
                AdminLoginChallenge.created_at >= window_start,
            )
        ).scalar_one()
        if recent >= _RATE_MAX:
            raise AdminLoginRateLimited(f"ip {client_ip} exceeded {_RATE_MAX}/{_RATE_WINDOW_MINUTES}min")

    challenge_id = _gen_challenge_id()
    browser_bind_raw = secrets.token_urlsafe(32)
    human_code = _gen_human_code()
    session.add(AdminLoginChallenge(
        id=f"alc_{secrets.token_hex(12)}",
        challenge_id=challenge_id,
        browser_bind_hash=_hash(browser_bind_raw),
        human_code=human_code,
        status="pending",
        client_ip=client_ip,
        expires_at=now + timedelta(minutes=CHALLENGE_TTL_MINUTES),
        created_at=now,
    ))
    session.commit()
    return StartChallengeResult(challenge_id, browser_bind_raw, human_code)


def attach_bot(
    session: Session, challenge_id: str, tg_id: str, bot_key: str | None,
    chat_id: str | None, *, now: datetime | None = None,
) -> AdminLoginChallenge | None:
    """Бот /start: атомарный send-claim. Возвращает строку ТОЛЬКО при первом
    выигранном переходе (button_send_claimed_at IS NULL) → кнопку шлём лишь тогда.
    Дубль /start → None (не шлём вторую кнопку). Статус остаётся pending для confirm."""
    now = now or _utcnow()
    row = session.execute(
        update(AdminLoginChallenge)
        .where(
            AdminLoginChallenge.challenge_id == challenge_id,
            AdminLoginChallenge.status == "pending",
            AdminLoginChallenge.button_send_claimed_at.is_(None),
            AdminLoginChallenge.expires_at > now,
        )
        .values(button_send_claimed_at=now, tg_id=str(tg_id), bot_key=bot_key, chat_id=chat_id)
        .returning(AdminLoginChallenge)
    ).scalar_one_or_none()
    session.commit()
    return row


def record_confirm_message(
    session: Session, challenge_id: str, message_id: int, *, now: datetime | None = None,
) -> None:
    """Сохранить message_id отправленной confirm-кнопки (для последующего удаления).
    Идемпотентно (WHERE message_id IS NULL)."""
    session.execute(
        update(AdminLoginChallenge)
        .where(
            AdminLoginChallenge.challenge_id == challenge_id,
            AdminLoginChallenge.status == "pending",
            AdminLoginChallenge.message_id.is_(None),
        )
        .values(message_id=message_id)
    )
    session.commit()


def confirm(
    session: Session, challenge_id: str, tg_id: str, *, now: datetime | None = None,
) -> str:
    """Callback adm_confirm: атомарный pending→confirmed. Только тот же tg_id,
    что начал (attach). Возвращает 'confirmed' | 'already' | 'expired_or_denied'."""
    now = now or _utcnow()
    row = session.execute(
        update(AdminLoginChallenge)
        .where(
            AdminLoginChallenge.challenge_id == challenge_id,
            AdminLoginChallenge.status == "pending",
            AdminLoginChallenge.tg_id == str(tg_id),
            AdminLoginChallenge.expires_at > now,
        )
        .values(status="confirmed", confirmed_at=now)
        .returning(AdminLoginChallenge)
    ).scalar_one_or_none()
    session.commit()
    if row is not None:
        return "confirmed"
    # различаем «уже подтверждён» (для нейтрального ответа) от «нет/протух»
    existing = session.execute(
        select(AdminLoginChallenge.status).where(
            AdminLoginChallenge.challenge_id == challenge_id,
            AdminLoginChallenge.tg_id == str(tg_id),
        )
    ).scalar_one_or_none()
    return "already" if existing in ("confirmed", "consumed") else "expired_or_denied"


def get_status(session: Session, challenge_id: str, browser_bind_raw: str) -> str:
    """READ-only статус для поллинга (GET /status). НЕ мутирует. 'unknown' если
    нет/чужой browser_bind."""
    if not challenge_id or not browser_bind_raw:
        return "unknown"
    row = session.execute(
        select(AdminLoginChallenge).where(
            AdminLoginChallenge.challenge_id == challenge_id,
            AdminLoginChallenge.browser_bind_hash == _hash(browser_bind_raw),
        )
    ).scalar_one_or_none()
    return row.status if row is not None else "unknown"


def claim(
    session: Session, challenge_id: str, browser_bind_raw: str, *, now: datetime | None = None,
) -> str | None:
    """POST /admin/login/claim: атомарный confirmed→consumed при совпадении
    browser_bind. Возвращает tg_id (→ минт сессии) или None. Чужой bind НЕ жжёт."""
    now = now or _utcnow()
    if not browser_bind_raw:
        return None
    row = session.execute(
        update(AdminLoginChallenge)
        .where(
            AdminLoginChallenge.challenge_id == challenge_id,
            AdminLoginChallenge.status == "confirmed",
            AdminLoginChallenge.browser_bind_hash == _hash(browser_bind_raw),
            AdminLoginChallenge.expires_at > now,
        )
        .values(status="consumed", consumed_at=now)
        .returning(AdminLoginChallenge)
    ).scalar_one_or_none()
    session.commit()
    return str(row.tg_id) if row is not None and row.tg_id else None


def cleanup_expired_challenges(session: Session, *, now: datetime | None = None) -> int:
    """GC: удалить challenge'и, истёкшие более суток назад."""
    now = now or _utcnow()
    cutoff = now - timedelta(days=1)
    res = session.execute(
        AdminLoginChallenge.__table__.delete().where(AdminLoginChallenge.expires_at < cutoff)
    )
    session.commit()
    return res.rowcount or 0
