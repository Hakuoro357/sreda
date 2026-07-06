"""Admin dashboard authentication (#305: AdminPrincipal + Telegram sessions).

Резолв личности на КАЖДЫЙ admin-request:
1. Кука ``admin_tg_session`` (Telegram-путь, #305) → серверная сессия
   ``admin_sessions`` → principal(telegram, tg_id). Allowlist сверяется ЗДЕСЬ
   (per-request отзыв: убрал tg_id из ``SREDA_ADMIN_TG_IDS`` → сессия перестаёт
   пускать на следующем запросе). Резолвится ПЕРВЫМ, до легаси.
2. Fallback ``SREDA_ADMIN_TOKEN`` (break-glass): легаси-кука ``admin_session``
   (HMAC от токена — ротация токена гасит) / header ``X-Admin-Token`` / query
   ``?token=``. Не изменён. Даёт principal(token).

``require_admin_token`` возвращает ``AdminPrincipal`` (НЕ raw-токен) — роуты/аудит
берут ``principal.actor_id``; raw-токен больше не течёт в шаблоны/URL.

403 «admin disabled» — только если НЕТ ни токена, ни allowlist.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

from fastapi import Cookie, Header, HTTPException, Query, Request

from sreda.config.settings import get_settings

logger = logging.getLogger("sreda.admin.auth")

_SESSION_TTL = 86400  # 24h — серверный срок жизни легаси session-cookie (fallback)


@dataclass(frozen=True)
class AdminPrincipal:
    """Кто аутентифицирован в админке. ``actor_id`` идёт в audit."""

    auth_method: str            # "telegram" | "token"
    tg_id: str | None = None

    @property
    def actor_id(self) -> str:
        return f"admin_tg:{self.tg_id}" if self.tg_id else "admin_token"


def _make_session(admin_token: str, *, now: int | None = None) -> str:
    """Подписанный маркер `<exp>.<hmac>` для ЛЕГАСИ fallback-куки (#150)."""
    exp = (int(time.time()) if now is None else now) + _SESSION_TTL
    sig = hmac.new(
        admin_token.encode("utf-8"), f"admin-session-v1:{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{exp}.{sig}"


def _verify_session(admin_token: str, cookie: str, *, now: int | None = None) -> bool:
    """True если легаси-cookie подписан ЭТИМ токеном и НЕ истёк."""
    _now = int(time.time()) if now is None else now
    try:
        exp_s, sig = cookie.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < _now:
        return False
    expected = hmac.new(
        admin_token.encode("utf-8"), f"admin-session-v1:{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def _resolve_tg_principal(cookie: str) -> AdminPrincipal | None:
    """Резолв Telegram-сессии из ``admin_tg_session``-куки + allowlist-проверка
    СЕЙЧАС. Открывает короткую сессию БД. None если нет/истекла/отозвана/не в allowlist."""
    from sreda.db.session import get_session_factory
    from sreda.services.admin_login import resolve_session

    settings = get_settings()
    allowlist = settings.admin_tg_ids
    if not allowlist:
        return None
    s = get_session_factory()()
    try:
        sess = resolve_session(s, cookie)
        tg_id = str(sess.tg_id) if sess is not None else None
    finally:
        s.close()
    if tg_id and tg_id in allowlist:
        return AdminPrincipal("telegram", tg_id=tg_id)
    return None


def require_admin_token(
    request: Request,
    header_token: str | None = Header(default=None, alias="X-Admin-Token"),
    query_token: str | None = Query(default=None, alias="token"),
    tg_cookie: str | None = Cookie(default=None, alias="admin_tg_session"),
    cookie_token: str | None = Cookie(default=None, alias="admin_session"),
) -> AdminPrincipal:
    """FastAPI dependency: вернуть ``AdminPrincipal`` или бросить 401/403."""
    settings = get_settings()
    admin_token = settings.admin_token
    allowlist = settings.admin_tg_ids

    # 403 только если админка выключена целиком (нет ни токена, ни allowlist).
    if not admin_token and not allowlist:
        logger.warning(
            "admin auth: 403 ADMIN_DISABLED ip=%s path=%s",
            _client_ip(request), request.url.path,
        )
        raise HTTPException(status_code=403, detail="Admin dashboard is disabled")

    # (1) Telegram-сессия — резолвим ПЕРВОЙ.
    if isinstance(tg_cookie, str) and tg_cookie:
        principal = _resolve_tg_principal(tg_cookie)
        if principal is not None:
            logger.debug("admin auth: OK via telegram tg_id=%s path=%s",
                         principal.tg_id, request.url.path)
            return principal

    # (2) Легаси fallback-токен (только если задан SREDA_ADMIN_TOKEN).
    if admin_token:
        if isinstance(cookie_token, str) and _verify_session(admin_token, cookie_token):
            logger.debug("admin auth: OK via legacy cookie path=%s", request.url.path)
            return AdminPrincipal("token")
        presented = header_token or query_token
        if presented and hmac.compare_digest(
            presented.encode("utf-8"), admin_token.encode("utf-8")
        ):
            try:
                request.state.admin_set_session = _make_session(admin_token)
            except Exception:  # noqa: BLE001 — best-effort сигнал middleware
                pass
            logger.debug(
                "admin auth: OK via %s path=%s",
                "header" if header_token else "query", request.url.path,
            )
            return AdminPrincipal("token")

    logger.info(
        "admin auth: 401 NO_VALID_CREDENTIAL ip=%s path=%s",
        _client_ip(request), request.url.path,
    )
    raise HTTPException(status_code=401, detail="Invalid admin credentials")


def _client_ip(request: Request) -> str:
    """Best-effort client IP (учёт X-Forwarded-For от nginx)."""
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if xff:
        return xff
    return request.client.host if request.client else "?"
