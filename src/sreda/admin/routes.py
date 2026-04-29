"""Admin dashboard routes — HTML views + management actions."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
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


def _audit_admin_view(
    session, action: str, token: str, request: Request, **metadata
) -> None:
    """Helper: пишет audit_log запись для admin GET-view операций.

    2026-04-28: для compliance каждое чтение PII через админку
    логируется. Best-effort — ошибки не пробрасывает (не валит view).
    """
    from sreda.services.audit import audit_event, hash_admin_token

    md = {
        "ip": request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "?"),
        "ua": (request.headers.get("user-agent") or "")[:120],
    }
    md.update(metadata)
    audit_event(
        session,
        actor_type="admin",
        actor_id=hash_admin_token(token),
        action=action,
        metadata=md,
    )


@router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(session, "admin.users.viewed", token, request)
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
    _audit_admin_view(session, "admin.budget.viewed", token, request)
    rows = get_budget_summary(session)
    return templates.TemplateResponse(
        request, "budget.html",
        {"token": token, "rows": rows, "section": "budget"},
    )


@router.get("/web-search", response_class=HTMLResponse)
def admin_web_search(
    request: Request,
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Tavily quota dashboard — global stats + per-user breakdown.

    2026-04-29: введено вместе с переездом web_search на Tavily
    (1000/мес free tier разделяется на per-user 30 + global 950)."""
    _audit_admin_view(session, "admin.web_search.viewed", token, request)
    from sreda.services.web_search_usage import WebSearchUsageCounter
    counter = WebSearchUsageCounter(session)
    summary = counter.admin_summary()
    per_user = counter.admin_per_user()
    return templates.TemplateResponse(
        request, "web_search.html",
        {
            "token": token,
            "summary": summary,
            "per_user": per_user,
            "section": "web-search",
        },
    )


_LLM_PROVIDERS_METADATA = [
    # Order here drives the dropdown order in llm.html. Keep the
    # current production default (mimo) first so the UI opens with a
    # sensible-looking selection. OpenRouter variants live after.
    {
        "key": "mimo",
        "label": "MiMo-V2-Pro",
        "default_model_attr": "mimo_chat_model",
        "resolver": "resolve_mimo_api_key",
        # Static model id for the UI catalogue row.
        "static_model": None,
    },
    {
        "key": "mimo-v2.5",
        "label": "MiMo-V2.5-Pro",
        "default_model_attr": None,
        "resolver": "resolve_mimo_api_key",
        "static_model": "mimo-v2.5-pro",
    },
    {
        "key": "mimo-v2.5-light",
        "label": "MiMo-V2.5 (light, для простых задач)",
        "default_model_attr": None,
        "resolver": "resolve_mimo_api_key",
        "static_model": "mimo-v2.5",
    },
    {
        "key": "openrouter",
        "label": "OpenRouter · Gemma 4 26B MoE",
        "default_model_attr": "openrouter_chat_model",
        "resolver": "resolve_openrouter_api_key",
        "static_model": None,
    },
    {
        "key": "openrouter-grok",
        "label": "OpenRouter · Grok 4.1 Fast",
        "default_model_attr": None,
        "resolver": "resolve_openrouter_api_key",
        "static_model": "x-ai/grok-4.1-fast",
    },
    {
        "key": "openrouter-qwen",
        "label": "OpenRouter · Qwen 3.6 Plus",
        "default_model_attr": None,
        "resolver": "resolve_openrouter_api_key",
        "static_model": "qwen/qwen3.6-plus",
    },
]


def _llm_context(
    session,
    token: str,
    *,
    flash: str | None = None,
    with_balances: bool = True,
) -> dict:
    """Shared state for GET + POST /admin/llm. Computes availability
    (key configured?), reads current DB overrides, and exposes a
    provider catalogue the template renders into two dropdowns.
    When ``with_balances`` is True (the default for GET), also fetches
    live provider balances — cached 60s inside the service so the
    extra round-trips don't accumulate across admin refreshes."""
    from sreda.services import runtime_config as rc
    from sreda.services import provider_balances as pb

    settings = get_settings()
    providers = []
    for meta in _LLM_PROVIDERS_METADATA:
        resolver = getattr(settings, meta["resolver"], None)
        available = bool(resolver and resolver())
        # Model label: static override beats settings attribute.
        # OpenRouter variants fix their model (grok / qwen / ...); the
        # two 'anchor' providers (mimo, openrouter) read the configured
        # default from settings. Either way, the UI just gets a string.
        if meta.get("static_model"):
            default_model = meta["static_model"]
        elif meta.get("default_model_attr"):
            default_model = getattr(settings, meta["default_model_attr"], "")
        else:
            default_model = ""
        providers.append({
            "key": meta["key"],
            "label": meta["label"],
            "default_model": default_model,
            "available": available,
        })
    current_primary = (
        rc.get_config(session, rc.KEY_CHAT_PROVIDER)
        or settings.chat_provider
    )
    current_fallback = (
        rc.get_config(session, rc.KEY_CHAT_FALLBACK_PROVIDER)
        or settings.chat_fallback_provider
        or ""
    )
    balances = pb.fetch_balances(settings) if with_balances else []
    return {
        "token": token,
        "section": "llm",
        "providers": providers,
        "current_primary": current_primary,
        "current_fallback": current_fallback,
        "balances": balances,
        "flash": flash,
    }


@router.get("/llm", response_class=HTMLResponse)
def admin_llm(
    request: Request,
    refresh: int = Query(default=0),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(
        session, "admin.llm.viewed", token, request,
        refresh=bool(refresh),
    )
    if refresh:
        from sreda.services import provider_balances as pb
        pb.invalidate_cache()
    ctx = _llm_context(session, token)
    return templates.TemplateResponse(request, "llm.html", ctx)


@router.post("/llm", response_class=HTMLResponse)
def admin_llm_save(
    request: Request,
    primary: str = Form(...),
    fallback: str = Form(default=""),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Persist the chosen primary/fallback to ``runtime_config``.
    Both values take effect on the NEXT chat turn in any process —
    5-second in-process cache on the service side caps staleness."""
    from sreda.services import runtime_config as rc

    # Validate against the known-provider catalogue — protects against
    # a stale bookmark sending a typo'd value via POST.
    known_keys = {meta["key"] for meta in _LLM_PROVIDERS_METADATA}
    if primary not in known_keys:
        ctx = _llm_context(
            session, token,
            flash=f"Неизвестный primary-провайдер: {primary!r}",
        )
        return templates.TemplateResponse(request, "llm.html", ctx)

    fallback_clean = fallback.strip()
    if fallback_clean and fallback_clean not in known_keys:
        ctx = _llm_context(
            session, token,
            flash=f"Неизвестный fallback-провайдер: {fallback_clean!r}",
        )
        return templates.TemplateResponse(request, "llm.html", ctx)
    if fallback_clean == primary:
        ctx = _llm_context(
            session, token,
            flash="Fallback не может совпадать с primary — сохранение отменено.",
        )
        return templates.TemplateResponse(request, "llm.html", ctx)

    rc.set_config(session, rc.KEY_CHAT_PROVIDER, primary)
    # Empty string in DB = "explicitly no fallback" so it overrides
    # env-based defaults. None drops the row entirely.
    rc.set_config(
        session, rc.KEY_CHAT_FALLBACK_PROVIDER,
        fallback_clean if fallback_clean else "",
    )
    # Audit: admin сменил LLM provider — важная compliance-actie.
    _audit_admin_view(
        session, "admin.llm.changed", token, request,
        primary=primary, fallback=fallback_clean or "(none)",
    )
    ctx = _llm_context(
        session, token,
        flash=(
            f"Сохранено — primary: {primary}"
            + (f", fallback: {fallback_clean}" if fallback_clean else ", без fallback")
            + ". Применится в течение ~5с."
        ),
    )
    return templates.TemplateResponse(request, "llm.html", ctx)


@router.get("/llm-calls", response_class=HTMLResponse)
def admin_llm_calls(
    request: Request,
    tenant_id: str = Query(...),
    feature_key: str = Query(...),
    page: int = Query(default=1, ge=1),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(
        session, "admin.llm_calls.viewed", token, request,
        tenant_id=tenant_id, feature_key=feature_key, page=page,
    )
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
    session=Depends(_get_session),
):
    _audit_admin_view(
        session, "admin.logs.viewed", token, request,
        file=file, tail=tail, grep=grep,
    )
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

    # 152-ФЗ Часть 2: audit log admin reset action до финального commit'а,
    # чтобы запись попала в одну транзакцию с deletes.
    from sreda.services.audit import audit_event, hash_admin_token

    audit_event(
        session,
        actor_type="admin",
        actor_id=hash_admin_token(token),
        action="admin.tenant.reset",
        resource_type="tenant",
        resource_id=tenant_id,
        metadata={
            "deleted_counts": {k: v for k, v in d.items() if v > 0},
        },
        commit=False,
    )

    session.commit()

    parts = [f"{k}={v}" for k, v in d.items() if v > 0]
    msg = "+".join(parts) if parts else "nothing+to+delete"

    return RedirectResponse(
        url=f"/admin/users?token={token}&reset=ok&msg={msg}",
        status_code=303,
    )


@router.post("/tenant/approve", response_class=HTMLResponse)
async def admin_tenant_approve(
    tenant_id: str = Query(...),
    token: str = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Approve a pending tenant + send the welcome message.

    Sets ``tenants.approved_at = NOW()`` so the telegram_webhook stops
    silent-dropping their messages, and proactively pushes the welcome
    card (same ``build_welcome_message`` used for the pre-approval
    flow) so the user knows they can now use the bot.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from sreda.db.models.core import Tenant, User
    from sreda.integrations.telegram.client import (
        TelegramClient,
        TelegramDeliveryError,
    )
    from sreda.services.onboarding import build_post_approve_message

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(
            url=f"/admin/users?token={token}&approve=err&msg=tenant_not_found",
            status_code=303,
        )

    was_pending = tenant.approved_at is None
    tenant.approved_at = _dt.now(UTC)
    user = (
        session.query(User).filter(User.tenant_id == tenant_id).first()
    )

    # Auto-grant подписки на housewife_assistant на 30 дней (2026-04-25).
    # Пока «Подписки» в Mini App скрыты, юзер сам не может оформить —
    # модератор апрувом сразу даёт месяц доступа. Идемпотентно:
    # только если ранее не было активной подписки на этот feature_key.
    grant_status = "skipped"
    if was_pending:
        from datetime import timedelta as _td
        from uuid import uuid4 as _uuid
        from sreda.db.models.billing import (
            SubscriptionPlan,
            TenantSubscription,
        )

        plan = (
            session.query(SubscriptionPlan)
            .filter(
                SubscriptionPlan.feature_key == "housewife_assistant",
                SubscriptionPlan.is_active.is_(True),
            )
            .order_by(SubscriptionPlan.price_rub.asc())
            .first()
        )
        if plan is not None:
            now_utc = _dt.now(UTC)
            until = now_utc + _td(days=30)
            existing = (
                session.query(TenantSubscription)
                .filter(
                    TenantSubscription.tenant_id == tenant_id,
                    TenantSubscription.plan_id == plan.id,
                )
                .one_or_none()
            )
            if existing is None:
                session.add(TenantSubscription(
                    id=f"sub_{_uuid().hex[:24]}",
                    tenant_id=tenant_id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=now_utc,
                    active_until=until,
                    cancel_at_period_end=False,
                    quantity=1,
                    next_cycle_quantity=1,
                    updated_at=now_utc,
                ))
                grant_status = "granted_30d"
            else:
                # Если ранее уже была подписка (например, прошлая итерация
                # — отменили) — освежаем active_until до now+30 без
                # дублирования строки.
                existing.status = "active"
                existing.starts_at = now_utc
                existing.active_until = until
                existing.cancel_at_period_end = False
                existing.quantity = 1
                existing.next_cycle_quantity = 1
                existing.updated_at = now_utc
                grant_status = "renewed_30d"
        else:
            grant_status = "no_plan_in_db"

    # 152-ФЗ Часть 2: audit admin approve action.
    if was_pending:
        from sreda.services.audit import audit_event, hash_admin_token

        audit_event(
            session,
            actor_type="admin",
            actor_id=hash_admin_token(token),
            action="admin.tenant.approve",
            resource_type="tenant",
            resource_id=tenant_id,
            metadata={"grant_status": grant_status},
            commit=False,
        )

    session.commit()

    # Proactive Telegram welcome. Skipped if:
    # - tenant was already approved (idempotent re-click)
    # - no bot token configured (dev/test)
    # - user has no telegram_account_id (manual CLI-created tenant)
    settings = get_settings()
    delivery_status = "skipped"
    if (
        was_pending
        and settings.telegram_bot_token
        and user is not None
        and user.telegram_account_id
    ):
        client = TelegramClient(settings.telegram_bot_token)
        # 2026-04-27 simplified: после approve шлём ОДНО короткое
        # сообщение — подтверждение + вопрос про имя. Без кнопок.
        # LLM сама развивает диалог дальше (имя через
        # update_profile_field tool, остальное по контексту).
        text = build_post_approve_message()
        try:
            await client.send_message(
                chat_id=user.telegram_account_id,
                text=text,
                reply_markup=None,
            )
            delivery_status = "ok"
        except TelegramDeliveryError:
            delivery_status = "tg_error"

    return RedirectResponse(
        url=(
            f"/admin/users?token={token}&approve=ok&tenant={tenant_id}"
            f"&welcome={delivery_status}&grant={grant_status}"
        ),
        status_code=303,
    )
