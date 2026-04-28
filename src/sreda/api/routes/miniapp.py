"""Telegram Mini App router for subscription management.

Serves:
- GET /miniapp/           — HTML shell (SPA with hash-routing)
- GET/POST /miniapp/api/  — JSON API (requires initData auth)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from sreda.services.housewife_family import HousewifeFamilyService
from sreda.services.housewife_recipes import HousewifeRecipeService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.housewife_shopping import HousewifeShoppingService
from sreda.services.onboarding import ensure_telegram_user_bundle_by_id
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
        # Diagnostic: log enough to distinguish "no header at all" vs
        # "header present but wrong prefix" without leaking the payload.
        ua = request.headers.get("user-agent", "")[:120]
        logger.warning(
            "miniapp auth: missing/invalid Authorization header "
            "(len=%d prefix=%r ua=%r path=%s)",
            len(auth_header),
            auth_header[:8],
            ua,
            request.url.path,
        )
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
        # User has a valid Telegram-signed initData but never triggered the
        # bot webhook (e.g. opened the Mini App directly via menu button or
        # deep link before sending /start). Hash was signed by Telegram —
        # trust it and provision a bundle lazily so the Mini App is usable
        # immediately instead of returning 401 user_not_found.
        display_name = (
            (webapp_user.first_name or "").strip()
            or (webapp_user.username or "").strip()
            or None
        )
        try:
            onboarding = ensure_telegram_user_bundle_by_id(
                session,
                telegram_id=webapp_user.telegram_id,
                display_name=display_name,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception(
                "miniapp auth: lazy provision failed for tg=%s",
                webapp_user.telegram_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="provision_failed",
            )
        logger.info(
            "miniapp auth: lazily provisioned bundle tg=%s tenant=%s user=%s new=%s",
            webapp_user.telegram_id,
            onboarding.tenant_id,
            onboarding.user_id,
            onboarding.is_new_user,
        )
        if onboarding.tenant_id is None or onboarding.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="provision_incomplete",
            )
        tenant_id = onboarding.tenant_id
        user_id = onboarding.user_id
    else:
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
    # Embed a server-side build stamp so we can confirm what code the
    # client actually received (iOS Telegram WebView caches aggressively
    # and bug reports sometimes involve stale HTML).
    build_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    html = template.render(base_url=base_url, build_stamp=build_stamp)
    # no-store + no-cache forces the Telegram WebView to re-fetch every
    # time the WebApp is opened, so we never serve a stale subscriptions
    # shell after a deploy.
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.post("/api/v1/client-diagnostic")
async def client_diagnostic(request: Request) -> dict:
    """Unauthenticated diagnostic sink.

    The Mini App JS posts here when something prevents the normal
    bootstrap (e.g. ``window.Telegram.WebApp.initData`` is empty). We
    log the payload so we can tell from server logs whether the client
    even reached JS, what User-Agent it was, and what diagnostic the
    frontend found — instead of staring at a white screen with no trace.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.warning(
        "miniapp client-diagnostic: reason=%r has_tg=%r hash_keys=%r ua=%r ip=%s body=%r",
        (body.get("reason") if isinstance(body, dict) else None),
        (body.get("has_tg") if isinstance(body, dict) else None),
        (body.get("hash_keys") if isinstance(body, dict) else None),
        (body.get("ua") if isinstance(body, dict) else None),
        request.client.host if request.client else "?",
        body,
    )
    return {"ok": True}


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
    """Serialize a datetime for JSON consumption by the Mini App.

    SQLite drops tzinfo on round-trip, so every datetime we read back
    is naive — but the services always write UTC via ``_utcnow()``.
    Without the ``Z`` suffix, JS's ``new Date(iso)`` interprets the
    string as LOCAL time, which shifted reminder display by the
    local timezone offset (prod 2026-04-22: MSK user saw '09:00' for
    a 12:00-MSK reminder because UTC 09:00 was re-parsed as MSK).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
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
    # Platform-level tile «Подписки» временно скрыт (2026-04-25 —
    # подписку выдаёт админ при approve, юзер сам не оформляет).
    # Route #/subscriptions и весь биллинг-код остаются нетронутыми;
    # просто tile не инжектится в Mini App home до запуска платежей.
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


@router.post("/api/v1/shopping/clear-all")
def clear_all_shopping_endpoint(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Cancel every pending item — the "Очистить всё" button on the
    Mini App shopping screen. Bought items survive (history).
    """
    service = HousewifeShoppingService(session)
    n = service.clear_pending(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
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
        "calories_per_serving": row.calories_per_serving,
        "protein_per_serving": row.protein_per_serving,
        "fat_per_serving": row.fat_per_serving,
        "carbs_per_serving": row.carbs_per_serving,
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
# REMOVED from Mini App 2026-04-22. Voice + backend (HousewifeMenuService,
# plan_week_menu/update_menu_item/list_menu/clear_menu/
# generate_shopping_from_menu chat tools) preserved intact in
# services/housewife_menu.py and services/housewife_chat_tools.py.
# UI may return in a later iteration; this section was the front door,
# nothing downstream relied on it.
# ---------------------------------------------------------------------------


# (menu helpers + 5 endpoints removed below — they lived here before
#  the 2026-04-22 Mini App cleanup. Task scheduler below replaces
#  the UI slot.)


# ---------------------------------------------------------------------------
# JSON API — Task scheduler («Расписание»)
# ---------------------------------------------------------------------------


def _task_dict(task) -> dict:
    """Serialize one Task row for the /schedule/week response.

    Time fields come back as ``"HH:MM"`` strings (LLM tools accept the
    same shape). ``has_reminder`` + ``reminder_offset_minutes`` let
    the Mini App render the 🔔 icon + "напомнить за N мин" label
    without an extra FK fetch per row.
    """
    return {
        "id": task.id,
        "title": task.title,
        "notes": task.notes,
        "time_start": task.time_start.strftime("%H:%M") if task.time_start else None,
        "time_end": task.time_end.strftime("%H:%M") if task.time_end else None,
        "is_recurring": bool(task.recurrence_rule),
        "has_reminder": bool(task.reminder_id),
        "reminder_offset_minutes": task.reminder_offset_minutes,
        "delegated_to": task.delegated_to,
    }


# Human-readable day labels for the schedule screen. Generated server-
# side so the Mini App just renders whatever the endpoint returns; no
# JS locale dance, and the same wording lands in the home card count
# if we ever surface it there.
_DAY_NAMES_RU = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)
_MONTH_NAMES_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _day_label(d: date) -> str:
    """«День-недели, ДД месяц» — e.g. «Четверг, 23 апреля»."""
    return f"{_DAY_NAMES_RU[d.weekday()]}, {d.day} {_MONTH_NAMES_RU[d.month - 1]}"


def _current_monday(today: date) -> date:
    """Start of the ISO week (Monday) that contains ``today``."""
    return today - timedelta(days=today.weekday())


@router.get("/api/v1/schedule/week")
def get_week_schedule(
    start: str | None = None,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Return a 7-day window starting at ``start`` (ISO date, Monday-
    aligned by convention but not enforced).

    If ``start`` is omitted we default to the Monday of the current
    UTC week. Undated (``scheduled_date IS NULL``) tasks surface in
    the top-level ``inbox`` field **only** when ``start`` equals the
    current-week Monday — so the Mini App can render the inbox block
    once above the first loaded week and subsequent «next week»
    fetches don't duplicate it.

    Response shape::

        {
          "week_start": "YYYY-MM-DD",
          "inbox": [task_dict, ...],
          "days": [
            {"date": "...", "label": "Понедельник, 20 апреля",
             "is_past": bool, "tasks": [...]},
            ... # exactly 7 entries
          ]
        }

    Read-only — mutations go through voice (chat tools).
    """
    from sreda.services.tasks import TaskService

    if ctx.user_id is None:
        return {"week_start": None, "inbox": [], "days": []}

    today = datetime.now(UTC).date()
    current_monday = _current_monday(today)

    if start:
        try:
            week_start = date.fromisoformat(start)
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"invalid start date: {exc}",
            ) from exc
    else:
        week_start = current_monday
    week_end = week_start + timedelta(days=6)

    svc = TaskService(session)
    per_day = svc.list_range(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id,
        from_date=week_start, to_date=week_end,
    )

    # Inbox only on the current week — avoids rendering the same
    # undated list at the top of every loaded week.
    if week_start == current_monday:
        inbox_rows = svc.list(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id,
            scheduled_date=None, include_no_date=True,
            status="pending",
        )
        inbox = [_task_dict(t) for t in inbox_rows]
    else:
        inbox = []

    days = []
    cursor = week_start
    while cursor <= week_end:
        days.append({
            "date": cursor.isoformat(),
            "label": _day_label(cursor),
            "is_past": cursor < today,
            "tasks": [_task_dict(t) for t in per_day.get(cursor, [])],
        })
        cursor = cursor + timedelta(days=1)

    return {
        "week_start": week_start.isoformat(),
        "inbox": inbox,
        "days": days,
    }


# ---------------------------------------------------------------------------
# JSON API — Family members (housewife v1.2)
# ---------------------------------------------------------------------------


class FamilyMemberCreate(BaseModel):
    name: str
    role: str
    birth_year: int | None = None
    age_hint: str | None = None
    notes: str | None = None


class FamilyMemberPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    birth_year: int | None = None
    age_hint: str | None = None
    notes: str | None = None


def _family_member_dict(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "role": row.role,
        "birth_year": row.birth_year,
        "age_hint": row.age_hint,
        "notes": row.notes,
        "created_at": _iso(row.created_at),
    }


@router.get("/api/v1/family")
def list_family(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """All family members for the current user, ordered by role."""
    service = HousewifeFamilyService(session)
    rows = service.list_members(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
    return {"items": [_family_member_dict(r) for r in rows]}


@router.post("/api/v1/family")
def add_family_member_endpoint(
    body: FamilyMemberCreate,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Add one family member from Mini App UI."""
    service = HousewifeFamilyService(session)
    try:
        row = service.add_member(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            name=body.name,
            role=body.role,
            birth_year=body.birth_year,
            age_hint=body.age_hint,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "member": _family_member_dict(row)}


@router.patch("/api/v1/family/{member_id}")
def update_family_member_endpoint(
    member_id: str,
    body: FamilyMemberPatch,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Update any subset of fields. Pass only what changed."""
    service = HousewifeFamilyService(session)
    try:
        row = service.update_member(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            member_id=member_id,
            name=body.name,
            role=body.role,
            birth_year=body.birth_year,
            age_hint=body.age_hint,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="member_not_found")
    return {"ok": True, "member": _family_member_dict(row)}


@router.delete("/api/v1/family/{member_id}")
def delete_family_member_endpoint(
    member_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    service = HousewifeFamilyService(session)
    ok = service.remove_member(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, member_id=member_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="member_not_found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Checklists (план 2026-04-25 — именованные списки дел с галочками)
# ---------------------------------------------------------------------------


@router.get("/api/v1/checklists")
def list_checklists_endpoint(
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """All ACTIVE checklists with their items embedded.

    One-shot fetch — Mini App рендерит весь экран за один запрос.
    Items в каждом checklist отсортированы по position.

    Shape::

        {
          "checklists": [
            {
              "id": "checklist_xxx",
              "title": "План кроя на эту неделю",
              "pending": 5, "done": 2, "total": 7,
              "items": [
                {"id": "clitem_yyy", "title": "Лаванда 298 ТС...",
                 "status": "pending", "position": 0},
                ...
              ]
            },
            ...
          ]
        }

    Mutations через голос (по UX-решению — Mini App read-only в MVP).
    """
    from sreda.services.checklists import ChecklistService

    svc = ChecklistService(session)
    lists = svc.list_active(tenant_id=ctx.tenant_id, user_id=ctx.user_id)

    out = []
    for cl in lists:
        items = svc.list_items(list_id=cl.id)
        pending = sum(1 for i in items if i.status == "pending")
        done = sum(1 for i in items if i.status == "done")
        out.append({
            "id": cl.id,
            "title": cl.title,
            "pending": pending,
            "done": done,
            "total": len(items),
            "items": [
                {
                    "id": i.id,
                    "title": i.title,
                    "status": i.status,
                    "position": i.position,
                }
                for i in items
            ],
        })
    return {"checklists": out}


@router.post("/api/v1/checklist/items/{item_id}/toggle")
def toggle_checklist_item_endpoint(
    item_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """Toggle pending ↔ done для пункта чек-листа из Mini App.

    Pre-2026-04-28 поведение: галочки были read-only (пункты ставит
    голос). Юзеры жаловались — тапали и ничего не происходит.
    Теперь tap → toggle через этот endpoint.

    Семантика:
    * pending → done (отметили сделанным)
    * done → pending (передумали, undo)
    * cancelled → нельзя toggle (юзер сам сказал «удали этот пункт»,
      возврат через UI запрещён, можно только голосом)

    Security: проверяем что пункт принадлежит чек-листу tenant+user.
    """
    from sreda.db.models.checklists import Checklist, ChecklistItem
    from sreda.services.checklists import ChecklistService

    # Ownership check: item → checklist → tenant/user.
    item = (
        session.query(ChecklistItem)
        .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
        .filter(
            ChecklistItem.id == item_id,
            Checklist.tenant_id == ctx.tenant_id,
            Checklist.user_id == ctx.user_id,
        )
        .one_or_none()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="item_not_found")

    svc = ChecklistService(session)
    if item.status == "pending":
        updated = svc.mark_done(item_id=item_id)
        new_status = "done"
    elif item.status == "done":
        updated = svc.undo_done(item_id=item_id)
        new_status = "pending"
    else:
        # cancelled — не позволяем toggle через UI
        raise HTTPException(
            status_code=409,
            detail="cancelled_items_cannot_be_toggled",
        )

    if updated is None:
        # race / concurrent delete
        raise HTTPException(status_code=404, detail="item_not_found")

    return {"ok": True, "status": new_status}


