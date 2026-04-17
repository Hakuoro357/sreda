"""Telegram Mini App router for subscription management.

Serves:
- GET /miniapp/           — HTML shell (SPA with hash-routing)
- GET/POST /miniapp/api/  — JSON API (requires initData auth)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_miniapp_rate_limit, get_session
from sreda.config.settings import get_settings
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.services.billing import (
    PLAN_EDS_MONITOR_BASE,
    PLAN_EDS_MONITOR_EXTRA,
    PLAN_VOICE_TRANSCRIPTION,
    BillingService,
)
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService
from sreda.services.telegram_auth import (
    TelegramInitDataError,
    resolve_tenant_from_telegram_id,
    validate_init_data,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/miniapp",
    tags=["miniapp"],
    dependencies=[Depends(enforce_miniapp_rate_limit)],
)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "miniapp" / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MiniAppContext:
    tenant_id: str
    user_id: str
    telegram_id: str
    workspace_id: str | None


def _require_miniapp_auth(
    request: Request,
    session: Session = Depends(get_session),
) -> MiniAppContext:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("tma "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_auth")

    init_data_raw = auth_header[4:]
    settings = get_settings()
    bot_token = settings.telegram_bot_token
    if not bot_token:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="bot_token_not_configured")

    try:
        webapp_user = validate_init_data(init_data_raw, bot_token)
    except TelegramInitDataError as exc:
        logger.warning("miniapp auth failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_init_data") from exc

    resolved = resolve_tenant_from_telegram_id(session, webapp_user.telegram_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user_not_found")

    tenant_id, user_id = resolved

    # Resolve workspace_id for connect link creation
    from sreda.db.models.core import Assistant, Workspace

    assistant = (
        session.query(Assistant)
        .filter(Assistant.tenant_id == tenant_id)
        .order_by(Assistant.id.asc())
        .first()
    )
    workspace_id = assistant.workspace_id if assistant else None
    if workspace_id is None:
        workspace = (
            session.query(Workspace)
            .filter(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        workspace_id = workspace.id if workspace else None

    return MiniAppContext(
        tenant_id=tenant_id,
        user_id=user_id,
        telegram_id=webapp_user.telegram_id,
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PlanKeyBody(BaseModel):
    plan_key: str


class EmptyBody(BaseModel):
    pass


# ---------------------------------------------------------------------------
# HTML endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def miniapp_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    base_url = (settings.connect_public_base_url or "").strip().rstrip("/")
    template = _jinja_env.get_template("subscriptions.html")
    html = template.render(base_url=base_url)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# JSON API — Summary
# ---------------------------------------------------------------------------


@router.get("/api/v1/summary")
def get_summary(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    summary = billing.get_summary(ctx.tenant_id)
    now = datetime.now(UTC)

    # Query plans and subscriptions directly
    plans_by_key = _plans_by_key(session)

    base_plan = plans_by_key.get(PLAN_EDS_MONITOR_BASE)
    extra_plan = plans_by_key.get(PLAN_EDS_MONITOR_EXTRA)
    extra_price_rub = extra_plan.price_rub if extra_plan else 2990

    # Build active skills list (for main screen cards)
    active_skills: list[dict] = []
    available_skills: list[dict] = []

    # EDS Monitor
    if summary.base_active and base_plan:
        total_eds_subs = 1 + summary.extra_quantity
        total_eds_price = base_plan.price_rub
        if summary.extra_quantity > 0 and extra_plan:
            total_eds_price += extra_plan.price_rub * summary.extra_quantity
        active_skills.append({
            "feature_key": "eds_monitor",
            "title": "EDS Monitor",
            "icon": "\U0001f4ca",
            "summary_line": f"{total_eds_subs} шт \u00b7 {total_eds_price:,} \u20bd/мес".replace(",", " "),
            "is_active": True,
        })
    elif base_plan:
        available_skills.append({
            "feature_key": "eds_monitor",
            "plan_key": PLAN_EDS_MONITOR_BASE,
            "title": "EDS Monitor",
            "icon": "\U0001f4ca",
            "description": base_plan.description or "",
            "price_rub": base_plan.price_rub,
            "is_active": False,
        })

    # Simple skills (one plan → one subscription → one card). Voice was
    # first, housewife joined it; any future plan that doesn't need
    # bespoke aggregation (like EDS base+extra) can be added here with
    # just one line.
    _simple_skills: list[tuple[str, str, str, str]] = [
        # (plan_key, feature_key, default_title, icon)
        (PLAN_VOICE_TRANSCRIPTION, "voice_transcription", "Распознавание голоса", "\U0001f3a4"),
        ("housewife_assistant_base", "housewife_assistant", "Помощник домохозяйки", "\U0001f3e0"),
    ]
    for plan_key, feature_key, default_title, icon in _simple_skills:
        plan = plans_by_key.get(plan_key)
        if plan is None:
            continue
        sub = _get_sub(session, ctx.tenant_id, plan)
        is_active = _is_active(sub, now)
        if is_active:
            active_skills.append({
                "feature_key": feature_key,
                "title": plan.title or default_title,
                "icon": icon,
                "summary_line": "Бесплатно" if plan.price_rub == 0 else f"{plan.price_rub:,} \u20bd/мес".replace(",", " "),
                "is_active": True,
                "plan_key": plan_key,
                "description": plan.description or "",
                "price_rub": plan.price_rub,
                "active_until": _iso(sub.active_until) if sub else None,
            })
        else:
            available_skills.append({
                "feature_key": feature_key,
                "plan_key": plan_key,
                "title": plan.title or default_title,
                "icon": icon,
                "description": plan.description or "",
                "price_rub": plan.price_rub,
                "is_active": False,
            })

    # EDS subscriptions detail (for EDS skill page)
    eds_subscriptions = _build_eds_subscriptions(
        session, summary, ctx.tenant_id, base_plan, extra_plan,
    )

    return {
        "next_payment_due_at": _iso(summary.next_payment_due_at),
        "next_amount_rub": summary.next_amount_rub,
        "active_skills": active_skills,
        "available_skills": available_skills,
        "eds_subscriptions": eds_subscriptions,
        "extra_price_rub": extra_price_rub,
    }


def _plans_by_key(session: Session) -> dict[str, SubscriptionPlan]:
    """Load all plans indexed by plan_key."""
    plans = session.query(SubscriptionPlan).all()
    return {p.plan_key: p for p in plans}


def _get_sub(
    session: Session, tenant_id: str, plan: SubscriptionPlan | None,
) -> TenantSubscription | None:
    if plan is None:
        return None
    return (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == tenant_id,
            TenantSubscription.plan_id == plan.id,
        )
        .first()
    )


def _is_active(sub: TenantSubscription | None, now: datetime) -> bool:
    if sub is None or not sub.quantity or sub.quantity <= 0:
        return False
    if not sub.active_until:
        return False
    active_until = sub.active_until
    if active_until.tzinfo is None:
        active_until = active_until.replace(tzinfo=UTC)
    if active_until <= now:
        return False
    return sub.status in {"active", "scheduled_for_cancel"}


def _build_eds_subscriptions(
    session: Session,
    summary,
    tenant_id: str,
    base_plan: SubscriptionPlan | None,
    extra_plan: SubscriptionPlan | None,
) -> list[dict]:
    """Build per-slot EDS subscription cards for the EDS detail page."""
    result: list[dict] = []
    accounts = summary.connected_accounts or []

    # Map accounts by role/index for slot assignment
    active_accounts = [a for a in accounts if not a.scheduled_for_disconnect]
    disconnecting_accounts = [a for a in accounts if a.scheduled_for_disconnect]

    if summary.base_active and base_plan:
        # Base subscription card
        base_account = None
        for acc in active_accounts:
            if acc.account_role != "extra":
                base_account = acc
                break
        if base_account is None and active_accounts:
            base_account = active_accounts[0]

        card: dict = {
            "title": "EDS Monitor",
            "price_rub": base_plan.price_rub,
            "active_until": _iso(summary.base_active_until),
            "status": "scheduled_for_cancel" if summary.base_cancel_at_period_end else "active",
            "can_cancel": not summary.base_cancel_at_period_end,
            "cancel_type": "base",
            "slot_type": "base",
            "is_free_slot": False,
            "account": None,
        }
        if base_account:
            card["account"] = _account_dict(base_account)
            active_accounts = [a for a in active_accounts if a.tenant_eds_account_id != base_account.tenant_eds_account_id]
        elif summary.free_count > 0:
            card["is_free_slot"] = True
        result.append(card)

    # Extra subscription cards
    extra_price = extra_plan.price_rub if extra_plan else 2990

    for i in range(summary.extra_quantity):
        acc = active_accounts[i] if i < len(active_accounts) else None
        is_free = acc is None

        # A free slot (paid but no cabinet attached) should be
        # cancel-able right from the card — otherwise the user has a
        # paid slot they can't get rid of unless they first attach a
        # cabinet. slot_type="free" routes the JS to window._removeSlot.
        card = {
            "title": "Доп. кабинет EDS",
            "price_rub": extra_price,
            "active_until": _iso(summary.extra_active_until),
            "status": "active",
            "can_cancel": is_free,
            "cancel_type": "extra",
            "slot_type": "free" if is_free else "extra",
            "is_free_slot": is_free,
            "account": _account_dict(acc) if acc else None,
        }
        # Already scheduled for removal at period end — keep the same
        # labelling as the free-slot case so the UI is consistent.
        if i >= summary.extra_next_cycle_quantity and summary.extra_next_cycle_quantity < summary.extra_quantity:
            card["slot_type"] = "free"
            card["can_cancel"] = True
        result.append(card)

    # Disconnecting accounts (shown in their slots)
    for acc in disconnecting_accounts:
        already_shown = any(
            c.get("account") and c["account"]["id"] == acc.tenant_eds_account_id
            for c in result
        )
        if not already_shown:
            card = {
                "title": "Доп. кабинет EDS",
                "price_rub": extra_price,
                "active_until": _iso(summary.extra_active_until),
                "status": "active",
                "can_cancel": False,
                "cancel_type": "extra",
                "slot_type": "extra",
                "is_free_slot": False,
                "account": _account_dict(acc),
            }
            result.append(card)

    return result


def _account_dict(acc) -> dict:
    return {
        "id": acc.tenant_eds_account_id,
        "login_masked": acc.login_masked,
        "status": acc.status,
    }


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


# ---------------------------------------------------------------------------
# JSON API — Plans catalog
# ---------------------------------------------------------------------------


@router.get("/api/v1/plans")
def get_plans(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    billing.ensure_default_plans()

    plans = (
        session.query(SubscriptionPlan)
        .filter(
            SubscriptionPlan.is_public.is_(True),
            SubscriptionPlan.is_active.is_(True),
        )
        .order_by(SubscriptionPlan.sort_order.asc())
        .all()
    )

    return {
        "plans": [
            {
                "plan_key": p.plan_key,
                "feature_key": p.feature_key,
                "title": p.title,
                "description": p.description,
                "price_rub": p.price_rub,
                "billing_period_days": p.billing_period_days,
            }
            for p in plans
        ]
    }


# ---------------------------------------------------------------------------
# JSON API — Subscribe / Cancel / Resume / Renew
# ---------------------------------------------------------------------------


@router.post("/api/v1/subscribe")
def subscribe(
    body: PlanKeyBody,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    plan_key = body.plan_key

    if plan_key == PLAN_EDS_MONITOR_BASE:
        result = billing.start_base_subscription(ctx.tenant_id)
    elif plan_key == PLAN_EDS_MONITOR_EXTRA:
        result = billing.add_extra_eds_account(ctx.tenant_id)
    elif plan_key == PLAN_VOICE_TRANSCRIPTION:
        result = billing.start_voice_subscription(ctx.tenant_id)
    else:
        raise HTTPException(status_code=400, detail="unknown_plan")

    return {"ok": True, "message": result.message_text}


@router.post("/api/v1/cancel")
def cancel(
    body: PlanKeyBody,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    plan_key = body.plan_key

    if plan_key == PLAN_EDS_MONITOR_BASE:
        result = billing.cancel_base_at_period_end(ctx.tenant_id)
    elif plan_key == PLAN_VOICE_TRANSCRIPTION:
        result = billing.cancel_voice_subscription(ctx.tenant_id)
    else:
        raise HTTPException(status_code=400, detail="unknown_plan")

    return {"ok": True, "message": result.message_text}


@router.post("/api/v1/resume")
def resume(
    body: PlanKeyBody,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)

    if body.plan_key == PLAN_EDS_MONITOR_BASE:
        result = billing.resume_base_renewal(ctx.tenant_id)
    else:
        raise HTTPException(status_code=400, detail="unknown_plan")

    return {"ok": True, "message": result.message_text}


@router.post("/api/v1/renew")
def renew(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    result = billing.renew_cycle(ctx.tenant_id)
    return {"ok": True, "message": result.message_text}


# ---------------------------------------------------------------------------
# JSON API — EDS accounts
# ---------------------------------------------------------------------------


@router.get("/api/v1/eds/accounts")
def list_eds_accounts(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    summary = billing.get_summary(ctx.tenant_id)
    return {
        "accounts": [_account_dict(a) for a in summary.connected_accounts],
    }


@router.post("/api/v1/eds/accounts/{account_id}/cancel")
def cancel_eds_account(
    account_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    result = billing.schedule_connected_eds_account_cancel(ctx.tenant_id, account_id)
    return {"ok": True, "message": result.message_text}


@router.post("/api/v1/eds/accounts/{account_id}/restore")
def restore_eds_account(
    account_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    result = billing.restore_connected_eds_account_cancel(ctx.tenant_id, account_id)
    return {"ok": True, "message": result.message_text}


@router.post("/api/v1/eds/slot/remove")
def remove_eds_slot(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    result = billing.remove_extra_account_at_period_end(ctx.tenant_id)
    return {"ok": True, "message": result.message_text}


@router.post("/api/v1/eds/slot/restore")
def restore_eds_slot(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    billing = BillingService(session)
    result = billing.restore_extra_account_slot(ctx.tenant_id)
    return {"ok": True, "message": result.message_text}


# ---------------------------------------------------------------------------
# JSON API — EDS connect (create session + return form URL)
# ---------------------------------------------------------------------------


@router.post("/api/v1/eds/connect")
def eds_connect(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Create a one-time connect session for a free EDS slot."""
    settings = get_settings()
    try:
        link = EDSConnectService(session, settings).create_connect_link(
            tenant_id=ctx.tenant_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            slot_type="extra",
        )
    except ConnectSessionError as exc:
        return {"ok": False, "message": exc.message, "connect_url": None}

    return {"ok": True, "connect_url": link.url, "message": None}


@router.post("/api/v1/eds/add-and-connect")
def eds_add_and_connect(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Add an extra EDS subscription slot AND create a connect session."""
    billing = BillingService(session)
    result = billing.add_extra_eds_account(ctx.tenant_id)

    # Now create the connect link
    settings = get_settings()
    try:
        link = EDSConnectService(session, settings).create_connect_link(
            tenant_id=ctx.tenant_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            slot_type="extra",
        )
    except ConnectSessionError:
        # Subscription was created but link failed — still return success
        # so the UI can refresh and show the new slot.
        return {"ok": True, "message": result.message_text, "connect_url": None}

    return {"ok": True, "connect_url": link.url, "message": result.message_text}
