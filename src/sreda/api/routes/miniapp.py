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
from sreda.features.app_registry import get_feature_registry
from sreda.features.contracts import MiniAppSection, MiniAppSectionsProvider
from sreda.services.agent_capabilities import active_feature_keys
from sreda.services.billing import (
    PLAN_EDS_MONITOR_BASE,
    PLAN_EDS_MONITOR_EXTRA,
    BillingService,
)
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService
from sreda.services.housewife_menu import HousewifeMenuService
from sreda.services.housewife_recipes import HousewifeRecipeService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.housewife_shopping import HousewifeShoppingService
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

    # Simple skills (one plan → one subscription → one card). Voice
    # transcription was removed in 2026-04 — it's now a capability
    # bundled with agents (see SkillManifestBase.includes_voice), not
    # a standalone subscription. Add any future simple agent here with
    # one tuple and no per-agent branching.
    _simple_skills: list[tuple[str, str, str, str]] = [
        # (plan_key, feature_key, default_title, icon)
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
    else:
        # Generic simple-skill path: any plan_key that exists in
        # subscription_plans with a feature_key can be (un)subscribed
        # via start_simple_subscription. Covers voice, housewife, any
        # future simple skill without per-skill branching.
        plan = session.query(SubscriptionPlan).filter(
            SubscriptionPlan.plan_key == plan_key
        ).one_or_none()
        if plan is None:
            raise HTTPException(status_code=400, detail="unknown_plan")
        result = billing.start_simple_subscription(ctx.tenant_id, plan_key)

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
    else:
        plan = session.query(SubscriptionPlan).filter(
            SubscriptionPlan.plan_key == plan_key
        ).one_or_none()
        if plan is None:
            raise HTTPException(status_code=400, detail="unknown_plan")
        result = billing.cancel_simple_subscription(ctx.tenant_id, plan_key)

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


# ---------------------------------------------------------------------------
# JSON API — Mini App home-screen menu (skill-level sections)
# ---------------------------------------------------------------------------


def _collect_menu_sections(
    session: Session, tenant_id: str, user_id: str | None
) -> list[MiniAppSection]:
    """Aggregate Mini App sections from every agent the tenant has active.

    Deduplication rule: if two agents contribute sections with the same
    ``id``, the first-registered wins title/icon, counts are summed.
    This lets "Напоминания" from housewife and teamlead surface as one
    card in the future without Mini App needing to know about agents.
    """
    active_keys = active_feature_keys(session, tenant_id)
    if not active_keys:
        return []

    registry = get_feature_registry()
    merged: dict[str, MiniAppSection] = {}
    for feature_key in active_keys:
        module = registry.modules.get(feature_key)
        if module is None or not isinstance(module, MiniAppSectionsProvider):
            continue
        try:
            sections = module.get_miniapp_sections(session, tenant_id, user_id)
        except Exception:  # noqa: BLE001 — one broken agent mustn't kill the menu
            logger.exception(
                "miniapp sections provider failed for feature=%s", feature_key
            )
            continue
        for section in sections:
            existing = merged.get(section.id)
            if existing is None:
                merged[section.id] = section
            else:
                # Sum counts; keep first agent's title/icon/route.
                summed_count = (existing.count or 0) + (section.count or 0)
                subtitle = (
                    f"{summed_count} активных" if summed_count else "пока пусто"
                )
                merged[section.id] = MiniAppSection(
                    id=existing.id,
                    title=existing.title,
                    icon=existing.icon,
                    route=existing.route,
                    subtitle=subtitle,
                    count=summed_count,
                )
    return list(merged.values())


@router.get("/api/v1/menu")
def get_menu(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Home-screen menu for the Mini App.

    Returns skill-level entry-points (Напоминания, etc.) aggregated
    from subscribed agents, plus the always-on Подписки tile.
    """
    sections = _collect_menu_sections(session, ctx.tenant_id, ctx.user_id)
    items = [
        {
            "id": s.id,
            "title": s.title,
            "icon": s.icon,
            "route": s.route,
            "subtitle": s.subtitle,
            "count": s.count,
        }
        for s in sections
    ]
    # Platform-level tile — not an agent contribution, always present.
    items.append(
        {
            "id": "subscriptions",
            "title": "Подписки",
            "icon": "\U0001f4b3",  # 💳
            "route": "#/subscriptions",
            "subtitle": None,
            "count": None,
        }
    )
    return {"items": items}


# ---------------------------------------------------------------------------
# JSON API — Reminders (housewife skill; listed / cancelled from UI)
# ---------------------------------------------------------------------------


def _reminder_to_dict(reminder) -> dict:
    """Serialise a FamilyReminder for the Mini App. Times as ISO-8601
    UTC; the browser applies user locale when formatting."""
    return {
        "id": reminder.id,
        "title": reminder.title,
        "next_trigger_at": _iso(reminder.next_trigger_at),
        "recurrence_rule": reminder.recurrence_rule,
        "is_recurring": bool(reminder.recurrence_rule),
    }


@router.get("/api/v1/reminders")
def list_reminders(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """List pending reminders for the current tenant + user.

    Scoped by user so two people in the same tenant don't see each
    other's reminders. Future agents that contribute reminders (e.g.
    teamlead) can plug into this endpoint by storing into the same
    table or by adding a provider pattern — for v1 only housewife.
    """
    service = HousewifeReminderService(session)
    rows = service.list_active(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
    return {"items": [_reminder_to_dict(r) for r in rows]}


@router.post("/api/v1/reminders/{reminder_id}/cancel")
def cancel_reminder(
    reminder_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    service = HousewifeReminderService(session)
    ok = service.cancel(tenant_id=ctx.tenant_id, reminder_id=reminder_id)
    if not ok:
        raise HTTPException(status_code=404, detail="reminder_not_found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# JSON API — Shopping list (housewife v1.1)
# ---------------------------------------------------------------------------


class ShoppingItemCreate(BaseModel):
    """Mini App inline "+ Добавить" input. Only title is required;
    category defaults to ``другое`` server-side and the LLM can later
    re-classify in a follow-up chat turn."""

    title: str
    quantity_text: str | None = None
    category: str | None = None


def _shopping_item_to_dict(row) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "quantity_text": row.quantity_text,
        "category": row.category,
        "status": row.status,
        "added_at": _iso(row.added_at),
    }


@router.get("/api/v1/shopping")
def list_shopping(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """All ``pending`` shopping items for (tenant, user), ordered by
    the fixed category taxonomy (матчится со store-aisle раскладкой,
    не по алфавиту). Front-end groups by ``category`` field."""
    service = HousewifeShoppingService(session)
    rows = service.list_pending(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
    return {"items": [_shopping_item_to_dict(r) for r in rows]}


@router.post("/api/v1/shopping")
def add_shopping_item(
    body: ShoppingItemCreate,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Add one item from the Mini App inline input. LLM-driven bulk
    adds use the chat tool (``add_shopping_items``) — this endpoint
    is the UI-side "+ Добавить" button."""
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="empty_title")
    service = HousewifeShoppingService(session)
    rows = service.add_items(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        items=[
            {
                "title": title,
                "quantity_text": body.quantity_text,
                "category": body.category,
            }
        ],
    )
    if not rows:
        raise HTTPException(status_code=400, detail="insert_failed")
    return {"ok": True, "item": _shopping_item_to_dict(rows[0])}


@router.post("/api/v1/shopping/{item_id}/bought")
def mark_shopping_bought_endpoint(
    item_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Checkbox flip in Mini App → ``status='bought'``."""
    service = HousewifeShoppingService(session)
    n = service.mark_bought(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, ids=[item_id]
    )
    if n == 0:
        raise HTTPException(status_code=404, detail="item_not_found")
    return {"ok": True}


@router.delete("/api/v1/shopping/{item_id}")
def delete_shopping_item(
    item_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Trash icon / swipe-to-delete — cancel without buying.

    Semantically different from mark-bought: user never bought it, they
    just removed it from the list. Both hide from ``list_pending``.
    """
    service = HousewifeShoppingService(session)
    n = service.remove_items(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, ids=[item_id]
    )
    if n == 0:
        raise HTTPException(status_code=404, detail="item_not_found")
    return {"ok": True}


@router.post("/api/v1/shopping/clear-bought")
def clear_bought_shopping_endpoint(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Bulk housekeeping — cancel every item in ``bought`` state.
    Used by the Mini App "Очистить купленное" button."""
    service = HousewifeShoppingService(session)
    n = service.clear_bought(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
    return {"ok": True, "cleared": n}


# ---------------------------------------------------------------------------
# JSON API — Recipes (housewife v1.1)
# ---------------------------------------------------------------------------


def _recipe_summary_dict(row) -> dict:
    """Short dict for the list view — no ingredients / no instructions
    (large text, only fetched on detail view)."""
    import json as _json

    tags: list[str] = []
    if row.tags_json:
        try:
            parsed = _json.loads(row.tags_json)
            if isinstance(parsed, list):
                tags = [str(t) for t in parsed]
        except _json.JSONDecodeError:
            pass
    return {
        "id": row.id,
        "title": row.title,
        "servings": row.servings,
        "source": row.source,
        "source_url": row.source_url,
        "tags": tags,
        "created_at": _iso(row.created_at),
    }


def _recipe_detail_dict(row) -> dict:
    """Full dict for the detail view — ingredients + instructions."""
    base = _recipe_summary_dict(row)
    base["description"] = row.description
    base["instructions_md"] = row.instructions_md
    base["ingredients"] = [
        {
            "id": ing.id,
            "title": ing.title,
            "quantity_text": ing.quantity_text,
            "is_optional": ing.is_optional,
        }
        for ing in (row.ingredients or [])
    ]
    return base


@router.get("/api/v1/recipes")
def list_recipes(
    q: str | None = None,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """List the user's recipe book (most recent first). ``?q=...``
    filters by title or tag substring (case-insensitive).
    """
    service = HousewifeRecipeService(session)
    rows = service.list_recipes(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, query=q or None,
    )
    return {"items": [_recipe_summary_dict(r) for r in rows]}


@router.get("/api/v1/recipes/{recipe_id}")
def get_recipe_endpoint(
    recipe_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Full recipe with ingredients + instructions for the detail view."""
    service = HousewifeRecipeService(session)
    recipe = service.get_recipe(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, recipe_id=recipe_id,
    )
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    return {"recipe": _recipe_detail_dict(recipe)}


@router.delete("/api/v1/recipes/{recipe_id}")
def delete_recipe_endpoint(
    recipe_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Delete a recipe from the user's book. Cascades to ingredients."""
    service = HousewifeRecipeService(session)
    ok = service.delete_recipe(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, recipe_id=recipe_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# JSON API — Menu planning (housewife v1.1)
# ---------------------------------------------------------------------------


def _menu_item_dict(item) -> dict:
    """Serialise one menu cell for the week grid. Includes the linked
    recipe's title if any so the UI doesn't need a second request."""
    out = {
        "id": item.id,
        "day_of_week": item.day_of_week,
        "meal_type": item.meal_type,
        "recipe_id": item.recipe_id,
        "free_text": item.free_text,
        "notes": item.notes,
        "recipe_title": None,
    }
    if item.recipe_id and item.recipe is not None:
        out["recipe_title"] = item.recipe.title
    return out


def _menu_plan_dict(plan) -> dict:
    return {
        "id": plan.id,
        "week_start_date": plan.week_start_date.isoformat(),
        "notes": plan.notes,
        "status": plan.status,
        "items": [_menu_item_dict(item) for item in (plan.items or [])],
    }


@router.get("/api/v1/weekly-menu")
def get_weekly_menu(
    week_start: str | None = None,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Fetch the user's weekly menu grid.

    Without ``?week_start=`` returns the most recent plan. With it
    returns the plan for that specific week (Monday-anchored; any date
    works, service coerces). 404 if no plan exists for the requested
    week.
    """
    service = HousewifeMenuService(session)

    if week_start:
        plan = service.get_plan_for_week(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            week_start=week_start,
        )
    else:
        all_plans = service.list_user_plans(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id
        )
        plan = all_plans[0] if all_plans else None
        if plan is not None:
            # Re-fetch with items eagerly loaded — list_user_plans
            # skips them for cheap listings.
            plan = service.get_plan_for_week(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                week_start=plan.week_start_date,
            )

    if plan is None:
        return {"plan": None}
    return {"plan": _menu_plan_dict(plan)}


@router.post("/api/v1/weekly-menu/{plan_id}/generate-shopping")
def generate_shopping_from_menu_endpoint(
    plan_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Aggregate all recipe ingredients referenced by the plan's items
    into the user's shopping list. Called by the Mini App button
    "Добавить ингредиенты в список покупок".

    Cross-tenant safe — aggregator returns empty if plan isn't owned
    by the calling user, producing ``added=0`` instead of 404 to keep
    the UI path predictable.
    """
    menu_svc = HousewifeMenuService(session)
    shop_svc = HousewifeShoppingService(session)

    ingredients = menu_svc.aggregate_ingredients_for_shopping(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, plan_id=plan_id
    )
    if not ingredients:
        return {"ok": True, "added": 0}

    rows = shop_svc.add_items(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        items=[
            {
                "title": ing.title,
                "quantity_text": ing.quantity_text,
                "category": None,
                "source_recipe_id": ing.source_recipe_id,
            }
            for ing in ingredients
        ],
    )
    return {"ok": True, "added": len(rows)}


@router.delete("/api/v1/weekly-menu")
def clear_weekly_menu_endpoint(
    week_start: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Remove the weekly menu for the given week (UI "Очистить меню")."""
    service = HousewifeMenuService(session)
    n = service.clear_menu(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        week_start=week_start,
    )
    return {"ok": True, "cleared": n}
