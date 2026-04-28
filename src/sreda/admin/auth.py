"""Admin dashboard authentication.

2026-04-28 hardening:
- Поддержка header `X-Admin-Token` (предпочтительный способ — не
  попадает в URL / browser history / web logs / referer).
- Backward-compat fallback на query param `?token=` (transitional —
  bookmarks / curl команды по старым URL продолжают работать).
- Timing-safe сравнение через `hmac.compare_digest` — защита от
  byte-by-byte timing attack.
- Logging admin auth attempts (success / 401 / 403) для видимости в
  /admin/logs.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, Query, Request

from sreda.config.settings import get_settings


logger = logging.getLogger("sreda.admin.auth")


def require_admin_token(
    request: Request,
    header_token: str | None = Header(default=None, alias="X-Admin-Token"),
    query_token: str | None = Query(default=None, alias="token"),
) -> str:
    """FastAPI dependency: validate admin token from header (preferred)
    or query param (legacy fallback).

    Resolution order:
    1. ``X-Admin-Token`` HTTP header (предпочтительный — не leak'ится
       в логах прокси, history браузера, referer заголовках).
    2. ``?token=`` query param (legacy — для существующих закладок и
       curl-скриптов; будет deprecated в Phase 6).

    Raises:
        403 если admin disabled (нет SREDA_ADMIN_TOKEN в env).
        401 если token missing OR не совпадает (через timing-safe compare).

    Returns valid token (для template propagation в формах).
    """
    settings = get_settings()
    expected = settings.admin_token

    if not expected:
        logger.warning(
            "admin auth: 403 ADMIN_DISABLED ip=%s path=%s",
            _client_ip(request), request.url.path,
        )
        raise HTTPException(status_code=403, detail="Admin dashboard is disabled")

    # Header wins over query param.
    presented = header_token or query_token

    if not presented:
        logger.info(
            "admin auth: 401 NO_TOKEN ip=%s path=%s "
            "(neither header X-Admin-Token nor ?token=)",
            _client_ip(request), request.url.path,
        )
        raise HTTPException(status_code=401, detail="Invalid admin token")

    # Timing-safe compare. compare_digest требует одинаковую длину
    # bytes → если presented короче / длиннее, всё равно проходит
    # (constant-time для одинаковой длины, byte-equal для разной).
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        logger.info(
            "admin auth: 401 BAD_TOKEN ip=%s path=%s via=%s",
            _client_ip(request), request.url.path,
            "header" if header_token else "query",
        )
        raise HTTPException(status_code=401, detail="Invalid admin token")

    # Success path — debug-level log (не INFO чтоб не засорять при
    # каждом GET /admin/users refresh).
    logger.debug(
        "admin auth: OK ip=%s path=%s via=%s",
        _client_ip(request), request.url.path,
        "header" if header_token else "query",
    )
    return presented


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Учитывает X-Forwarded-For (от nginx),
    falls back на request.client.host."""
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if xff:
        return xff
    return request.client.host if request.client else "?"
