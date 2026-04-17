"""Admin dashboard routes — HTML views + management actions."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sreda.admin.auth import require_admin_token
from sreda.admin.queries import get_all_users, get_budget_summary, get_llm_calls
from sreda.config.settings import get_settings
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
        request, "users.html",
        {"token": token, "users": users, "section": "users"},
    )


@router.get("/budget", response_class=HTMLResponse)
def admin_budget(
    request: Request,
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    rows = get_budget_summary(session)
    return templates.TemplateResponse(
        request, "budget.html",
        {"token": token, "rows": rows, "section": "budget"},
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
        request, "llm_calls.html",
        {"token": token, "data": data, "section": "users"},
    )


@router.get("/logs", response_class=HTMLResponse)
def admin_logs(
    request: Request,
    file: str | None = Query(default=None),
    tail: int = Query(default=500, ge=50, le=5000),
    grep: str | None = Query(default=None),
    token: str = Depends(require_admin_token),
):
    """Tail view over launchd log files configured in settings.

    The file is picked via ``?file=<index>`` (stringified). When a file
    is missing we still render the page with an empty body and an
    inline error, so the sidebar nav keeps working even on a fresh
    host that hasn't produced any logs yet.

    Security: we only read paths declared in the settings whitelist —
    the ``?file`` query is an index into that list, never a user-
    provided filesystem path. This blocks path traversal by design.
    """
    settings = get_settings()
    log_files_cfg = settings.admin_log_files

    files_meta: list[dict] = []
    for idx, (label, path) in enumerate(log_files_cfg):
        exists = os.path.isfile(path)
        files_meta.append({
            "key": str(idx),
            "label": label,
            "path": path,
            "exists": exists,
        })

    selected_key: str | None = None
    selected_meta: dict | None = None
    if file is not None:
        for meta in files_meta:
            if meta["key"] == file:
                selected_key = meta["key"]
                selected_meta = meta
                break
    if selected_meta is None and files_meta:
        # Default to first existing file, or the first entry if none exist.
        for meta in files_meta:
            if meta["exists"]:
                selected_meta = meta
                selected_key = meta["key"]
                break
        if selected_meta is None:
            selected_meta = files_meta[0]
            selected_key = selected_meta["key"]

    lines: list[str] = []
    error: str | None = None
    selected_file: dict | None = None
    if selected_meta is not None:
        selected_file = dict(selected_meta)
        path = selected_meta["path"]
        if not selected_meta["exists"]:
            error = f"Файл не найден: {path}"
            selected_file["size_human"] = "—"
            selected_file["mtime"] = "—"
        else:
            try:
                stat = os.stat(path)
                selected_file["size_human"] = _format_bytes(stat.st_size)
                selected_file["mtime"] = datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                lines = _tail_file(path, tail, grep)
            except OSError as exc:
                error = f"Ошибка чтения: {exc}"

    return templates.TemplateResponse(
        request, "logs.html",
        {
            "token": token,
            "section": "logs",
            "log_files": files_meta,
            "selected_key": selected_key,
            "selected_file": selected_file,
            "lines": lines,
            "tail": tail,
            "grep": grep,
            "error": error,
        },
    )


def _tail_file(path: str, n: int, grep: str | None) -> list[str]:
    """Read the last ``n`` lines of ``path`` and return them
    NEWEST-FIRST so the admin sees the latest event at the top of the
    page without scrolling. Optional substring filter is applied AFTER
    the tail, so grep narrows what's visible but does not widen the
    window (avoids scanning huge log files)."""
    # A blocksize of 64K is enough for any reasonable line length while
    # keeping us out of O(filesize) territory. For files smaller than
    # the blocksize we just read the whole thing.
    blocksize = 65536
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        lines_needed = n + 1  # +1 because the tail block may start mid-line
        while size > 0 and data.count(b"\n") < lines_needed:
            read = min(blocksize, size)
            size -= read
            f.seek(size)
            data = f.read(read) + data
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = repr(data)
    lines = text.splitlines()[-n:]
    if grep:
        lines = [line for line in lines if grep in line]
    lines.reverse()
    return lines


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@router.post("/tenant/reset", response_class=HTMLResponse)
def admin_tenant_reset(
    request: Request,
    tenant_id: str = Query(...),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Full tenant reset: unbind EDS cabinets, delete subscriptions,
    skill states, all events, outbox — as if the user just registered."""
    from sreda.db.models.billing import TenantSubscription  # noqa: keep cycles/orders
    from sreda.db.models.connect import ConnectSession, TenantEDSAccount
    from sreda.db.models.core import OutboxMessage, SecureRecord, Tenant
    from sreda.db.models.eds_monitor import EDSAccount, EDSChangeEvent, EDSClaimState
    from sreda.db.models.inbound_event import InboundEvent
    from sreda.db.models.skill_platform import TenantSkillConfig, TenantSkillState
    # Note: PaymentOrder / TenantBillingCycle are NOT deleted — FK cascades
    # make it fragile, and billing history is useful for audit.

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
