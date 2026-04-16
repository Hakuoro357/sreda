"""Admin dashboard routes — HTML views + management actions."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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


@router.post("/tenant/reset", response_class=HTMLResponse)
def admin_tenant_reset(
    request: Request,
    tenant_id: str = Query(...),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Full tenant reset: unbind EDS cabinets, delete subscriptions,
    skill states, all events, outbox — as if the user just registered."""
    from sreda.db.models.billing import TenantSubscription
    from sreda.db.models.connect import ConnectSession, TenantEDSAccount
    from sreda.db.models.core import OutboxMessage, SecureRecord, Tenant
    from sreda.db.models.eds_monitor import EDSAccount, EDSChangeEvent, EDSClaimState
    from sreda.db.models.inbound_event import InboundEvent
    from sreda.db.models.skill_platform import TenantSkillConfig, TenantSkillState

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return HTMLResponse(f"Tenant {tenant_id} not found", status_code=404)

    d: dict[str, int] = {}

    # EDS state
    d["claim_states"] = session.query(EDSClaimState).filter(
        EDSClaimState.eds_account_id.in_(
            session.query(EDSAccount.id).filter_by(tenant_id=tenant_id)
        )
    ).delete(synchronize_session="fetch")
    d["change_events"] = session.query(EDSChangeEvent).filter(
        EDSChangeEvent.eds_account_id.in_(
            session.query(EDSAccount.id).filter_by(tenant_id=tenant_id)
        )
    ).delete(synchronize_session="fetch")

    # EDS accounts + secure records
    for ta in session.query(TenantEDSAccount).filter_by(tenant_id=tenant_id).all():
        if ta.secure_record_id:
            sr = session.get(SecureRecord, ta.secure_record_id)
            if sr:
                session.delete(sr)
                d["secure_records"] = d.get("secure_records", 0) + 1
        session.delete(ta)
        d["tenant_eds_accounts"] = d.get("tenant_eds_accounts", 0) + 1

    d["eds_accounts"] = session.query(EDSAccount).filter_by(
        tenant_id=tenant_id
    ).delete()

    # Events and outbox
    d["inbound_events"] = session.query(InboundEvent).filter_by(
        tenant_id=tenant_id
    ).delete()
    d["outbox"] = session.query(OutboxMessage).filter_by(
        tenant_id=tenant_id
    ).delete()

    # Connect sessions
    d["connect_sessions"] = session.query(ConnectSession).filter_by(
        tenant_id=tenant_id
    ).delete()

    # Subscriptions
    d["subscriptions"] = session.query(TenantSubscription).filter_by(
        tenant_id=tenant_id
    ).delete()

    # Skill states and configs
    d["skill_states"] = session.query(TenantSkillState).filter_by(
        tenant_id=tenant_id
    ).delete()
    d["skill_configs"] = session.query(TenantSkillConfig).filter_by(
        tenant_id=tenant_id
    ).delete()

    session.commit()

    parts = [f"{k}={v}" for k, v in d.items() if v > 0]
    msg = "+".join(parts) if parts else "nothing+to+delete"

    return RedirectResponse(
        url=f"/admin/users?token={token}&reset=ok&msg={msg}",
        status_code=303,
    )
