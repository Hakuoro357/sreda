"""Admin dashboard routes — read-only HTML views."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from sreda.admin.auth import require_admin_token
from sreda.admin.queries import get_all_users, get_budget_summary, get_llm_calls
from sreda.db.session import get_session_factory

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])


def _get_session():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    users = get_all_users(session)
    return templates.TemplateResponse(
        request, "users.html", {"token": token, "users": users},
    )


@router.get("/budget", response_class=HTMLResponse)
def admin_budget(
    request: Request,
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    rows = get_budget_summary(session)
    return templates.TemplateResponse(
        request, "budget.html", {"token": token, "rows": rows},
    )


@router.get("/llm-calls", response_class=HTMLResponse)
def admin_llm_calls(
    request: Request,
    tenant_id: str = Query(...),
    feature_key: str = Query(...),
    page: int = Query(default=1, ge=1),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    data = get_llm_calls(session, tenant_id, feature_key, page=page)
    return templates.TemplateResponse(
        request, "llm_calls.html", {"token": token, "data": data},
    )
