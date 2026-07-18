"""CSRF-защита admin POST (#305).

Переход на cookie-auth убрал НЕЯВНУЮ защиту от `?token=` (секрет в форме).
Double-submit токен, ПРИВЯЗАННЫЙ к session-куке: `csrf = HMAC(secret, cookie_value)`.
Атакующий с cross-site не знает ни секрет, ни значение session-куки жертвы → не
подделает токен. Плюс Origin + Sec-Fetch-Site как дешёвый второй слой.

``require_csrf`` вешается ЗАВИСИМОСТЬЮ на РОУТ (fail-closed: забыл поле → 403,
не «открыто»). Применяется к АУТЕНТИФИЦИРОВАННЫМ admin POST (форма рендерится на
GET при уже выставленной session-куке → токен стабилен).

``require_same_origin`` — облегчённый gate (Origin + Sec-Fetch-Site, БЕЗ токена)
для ``POST /admin/login/claim``: там session-куки ещё нет (её как раз минтят), а
привязка держится на SameSite=Strict ``browser_bind``-куке (не шлётся cross-site).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import Form, Header, HTTPException, Request
from urllib.parse import urlsplit

from sreda.config.settings import get_settings

logger = logging.getLogger("sreda.admin.csrf")

# Куки, к которым привязываем токен (первая присутствующая).
_BIND_COOKIES = ("admin_tg_session", "admin_session")

# Item G: single-fire warning when no real secret is configured.
_WARNED_NO_SECRET = False

# Аудит 2026-07-18 (api-admin #11): статический литерал «sreda-admin-csrf»
# заменён на per-process random — одинаковый fallback-secret на всех deploy'ях
# был молчаливой деградацией энтропии. Побочка: csrf-токены инвалидируются при
# рестарте процесса — безвредно (GET re-render'ит форму со свежим токеном);
# в prod всегда задан encryption_key → этот путь там инертен.
_FALLBACK_SECRET: bytes | None = None


def _secret() -> bytes:
    global _WARNED_NO_SECRET, _FALLBACK_SECRET
    s = get_settings()
    base = getattr(s, "encryption_key", None) or getattr(s, "admin_token", None)
    if not base:
        if not _WARNED_NO_SECRET:
            logger.warning(
                "admin CSRF: neither encryption_key nor admin_token is set — "
                "using a PER-PROCESS random csrf secret (dev-only; tokens "
                "invalidate on process restart). Set SREDA_ENCRYPTION_KEY in prod."
            )
            _WARNED_NO_SECRET = True
        if _FALLBACK_SECRET is None:
            _FALLBACK_SECRET = secrets.token_bytes(32)
        return _FALLBACK_SECRET
    return str(base).encode("utf-8")


def _binding(request: Request) -> str:
    for name in _BIND_COOKIES:
        v = request.cookies.get(name)
        if v:
            return f"{name}:{v}"
    # Item D: fallback-token bootstrap. On the FIRST GET authenticated via
    # header/query token, the admin_session cookie is set only in the RESPONSE
    # (middleware), so the rendered form binds to "anon" — but the subsequent
    # POST now carries admin_session and would compute a DIFFERENT token → 403.
    # auth.py stashes the pending session marker in request.state.admin_set_session
    # BEFORE the response is built; bind the form to THAT value so the token
    # matches once the cookie lands.
    pending = getattr(getattr(request, "state", None), "admin_set_session", None)
    if pending:
        return f"admin_session:{pending}"
    return "anon"


def csrf_token(request: Request) -> str:
    """Токен для скрытого поля формы (передаётся в шаблон как ``csrf_token``)."""
    return hmac.new(_secret(), _binding(request).encode("utf-8"), hashlib.sha256).hexdigest()


def _sec_fetch_ok(request: Request) -> bool:
    """Отклоняем, только если браузер ЯВНО говорит cross-site (иначе — на токен)."""
    sfs = request.headers.get("sec-fetch-site")
    return sfs is None or sfs in ("same-origin", "same-site", "none")


def _origin_ok(request: Request) -> bool:
    """Отклоняем, если заголовок ``Origin`` присутствует и его host не совпадает
    с host запроса (чужой Origin). Отсутствие Origin (обычная навигация/форма) —
    не блокируем (полагаемся на Sec-Fetch-Site + токен)."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    origin_host = urlsplit(origin).netloc.lower()
    if not origin_host:
        return False
    # host из заголовка Host (за прокси корректен: uvicorn --proxy-headers).
    req_host = (request.headers.get("host") or "").lower()
    return origin_host == req_host


def _same_origin_ok(request: Request) -> bool:
    return _sec_fetch_ok(request) and _origin_ok(request)


def require_same_origin(request: Request) -> None:
    """Облегчённый route-level gate (Origin + Sec-Fetch-Site, БЕЗ csrf-токена).

    Для ``POST /admin/login/claim``: session-куки ещё нет (её минтят в этом
    запросе), поэтому double-submit токен неприменим; привязка держится на
    SameSite=Strict ``browser_bind``-куке. Здесь мы добавляем именно route-level
    отклонение чужого Origin / cross-site Sec-Fetch (checklist #10)."""
    if not _same_origin_ok(request):
        raise HTTPException(status_code=403, detail="CSRF: cross-site request rejected")


def require_csrf(
    request: Request,
    csrf: str = Form(default=""),
    x_csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """FastAPI dependency для state-changing admin POST. Fail-closed."""
    if not _same_origin_ok(request):
        raise HTTPException(status_code=403, detail="CSRF: cross-site request rejected")
    presented = x_csrf or csrf
    expected = csrf_token(request)
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="CSRF: token mismatch")
