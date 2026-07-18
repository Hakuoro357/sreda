"""Admin dashboard routes — HTML views + management actions."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sreda.admin.auth import AdminPrincipal, require_admin_token
from sreda.admin.csrf import csrf_token, require_csrf, require_same_origin
from sreda.admin.queries import (
    get_budget_summary_for_day,
    get_llm_calls,
    get_spend_by_model,
    get_tenant_spend_detail,
    get_users_page,
    has_active_subscriptions,
)
from sreda.config.constants import TELEGRAM_WEB_DOMAIN
from sreda.config.settings import get_settings
from sreda.db.session import privileged_session

_MSK_TZ = ZoneInfo("Europe/Moscow")

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _fmt_usd(value) -> str:
    """#150: деньги для UI. None → «—»; точный $0 → «$0.0000»;
    0<v<$0.0001 → «<$0.0001»; иначе $X.XXXX.
    Централизует формат — никаких Decimal<float сравнений в шаблонах (Codex MAJOR).
    Точный ноль отделён от микросумм (Codex R4): пустой период показывает честный
    $0.0000, а не вводящее в заблуждение «<$0.0001»."""
    from decimal import Decimal as _D

    if value is None:
        return "—"
    v = value if isinstance(value, _D) else _D(str(value))
    if v == 0:
        return "$0.0000"
    if v < _D("0.0001"):
        return "<$0.0001"
    return f"${v:.4f}"


templates.env.filters["usd"] = _fmt_usd

router = APIRouter(prefix="/admin", tags=["admin"])


async def _get_session():
    # #138 Ф2: админ-дашборд видит ВСЕ тенанты (кросс-тенант) → privileged("admin").
    # Без обёртки под Ф3 RLS все запросы дашборда под ролью app вернут 0 строк (fail-closed).
    # 🔴 ASYNC ОБЯЗАТЕЛЬНО (R1 MAJOR): privileged_session ставит/сбрасывает ContextVar токенами;
    # sync-gen FastAPI-зависимость гоняется в threadpool (anyio КОПИРУЕТ контекст) → enter/exit в
    # разных контекстах → reset падает "Token created in a different Context" на КАЖДОМ запросе.
    # Тот же урок, что get_tenant_db в mini-app (g-073).
    with privileged_session("admin") as session:
        yield session


def _audit_admin_view(
    session, action: str, actor: str, request: Request, **metadata
) -> None:
    """Helper: пишет audit_log запись для admin GET-view операций.

    2026-04-28: для compliance каждое чтение PII через админку
    логируется. Best-effort — ошибки не пробрасывает (не валит view).

    #305: ``actor`` = ``principal.actor_id`` (``admin_tg:<id>`` для Telegram-входа,
    ``admin_token`` для legacy fallback) — raw-токен больше не хешируется/не течёт.
    """
    from sreda.services.audit import audit_event

    md = {
        "ip": request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "?"),
        "ua": (request.headers.get("user-agent") or "")[:120],
    }
    md.update(metadata)
    audit_event(
        session,
        actor_type="admin",
        actor_id=actor,
        action=action,
        metadata=md,
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Обзорный дашборд (#292): сервер/туннели + KPI + ошибки/медленные +
    деньги. Агрегаты читаются из СНАПШОТА (фоновый рефреш в job_runner) —
    на открытии страницы ноль сетевых вызовов и тяжёлых запросов
    (требование владельца 2026-07-02). Живьём — только host-метрики
    (/proc, микросекунды) и статус systemd-юнитов туннелей."""
    from sreda.admin import host_metrics as hostm
    from sreda.admin import overview_snapshot as ov

    from datetime import UTC

    _audit_admin_view(session, "admin.dashboard.viewed", principal.actor_id, request)
    raw_snap, snap_at = ov.load_snapshot(session, ov.KEY_OVERVIEW)
    # R1 medium MAJOR: устаревший/битый payload → пустые блоки, не 500.
    snap = ov.normalize_overview(raw_snap)
    snap_age_min: int | None = None
    if snap_at is not None:
        _now = datetime.now(UTC)
        _at = snap_at if snap_at.tzinfo else snap_at.replace(tzinfo=UTC)
        snap_age_min = max(0, int((_now - _at).total_seconds() // 60))
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "csrf_token": csrf_token(request),
            "section": "dashboard",
            "snap": snap,
            "snap_age_min": snap_age_min,
            "host": hostm.get_host_metrics(),
            "tunnels": hostm.get_egress_tunnels(),
        },
    )


@router.get("/llm-money", response_class=HTMLResponse)
def admin_llm_money(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Раздел «LLM и деньги» (#292 Фаза B): карточки провайдеров —
    баланс, траты день/неделя/месяц, ошибки 24ч, модели с ролями.
    Читает ТОТ ЖЕ снапшот, что обзор (ноль пересчёта на открытии)."""
    from datetime import UTC

    from sreda.admin import overview_snapshot as ov

    _audit_admin_view(session, "admin.llm_money.viewed", principal.actor_id, request)
    raw_snap, snap_at = ov.load_snapshot(session, ov.KEY_OVERVIEW)
    snap = ov.normalize_overview(raw_snap)
    snap_age_min: int | None = None
    if snap_at is not None:
        _now = datetime.now(UTC)
        _at = snap_at if snap_at.tzinfo else snap_at.replace(tzinfo=UTC)
        snap_age_min = max(0, int((_now - _at).total_seconds() // 60))
    return templates.TemplateResponse(
        request, "llm_money.html",
        {
            "csrf_token": csrf_token(request),
            "section": "llm-money",
            "snap": snap,
            "snap_age_min": snap_age_min,
        },
    )


@router.post("/refresh-snapshot")
async def admin_refresh_snapshot(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
    session=Depends(_get_session),
):
    """Ручной пересбор снапшота («обновить сейчас»). Явное действие
    админа — единственный путь, где агрегаты/балансы считаются в
    запросе; сам дашборд по-прежнему только читает.

    R1 (субагент): async + to_thread — сетевые балансы (до ~30с
    таймаутов) не держат воркер threadpool'а uvicorn."""
    import asyncio

    from sreda.admin import overview_snapshot as ov

    _audit_admin_view(session, "admin.dashboard.refreshed", principal.actor_id, request)
    ok = await asyncio.to_thread(
        ov.refresh_overview, None, get_settings()  # #138 R2 M7: None → privileged(admin)
    )
    suffix = "?refresh=err" if not ok else ""
    return RedirectResponse(
        url=f"/admin/{suffix}", status_code=303,
    )


# ---------------------------------------------------------------------------
# #305 — Telegram login flow (unauthenticated entry, NO require_admin_token)
# ---------------------------------------------------------------------------

# Cookies for the login handshake live under path=/admin/login so they are only
# sent to the login endpoints. SameSite=Strict browser_bind is the CSRF/binding
# defence for /admin/login/claim.
_LOGIN_COOKIE_KW = dict(
    httponly=True, secure=True, samesite="strict", path="/admin/login", max_age=300,
)


def _is_trusted_proxy(host: str) -> bool:
    """Доверенный ingress-peer: loopback (nginx на той же VDS) или private-LAN.
    Непарсящийся host (например TestClient «testclient») → НЕ доверенный."""
    try:
        ip = ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _login_client_ip(request: Request) -> str:
    """Client IP для rate-limit login-challenge'ей.

    X-Forwarded-For принимаем ТОЛЬКО от доверенного proxy (loopback/private
    peer — nginx на той же машине / LAN-ingress). Иначе при прямом доступе к
    app атакующий ротирует XFF и обходит per-IP rate-limit challenge'ей
    (аудит 2026-07-18 svc-security #5)."""
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if xff and _is_trusted_proxy(peer):
        return xff
    return peer or "?"


@router.get("/login", response_class=HTMLResponse)
def admin_login(request: Request, session=Depends(_get_session)):
    """Unauth entry: create a login challenge, render the deep-link button +
    human_code, set the two Strict/HttpOnly login cookies. No admin auth here."""
    from sreda.config.bot_registry import TelegramBotRegistry
    from sreda.services.admin_login import (
        AdminLoginRateLimited,
        start_challenge,
    )

    settings = get_settings()

    # Fail-closed: Telegram login is OFF unless SREDA_ADMIN_TG_IDS is non-empty.
    # Do NOT render a dead deep-link button (nor create a useless challenge) when
    # nobody can ever confirm it — return 403 (checklist #15/#10 fail-closed).
    if not settings.admin_tg_ids:
        return HTMLResponse(
            "<h3>Вход через Telegram не настроен</h3>"
            "<p>Список администраторов (SREDA_ADMIN_TG_IDS) пуст. "
            "Используйте аварийный токен-доступ.</p>",
            status_code=403,
        )

    # Fail-closed: need a resolvable admin-bot username to build the deep-link.
    try:
        registry = TelegramBotRegistry.from_settings(settings)
        bot_username = registry.resolve(settings.admin_bot_key).username
    except Exception:  # noqa: BLE001 — unknown key / misconfig
        bot_username = None
    if not bot_username:
        return HTMLResponse(
            "<h3>Вход через Telegram не настроен</h3>"
            "<p>Не задан username админ-бота (SREDA_*_BOT_USERNAME). "
            "Используйте аварийный токен-доступ.</p>",
            status_code=500,
        )

    try:
        result = start_challenge(session, _login_client_ip(request))
    except AdminLoginRateLimited:
        return HTMLResponse(
            "<h3>Слишком много попыток входа</h3>"
            "<p>Подождите несколько минут и обновите страницу.</p>",
            status_code=429,
        )

    # #370: t.me выпал из DNS 2026-07-14 → admin-login deep-link через telegram.me.
    deep_link = f"https://{TELEGRAM_WEB_DOMAIN}/{bot_username}?start=adm_{result.challenge_id}"
    response = templates.TemplateResponse(
        request, "login.html",
        {"deep_link": deep_link, "human_code": result.human_code},
    )
    response.set_cookie("challenge_ref", result.challenge_id, **_LOGIN_COOKIE_KW)
    response.set_cookie("browser_bind", result.browser_bind_raw, **_LOGIN_COOKIE_KW)
    return response


@router.get("/login/status")
def admin_login_status(
    session=Depends(_get_session),
    challenge_ref: str | None = Cookie(default=None),
    browser_bind: str | None = Cookie(default=None),
):
    """READ-only status poll. No mutation, no cookies set."""
    from sreda.services.admin_login import get_status

    if not challenge_ref or not browser_bind:
        return JSONResponse({"status": "unknown"})
    status = get_status(session, challenge_ref, browser_bind)
    return JSONResponse({"status": status})


@router.post("/login/claim")
def admin_login_claim(
    request: Request,
    _: None = Depends(require_same_origin),
    session=Depends(_get_session),
    challenge_ref: str | None = Cookie(default=None),
    browser_bind: str | None = Cookie(default=None),
):
    """Consume a confirmed challenge → mint admin session cookie.

    CSRF defence, two layers (checklist #10):
    1. Route-level ``require_same_origin`` (Origin + Sec-Fetch-Site) rejects a
       cross-site / foreign-Origin POST fail-closed (403) BEFORE any DB work.
       There is no session cookie yet (it's minted here), so the double-submit
       token doesn't apply — this is the route-level gate #10 asks for.
    2. The ``browser_bind`` cookie is SameSite=Strict (never sent cross-site) AND
       a secret the attacker cannot know, so a forged claim can't consume the
       challenge even if the Origin gate were somehow bypassed."""
    from sreda.services.admin_login import claim, mint_session

    if not challenge_ref or not browser_bind:
        return JSONResponse({"status": "denied"}, status_code=400)

    tg_id = claim(session, challenge_ref, browser_bind)
    if not tg_id:
        return JSONResponse({"status": "denied"}, status_code=400)

    raw_session = mint_session(session, tg_id)
    response = JSONResponse({"redirect": "/admin"})
    response.set_cookie(
        "admin_tg_session", raw_session,
        httponly=True, secure=True, samesite="lax", path="/admin", max_age=86400,
    )
    # Clear the one-shot login cookies (same path they were set under).
    response.delete_cookie("challenge_ref", path="/admin/login")
    response.delete_cookie("browser_bind", path="/admin/login")
    return response


@router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    page: int = Query(1, ge=1),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(session, "admin.users.viewed", principal.actor_id, request, page=page)
    users_page = get_users_page(session, page=page)
    return templates.TemplateResponse(
        request, "users.html",
        {
            "csrf_token": csrf_token(request),
            "users": users_page.rows,
            "page": users_page,
            "section": "users",
        },
    )


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def admin_tenant_detail(
    tenant_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Карточка тенанта: траты день/неделя/месяц по моделям + ссылки на
    переписку (llm-calls). Только агрегаты/метаданные — без текстов сообщений.
    Подпись «расходы тенанта/аккаунта, не пользователя» (тенант может иметь
    несколько юзеров) — в шаблоне."""
    _audit_admin_view(
        session, "admin.tenant.viewed", principal.actor_id, request, tenant_id=tenant_id,
    )
    detail = get_tenant_spend_detail(session, tenant_id)
    return templates.TemplateResponse(
        request, "tenant_detail.html",
        {"csrf_token": csrf_token(request), "section": "users", "detail": detail},
    )


@router.get("/budget", response_class=HTMLResponse)
def admin_budget(
    request: Request,
    date_str: str | None = Query(None, alias="date"),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Per-day budget consumption view.

    ?date=YYYY-MM-DD selects a calendar day in Europe/Moscow. Default is
    today MSK. Future dates are silently clamped to today; unparseable
    input also falls back to today (no surfacing — the user can just
    click a valid date instead of getting an error page).

    Nav links:
    - "← Previous day" is always shown (admin may go arbitrarily deep).
    - "→ Next day" is hidden when ``selected == today`` — nothing to see
      in the future.
    """
    _audit_admin_view(session, "admin.budget.viewed", principal.actor_id, request)

    today = datetime.now(_MSK_TZ).date()
    selected = today
    if date_str:
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
            if parsed <= today:
                selected = parsed
        except ValueError:
            pass  # malformed query string — fall back to today

    rows = get_budget_summary_for_day(session, selected)
    # #175: различаем «активных подписок нет» от «подписки есть, но за день нет расхода»
    # (иначе пустой день показывал ложное «Нет активных подписок»).
    has_subs = has_active_subscriptions(session)
    return templates.TemplateResponse(
        request, "budget.html",
        {
            "csrf_token": csrf_token(request),
            "rows": rows,
            "has_active_subs": has_subs,
            "section": "budget",
            "selected_date": selected.isoformat(),
            "prev_date": (selected - timedelta(days=1)).isoformat(),
            "next_date": (
                (selected + timedelta(days=1)).isoformat()
                if selected < today
                else None
            ),
        },
    )


@router.get("/spend-by-model", response_class=HTMLResponse)
def admin_spend_by_model(
    request: Request,
    period: str = Query("month"),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Траты по (provider_key, model) за день/неделю/месяц (MSK).

    ≈USD по ТЕКУЩЕМУ прайсу (`llm_pricing`); беспрайсовые модели → «—». Шапка
    показывает покрытие (% валидных вызовов/токенов с известной ценой) + список
    беспрайсовых + число аномалий. Только агрегаты — без текстов сообщений.
    """
    if period not in ("day", "week", "month"):
        period = "month"
    _audit_admin_view(
        session, "admin.spend_by_model.viewed", principal.actor_id, request, period=period,
    )
    report = get_spend_by_model(session, period)
    return templates.TemplateResponse(
        request, "spend_by_model.html",
        {
            "csrf_token": csrf_token(request),
            "section": "spend-by-model",
            "period": period,
            "report": report,
        },
    )


@router.get("/web-search", response_class=HTMLResponse)
def admin_web_search(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Tavily quota dashboard — global stats + per-user breakdown.

    2026-04-29: введено вместе с переездом web_search на Tavily
    (1000/мес free tier разделяется на per-user 30 + global 950)."""
    _audit_admin_view(session, "admin.web_search.viewed", principal.actor_id, request)
    from sreda.services.web_search_usage import WebSearchUsageCounter
    counter = WebSearchUsageCounter(session)
    summary = counter.admin_summary()
    per_user = counter.admin_per_user()
    return templates.TemplateResponse(
        request, "web_search.html",
        {
            "csrf_token": csrf_token(request),
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
        # 2026-05-29 rename: provider key == model name.
        "key": "mimo-v2.5-pro",
        "label": "MiMo-V2.5-Pro",
        "default_model_attr": None,
        "resolver": "resolve_mimo_api_key",
        "static_model": "mimo-v2.5-pro",
    },
    {
        "key": "mimo-v2.5",
        "label": "MiMo-V2.5 (light, для простых задач)",
        "default_model_attr": None,
        "resolver": "resolve_mimo_api_key",
        "static_model": "mimo-v2.5",
    },
    {
        # #150: планировщик Среды (с 2026-06-15). Баланса нет — fetch_balances
        # тянет только openrouter+mimo, Inception сетью не дёргается («нет API»).
        "key": "inception-mercury2",
        "label": "Inception · Mercury-2 (Фредди, планировщик)",
        "default_model_attr": None,
        "resolver": "resolve_inception_api_key",
        "static_model": "mercury-2",
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
    # 2026-05-12: 5 экспериментальных провайдеров удалены (gpt-oss-120b,
    # llama-70b, llama-4-scout, deepseek, gemini-lite). Не показали value
    # vs mimo/gemini-2.5-flash; держать в UI = риск misconfiguration.
    # 2026-05-10 PM: Nemotron 3 Super (DeepInfra/bf16) — current
    # production primary after bench showed 10× speedup over mimo.
    {
        "key": "openrouter-nemotron-3-super",
        "label": "OpenRouter · Nemotron 3 Super 120B (DeepInfra/bf16)",
        "default_model_attr": None,
        "resolver": "resolve_openrouter_api_key",
        "static_model": "nvidia/nemotron-3-super-120b-a12b",
    },
    # 2026-05-12: Qwen 3.5 Flash — cheapest tool-calling provider в бенче.
    # Активирован как primary через runtime_config (А/B vs mimo).
    {
        "key": "openrouter-qwen-flash",
        "label": "OpenRouter · Qwen 3.5 Flash",
        "default_model_attr": None,
        "resolver": "resolve_openrouter_api_key",
        "static_model": "qwen/qwen3.5-flash-02-23",
    },
    # 2026-05-12: Gemini 2.5 Flash — fastest reliable в бенче (1.5s avg,
    # all 8 scenarios tools clean). Pro tier candidate / mimo fallback.
    {
        "key": "openrouter-gemini-2.5-flash",
        "label": "OpenRouter · Gemini 2.5 Flash",
        "default_model_attr": None,
        "resolver": "resolve_openrouter_api_key",
        "static_model": "google/gemini-2.5-flash",
    },
    # 2026-06-10 (#107, выбор Бориса): кандидат в РОТ против лёгкой mimo-v2.5.
    {
        "key": "openrouter-gemini-2.5-flash-lite",
        "label": "OpenRouter · Gemini 2.5 Flash Lite",
        "default_model_attr": None,
        "resolver": "resolve_openrouter_api_key",
        "static_model": "google/gemini-2.5-flash-lite",
    },
]


def _provider_spend(session, *, days: int = 30) -> list[dict]:
    """#184 ч.3 / #238: расход по провайдерам из skill_ai_executions за N дней.

    Стоимость — USD-оценка ИЗ ТОКЕНОВ через ``llm_pricing.cost_estimate`` (group by
    provider_key+model → est → агрегация по провайдеру), как ``get_spend_by_model``.
    #238: столбец ``estimated_cost_rub_micro`` МЁРТВ (нигде не пишется) → больше не
    суммируем его. Беспрайсовые провайдеры → ``cost_usd=None`` (страница покажет «—»,
    а не выдуманные/нулевые деньги). Сортировка: priced по убыванию стоимости, беспрайсовые в конце."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from sqlalchemy import func, select

    from sreda.admin.queries import _VALID_TOKENS
    from sreda.db.models.skill_platform import SkillAIExecution
    from sreda.services.llm_pricing import cost_estimate

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # #238 R1 (Codex): фильтруем malformed-строки (NULL/отрицательные токены, legacy
    # 0/0-при-total>0) тем же _VALID_TOKENS, что get_spend_by_model — иначе одна
    # отрицательная строка отравит СУММУ группы (→ cost_estimate=None → провайдер «—»
    # при валидных рядах). Токены/вызовы тоже считаем по валидным (консистентно).
    rows = session.execute(
        select(
            SkillAIExecution.provider_key,
            SkillAIExecution.model,
            func.count(),
            func.coalesce(func.sum(SkillAIExecution.prompt_tokens), 0),
            func.coalesce(func.sum(SkillAIExecution.completion_tokens), 0),
        )
        .where(SkillAIExecution.created_at >= cutoff, _VALID_TOKENS)
        .group_by(SkillAIExecution.provider_key, SkillAIExecution.model)
    ).all()

    agg: dict[str, dict] = {}
    for pk, model, cnt, pt, ct in rows:
        provider = pk or "(неизвестно)"
        a = agg.setdefault(provider, {
            "provider": provider, "calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "_cost": Decimal("0"), "_any_priced": False,
        })
        a["calls"] += int(cnt or 0)
        a["prompt_tokens"] += int(pt or 0)
        a["completion_tokens"] += int(ct or 0)
        est = cost_estimate(
            pk or "", model or "",
            prompt_tokens=int(pt or 0), completion_tokens=int(ct or 0),
        )
        if est is not None:
            a["_cost"] += est.est_usd
            a["_any_priced"] = True

    out = [
        {
            "provider": a["provider"],
            "calls": a["calls"],
            "prompt_tokens": a["prompt_tokens"],
            "completion_tokens": a["completion_tokens"],
            "cost_usd": (a["_cost"] if a["_any_priced"] else None),
        }
        for a in agg.values()
    ]
    out.sort(key=lambda r: (r["cost_usd"] is None, -(r["cost_usd"] or Decimal("0"))))
    return out


def _llm_context(
    session,
    request: Request,
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
    # #184 ч.2: react-механизм управляется ENV (НЕ свитчером chat-провайдера выше). Показываем
    # read-only, чтобы правки react/Осы перестали быть невидимы на этой странице.
    react_info = {
        "primary": settings.planner_provider,
        "osa_fallback_on": settings.react_osa_fallback,
        # R1-фикс: флаг ВКЛ ещё не значит «запас работает» — нужен ключ Groq. Показываем отдельно,
        # чтобы оператор не считал запас активным, когда LLM фактически не создастся.
        "osa_fallback_available": bool(
            settings.react_osa_fallback and settings.resolve_groq_api_key()
        ),
        "osa_primary_tenants": sorted(settings.react_osa_tenants),
    }
    # #184 ч.3: расход по провайдерам (баланс-API у Inception/Groq нет → считаем свой). Локальный
    # дешёвый запрос (НЕ сеть) → считаем ВСЕГДА, не под with_balances (R1: иначе POST-перерендер
    # показал бы «нет данных» при наличии расхода).
    react_spend = _provider_spend(session)
    return {
        "csrf_token": csrf_token(request),
        "section": "llm",
        "providers": providers,
        "current_primary": current_primary,
        "current_fallback": current_fallback,
        "balances": balances,
        "react_info": react_info,
        "react_spend": react_spend,
        "flash": flash,
    }


@router.get("/llm", response_class=HTMLResponse)
def admin_llm(
    request: Request,
    refresh: int = Query(default=0),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(
        session, "admin.llm.viewed", principal.actor_id, request,
        refresh=bool(refresh),
    )
    if refresh:
        from sreda.services import provider_balances as pb
        pb.invalidate_cache()
    ctx = _llm_context(session, request)
    return templates.TemplateResponse(request, "llm.html", ctx)


@router.post("/llm", response_class=HTMLResponse)
def admin_llm_save(
    request: Request,
    primary: str = Form(...),
    fallback: str = Form(default=""),
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
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
            session, request,
            flash=f"Неизвестный primary-провайдер: {primary!r}",
        )
        return templates.TemplateResponse(request, "llm.html", ctx)

    fallback_clean = fallback.strip()
    if fallback_clean and fallback_clean not in known_keys:
        ctx = _llm_context(
            session, request,
            flash=f"Неизвестный fallback-провайдер: {fallback_clean!r}",
        )
        return templates.TemplateResponse(request, "llm.html", ctx)
    if fallback_clean == primary:
        ctx = _llm_context(
            session, request,
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
        session, "admin.llm.changed", principal.actor_id, request,
        primary=primary, fallback=fallback_clean or "(none)",
    )
    ctx = _llm_context(
        session, request,
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
    feature_key: str | None = Query(None),
    page: int = Query(default=1, ge=1),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(
        session, "admin.llm_calls.viewed", principal.actor_id, request,
        tenant_id=tenant_id, feature_key=feature_key or "", page=page,
    )
    data = get_llm_calls(session, tenant_id, feature_key, page=page)
    return templates.TemplateResponse(
        request, "llm_calls.html",
        {"csrf_token": csrf_token(request), "data": data, "section": "users"},
    )


@router.get("/logs", response_class=HTMLResponse)
def admin_logs(
    request: Request,
    file: str | None = Query(default=None),
    tail: int = Query(default=500, ge=50, le=5000),
    grep: str | None = Query(default=None),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    _audit_admin_view(
        session, "admin.logs.viewed", principal.actor_id, request,
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
            "csrf_token": csrf_token(request),
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


# ---------------------------------------------------------------------------
# /admin/traces — per-stage latency breakdown viewer (Phase 2)
# ---------------------------------------------------------------------------

@router.get("/traces", response_class=HTMLResponse)
def admin_traces(
    request: Request,
    tail: int = Query(default=50, ge=1, le=500),
    min_total_ms: int = Query(default=0, ge=0, le=600_000),
    user_id: str = Query(default="", max_length=64, pattern=r"^[a-zA-Z0-9_]*$"),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """Per-stage latency breakdown viewer для conversation.chat turns.

    Парсит `/var/log/sreda/trace.log` (tail-only 5MB), показывает
    последние N blocks с цветовой подсветкой:
      - total_ms: <10s green / 10-30s yellow / >30s red
      - per stage: <2s neutral / 2-5s yellow / >5s red

    Query params validated (Codex r1 MAJOR #6):
      - tail ∈ [1, 500]
      - min_total_ms ∈ [0, 600_000]
      - user_id matches `[a-zA-Z0-9_]{0,64}`
    """
    from sreda.admin.trace_parser import (
        parse_trace_log, stage_duration_color, total_ms_color,
    )

    _audit_admin_view(
        session, "admin.traces.viewed", principal.actor_id, request,
        tail=tail, min_total_ms=min_total_ms,
        user_id=user_id or None,
    )

    settings = get_settings()
    trace_log_path = settings.trace_log_path

    error: str | None = None
    traces: list = []
    file_meta: dict = {"path": trace_log_path or "(не сконфигурирован)"}

    if not trace_log_path:
        error = (
            "SREDA_TRACE_LOG_PATH не сконфигурирован — sreda.trace logger "
            "пишет только в stderr/journal. Добавь путь в /etc/sreda/.env."
        )
    else:
        try:
            stat = os.stat(trace_log_path)
            file_meta["size_human"] = _format_bytes(stat.st_size)
            file_meta["mtime"] = datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            traces = parse_trace_log(
                trace_log_path,
                tail_turns=tail,
                min_total_ms=min_total_ms,
                user_filter=user_id or None,
            )
        except FileNotFoundError:
            error = f"Файл не найден: {trace_log_path}"
        except PermissionError as exc:
            error = f"Нет прав на чтение: {exc}"
        except OSError as exc:
            error = f"Ошибка чтения: {exc}"

    return templates.TemplateResponse(
        request, "traces.html",
        {
            "csrf_token": csrf_token(request),
            "section": "traces",
            "traces": traces,
            "tail": tail,
            "min_total_ms": min_total_ms,
            "user_id": user_id,
            "file_meta": file_meta,
            "error": error,
            "total_ms_color": total_ms_color,
            "stage_duration_color": stage_duration_color,
        },
    )


@router.post("/tenant/reset", response_class=HTMLResponse)
def admin_tenant_reset(
    request: Request,
    tenant_id: str = Query(...),
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
    session=Depends(_get_session),
):
    """Full tenant reset: delete subscriptions, skill states, all events,
    outbox — as if the user just registered."""
    from sreda.db.models.billing import TenantSubscription  # keep cycles/orders
    from sreda.db.models.core import OutboxMessage, Tenant
    from sreda.db.models.inbound_event import InboundEvent
    from sreda.db.models.skill_platform import TenantSkillConfig, TenantSkillState
    # Note: PaymentOrder / TenantBillingCycle are NOT deleted — FK cascades
    # make it fragile, and billing history is useful for audit.

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return HTMLResponse(f"Tenant {tenant_id} not found", status_code=404)

    # #187 (аудит 2026-07-18 api-admin #8): soft-deleted тенант НЕ мутируем —
    # инвариант «нет мутаций удалённого», тот же гейт, что is_tenant_active
    # на пользовательских путях (miniapp/channel-link). Tenant уже загружен →
    # проверяем флаг напрямую.
    if tenant.deleted_at is not None:
        return RedirectResponse(
            url="/admin/users?reset=err&msg=tenant_deleted",
            status_code=303,
        )

    d: dict[str, int] = {}

    # #181 Фаза B: EDS Monitor полностью ретайрен — connect-слой
    # (connect_sessions / tenant_eds_accounts) и его secure_records дропнуты
    # миграцией; reset их больше не чистит.

    # 152-ФЗ Часть 2: audit log admin reset action до финального commit'а,
    # чтобы запись попала в одну транзакцию с deletes.
    from sreda.services.audit import audit_event

    try:
        # Events and outbox
        d["inbound_events"] = session.query(InboundEvent).filter_by(
            tenant_id=tenant_id
        ).delete()
        d["outbox"] = session.query(OutboxMessage).filter_by(
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

        audit_event(
            session,
            actor_type="admin",
            actor_id=principal.actor_id,
            action="admin.tenant.reset",
            resource_type="tenant",
            resource_id=tenant_id,
            metadata={
                "deleted_counts": {k: v for k, v in d.items() if v > 0},
            },
            commit=False,
        )

        session.commit()
    except Exception:  # noqa: BLE001
        # rollback-guard (аудит 2026-07-18 api-admin #5): при сбое — откат и
        # ЧЕСТНЫЙ err-редирект, а не «reset=ok» на пустом commit'е после
        # чужого rollback.
        session.rollback()
        logger.exception("admin tenant reset failed: tenant_id=%s", tenant_id)
        return RedirectResponse(
            url="/admin/users?reset=err&msg=reset_failed",
            status_code=303,
        )

    parts = [f"{k}={v}" for k, v in d.items() if v > 0]
    msg = "+".join(parts) if parts else "nothing+to+delete"

    return RedirectResponse(
        url=f"/admin/users?reset=ok&msg={msg}",
        status_code=303,
    )


@router.post("/tenant/approve")
async def admin_tenant_approve(
    tenant_id: str = Query(...),
    principal: AdminPrincipal = Depends(require_admin_token),
    session=Depends(_get_session),
):
    """410 Gone — Phase 2D removed manual approval.

    Phase 2 deploy moved auto-grant + auto-approve to onboarding hook
    (`services/onboarding._auto_approve_and_grant_free_tier`). This
    route returns 410 для одного релиза чтобы старые форм-bookmarks
    видели понятный код. Будет полностью удалён в follow-up release.

    #305: no ``require_csrf`` gate — this route mutates nothing (returns 410
    unconditionally for legacy bookmarks). Adding CSRF would only turn the
    intended 410 into a 403 for a stale form; кроме того форма удалена.
    """
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "410 Gone — manual approval removed. Tenants are auto-approved "
        "on first contact (Phase 2 of free-tier-subscription plan). "
        "See /admin/tenant/<id>/suspend для отключения abuse.",
        status_code=410,
    )


# Legacy admin_tenant_approve body preserved as comment в commit history
# (git show before commit deploying Phase 2D). Restorable если auto-grant
# нужно roll back. Phase 3 cleanup полностью удалит route.

# --- below is the legacy implementation, no longer reachable ---
async def _legacy_admin_tenant_approve_unused(
    tenant_id: str,
    token: str,
    session,
):
    """Unreachable. Documents legacy semantic for future reference."""
    from datetime import UTC
    from datetime import datetime as _dt

    from sreda.config.bot_registry import TelegramBotRegistry, telegram_client_for
    from sreda.db.models.core import Tenant, User
    from sreda.integrations.telegram.client import TelegramDeliveryError
    from sreda.services.onboarding import build_post_approve_message

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(
            url="/admin/users?approve=err&msg=tenant_not_found",
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

    # Proactive welcome: route в TG если есть telegram_account_id, в MAX
    # если есть max_chat_id (пользователь зарегался через MAX-only). Если
    # есть и то и то — шлём в оба канала.
    # Skipped if:
    # - tenant was already approved (idempotent re-click)
    # - no bot token configured (dev/test)
    # - user has neither telegram_account_id nor max_chat_id
    settings = get_settings()
    delivery_status = "skipped"
    if was_pending and user is not None:
        text = build_post_approve_message()
        delivery_results: list[str] = []

        if settings.telegram_bot_token and user.telegram_account_id:
            _registry = TelegramBotRegistry.from_settings(settings)
            client = telegram_client_for(_registry.system_default_bot_key, _registry)
            try:
                await client.send_message(
                    chat_id=user.telegram_account_id,
                    text=text,
                    reply_markup=None,
                )
                delivery_results.append("tg_ok")
            except TelegramDeliveryError:
                delivery_results.append("tg_error")

        if settings.max_bot_token and user.max_chat_id:
            from sreda.integrations.max.client import MaxClient
            max_client = MaxClient(token=settings.max_bot_token)
            try:
                await max_client.send_message(
                    recipient={"chat_id": user.max_chat_id},
                    text=text,
                )
                delivery_results.append("max_ok")
            except Exception:  # noqa: BLE001
                delivery_results.append("max_error")

        if delivery_results:
            delivery_status = "+".join(delivery_results)

    return RedirectResponse(
        url=(
            f"/admin/users?approve=ok&tenant={tenant_id}"
            f"&welcome={delivery_status}&grant={grant_status}"
        ),
        status_code=303,
    )


# --- Phase 2D: suspend / unsuspend tenant subscription -----------------
# Single-active-per-feature constraint (migration 0042) makes target
# row deterministic — filter by feature_key='housewife_assistant'.

@router.post("/tenant/{tenant_id}/suspend", response_class=HTMLResponse)
async def admin_tenant_suspend(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
    session=Depends(_get_session),
):
    """Suspend tenant's housewife_assistant subscription (status='active' → 'suspended').

    Phase 2D — replaces manual approve flow для abuse mitigation.
    Suspended tenants get UPGRADE_COPY['suspended'] copy from
    EntitlementGate when they message the bot.
    """
    from sreda.db.models.billing import TenantSubscription
    from sreda.services.audit import audit_event

    sub = (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == tenant_id,
            TenantSubscription.feature_key == "housewife_assistant",
            TenantSubscription.status == "active",
        )
        .first()
    )
    if sub is None:
        return RedirectResponse(
            url="/admin/users?suspend=err&msg=no_active_sub",
            status_code=303,
        )
    sub.status = "suspended"
    # Phase 2 (Codex MINOR fix 2026-05-07): bump updated_at для audit/debug.
    from datetime import UTC as _UTC
    sub.updated_at = datetime.now(_UTC)
    try:
        audit_event(
            session,
            actor_type="admin",
            actor_id=principal.actor_id,
            action="admin.tenant.suspend",
            resource_type="tenant",
            resource_id=tenant_id,
            commit=False,
        )
        session.commit()
    except Exception:  # noqa: BLE001
        # rollback-guard (аудит 2026-07-18 api-admin #5): честный
        # err-редирект вместо ложного «ok».
        session.rollback()
        logger.exception("admin tenant suspend failed: tenant_id=%s", tenant_id)
        return RedirectResponse(
            url="/admin/users?suspend=err&msg=suspend_failed",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/users?suspend=ok&tenant={tenant_id}",
        status_code=303,
    )


@router.post("/tenant/{tenant_id}/unsuspend", response_class=HTMLResponse)
async def admin_tenant_unsuspend(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
    session=Depends(_get_session),
):
    """Unsuspend (status='suspended' → 'active'). Reverses suspend."""
    from sreda.db.models.billing import TenantSubscription
    from sreda.services.audit import audit_event

    sub = (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == tenant_id,
            TenantSubscription.feature_key == "housewife_assistant",
            TenantSubscription.status == "suspended",
        )
        .first()
    )
    if sub is None:
        return RedirectResponse(
            url="/admin/users?unsuspend=err&msg=no_suspended_sub",
            status_code=303,
        )
    sub.status = "active"
    # Phase 2 (Codex MINOR fix 2026-05-07): bump updated_at для audit/debug.
    from datetime import UTC as _UTC
    sub.updated_at = datetime.now(_UTC)
    try:
        audit_event(
            session,
            actor_type="admin",
            actor_id=principal.actor_id,
            action="admin.tenant.unsuspend",
            resource_type="tenant",
            resource_id=tenant_id,
            commit=False,
        )
        session.commit()
    except Exception:  # noqa: BLE001
        # rollback-guard (аудит 2026-07-18 api-admin #5): честный
        # err-редирект вместо ложного «ok».
        session.rollback()
        logger.exception("admin tenant unsuspend failed: tenant_id=%s", tenant_id)
        return RedirectResponse(
            url="/admin/users?unsuspend=err&msg=unsuspend_failed",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/users?unsuspend=ok&tenant={tenant_id}",
        status_code=303,
    )


@router.post("/tenant/{tenant_id}/soft-delete", response_class=HTMLResponse)
async def admin_tenant_soft_delete(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
    session=Depends(_get_session),
):
    """#187 Phase 4b-1 — soft-delete a tenant (admin-only trigger, A15).

    Reversible: flips ``tenants.deleted_at`` + actively drains pending
    artefacts under the tenant advisory-lock barrier (see
    ``services.tenant_lifecycle.soft_delete_tenant``). Protected EXACTLY like
    the other admin actions (suspend/unsuspend): ``require_admin_token`` →
    reachable only on the admin surface (IP-locked vhost + admin auth), NOT a
    public endpoint. The ``admin.tenant.soft_delete`` audit row (A11) is
    written INSIDE ``soft_delete_tenant``, atomically with the flag under the
    same lock/transaction — no separate commit here.
    """
    from sreda.services.tenant_lifecycle import soft_delete_tenant

    changed = soft_delete_tenant(
        session,
        tenant_id,
        actor_type="admin",
        actor_id=principal.actor_id,
        source="admin",
    )
    status = "ok" if changed else "noop"
    return RedirectResponse(
        url=f"/admin/users?soft_delete={status}&tenant={tenant_id}",
        status_code=303,
    )


@router.post("/tenant/{tenant_id}/restore", response_class=HTMLResponse)
async def admin_tenant_restore(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_admin_token),
    _: None = Depends(require_csrf),
    session=Depends(_get_session),
):
    """#187 Phase 4b-1 — restore a soft-deleted tenant (admin-only, A15).

    Reverses ``admin_tenant_soft_delete``: clears ``deleted_at`` and cleans the
    deletion window ``[deleted_at, restored_at]`` under the same advisory-lock
    (see ``services.tenant_lifecycle.restore_tenant``). Same protection as every
    other admin action (``require_admin_token``). The ``admin.tenant.restore``
    audit row (A11) is written inside ``restore_tenant``, atomically with the
    flag.
    """
    from sreda.services.tenant_lifecycle import restore_tenant

    changed = restore_tenant(
        session,
        tenant_id,
        actor_type="admin",
        actor_id=principal.actor_id,
        source="admin",
    )
    status = "ok" if changed else "noop"
    return RedirectResponse(
        url=f"/admin/users?restore={status}&tenant={tenant_id}",
        status_code=303,
    )
