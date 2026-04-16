"""Admin dashboard authentication."""

from __future__ import annotations

from fastapi import HTTPException, Query

from sreda.config.settings import get_settings


def require_admin_token(token: str | None = Query(default=None)) -> str:
    """FastAPI dependency: validate admin token from query param.

    Raises 403 if admin is disabled (no token configured).
    Raises 401 if token is missing or wrong.
    Returns the valid token (for template propagation).
    """
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(status_code=403, detail="Admin dashboard is disabled")
    if not token or token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return token
