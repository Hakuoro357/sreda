"""Telegram Mini App router for subscription management.

Serves:
- GET /miniapp/           — HTML shell (SPA with hash-routing)
- GET/POST /miniapp/api/  — JSON API (requires initData auth)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_miniapp_rate_limit, get_session
from sreda.config.settings import get_settings
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.domain.tenants.features import is_feature_disabled
from sreda.features.app_registry import get_feature_registry
from sreda.features.contracts import MiniAppSection, MiniAppSectionsProvider
from sreda.services.agent_capabilities import active_feature_keys
from sreda.services.billing import (
    DISABLED_FEATURE_MESSAGE,
    BillingService,
)
from sreda.services.housewife_family import HousewifeFamilyService
from sreda.services.housewife_recipes import HousewifeRecipeService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.housewife_shopping import HousewifeShoppingService
from sreda.services.max_auth import (
    MaxInitDataError,
    resolve_tenant_from_max_account_id,
    validate_max_init_data,
)
from sreda.services.onboarding import (
    _stamp_last_bot_key,
    ensure_max_user_bundle,
    ensure_telegram_user_bundle_by_id,
)
from sreda.config.bot_registry import TelegramBotRegistry, telegram_client_for
from sreda.services.telegram_auth import (
    TelegramInitDataError,
    resolve_tenant_from_telegram_id,
    validate_telegram_init_data_any_bot,
)

logger = logging.getLogger(__name__)

# #181 Phase B: legacy EDS plan_keys. EDS Monitor is retired and its plan rows
# are dropped, but old Mini App clients / deep-links may still POST these keys.
# subscribe/cancel answer them with the disabled tombstone (200, no mutation).
_EDS_PLAN_KEYS = frozenset({"eds_monitor_base", "eds_monitor_extra_account"})

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
    workspace_id: str | None
    # Channel-agnostic identity (Phase 8 dual-platform). ``channel``
    # ∈ {"telegram", "max"}. ``account_id`` — the channel-native id
    # (telegram numeric id OR MAX user_id). External callers should
    # not branch on these — used only for diagnostic logging.
    channel: str = "telegram"
    account_id: str = ""


def _gate_miniapp_tenant_active(session: Session, tenant_id: str) -> None:
    """#187 Phase 4a — soft-delete gate для mini-app auth-пути.

    Зовётся из ``_require_miniapp_auth`` СРАЗУ после резолва существующего
    тенанта и ДО любой durable-мутации auth-слоя (TG ``_stamp_last_bot_key``,
    MAX ``max_chat_id``-refresh). Удалён → 410 Gone reason ``tenant_deleted``
    (НЕ 401/404 — иначе фронт уходит в re-provision-петлю, см. план
    db-fix-tenant-deletion-plan.md дверь #8). Lazy-provisioned юзер сюда не
    передаётся (только что создан, всегда активен) — false-positive нет.
    """
    from sreda.services.tenant_lifecycle import is_tenant_active

    if not is_tenant_active(session, tenant_id):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="tenant_deleted",
        )


def _require_miniapp_auth(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> MiniAppContext:
    """Channel-agnostic mini-app auth.

    Reads ``?platform=`` query param (default ``telegram`` for backward
    compat). Dispatches initData validation to TG or MAX validator,
    resolves tenant by appropriate id, lazy-provisions if missing.

    Phase 8 of MAX integration sprint — без этого dispatch'а MAX-юзер
    получал 401 на любом ``/api/v1/...`` (старая версия валидировала
    как TG-only). Mini-app frontend (`platform_bridge.js` в base.html)
    добавляет ``?platform=`` query на каждый fetch — backend дальше
    маршрутит.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("tma "):
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

    # Default channel = telegram для backward-compat (старые TG-юзеры
    # без ?platform=). Frontend platform_bridge.js всегда добавляет
    # query, но осторожный default нужен для прямых curl/тестов.
    channel = (request.query_params.get("platform") or "telegram").lower()
    if channel not in ("telegram", "max"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown platform: {channel!r}",
        )

    if channel == "telegram":
        _bot_registry = TelegramBotRegistry.from_settings(settings)
        try:
            resolved_bot_key, webapp_user = validate_telegram_init_data_any_bot(
                init_data_raw, _bot_registry,
            )
        except TelegramInitDataError as exc:
            logger.warning("miniapp auth failed (tg): %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_init_data",
            ) from exc

        account_id = webapp_user.telegram_id
        resolved = resolve_tenant_from_telegram_id(session, account_id)
        if resolved is None:
            display_name = (
                (webapp_user.first_name or "").strip()
                or (webapp_user.username or "").strip()
                or None
            )
            from sreda.services.signup_abuse import SignupBlocked as _SignupBlocked
            try:
                onboarding = ensure_telegram_user_bundle_by_id(
                    session,
                    telegram_id=account_id,
                    display_name=display_name,
                    bot_key=resolved_bot_key,
                )
                session.commit()
            except _SignupBlocked as exc:
                # Phase 2C: separate except clause (Xiaomi m5 fix —
                # mirror MAX pattern для consistency и safety —
                # generic except + isinstance fragile).
                session.rollback()
                logger.info(
                    "miniapp auth: signup blocked tg=%s reason=%s",
                    account_id, exc.reason,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"signup_blocked:{exc.reason}",
                )
            except Exception:
                session.rollback()
                logger.exception(
                    "miniapp auth: lazy provision failed for tg=%s",
                    account_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="provision_failed",
                )
            logger.info(
                "miniapp auth: lazily provisioned tg=%s tenant=%s user=%s new=%s",
                account_id, onboarding.tenant_id, onboarding.user_id,
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
            # #187 Phase 4a — soft-delete gate ДО любой durable-мутации
            # auth-слоя (_stamp_last_bot_key коммитит last_bot_key). Удалён
            # existing-тенант → 410 БЕЗ мутации. Гейт только для уже
            # резолвленного тенанта; lazy-provision-ветка (resolved is None)
            # сюда не попадает — там тенант только что создан, всегда активен.
            _gate_miniapp_tenant_active(session, tenant_id)
            # #109 (Codex MAJOR): existing TG-юзер открыл Mini App, но
            # /start не слал — здесь НЕ вызывается
            # ensure_telegram_user_bundle_by_id, поэтому _stamp_last_bot_key
            # не сработал бы. Мигрировавший юзер, который пользуется только
            # Mini App'ом @sreda_home_bot, остался бы со stale/NULL
            # last_bot_key → async-уведомления mis-route. Штампуем текущий
            # (Telegram) bot_key напрямую: helper идемпотентен (no-op +
            # без commit'а если значение не изменилось), пропускает пустой
            # bot_key. resolved_bot_key в TG-ветке всегда зарегистрированный
            # TG bot_key.
            from sreda.db.models.core import User as _User
            _user_row = session.get(_User, user_id)
            if _user_row is not None:
                _stamp_last_bot_key(session, _user_row, resolved_bot_key)

    else:  # channel == "max"
        if not settings.max_bot_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="max_bot_token_not_configured",
            )
        try:
            max_user = validate_max_init_data(
                init_data_raw, settings.max_bot_token,
            )
        except MaxInitDataError as exc:
            logger.warning("miniapp auth failed (max): %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_init_data",
            ) from exc

        account_id = max_user.max_user_id
        try:
            resolved = resolve_tenant_from_max_account_id(session, account_id)
        except MultipleResultsFound:
            # codex R2 MINOR #6: specific exception вместо bare ``Exception``.
            # `find_user_by_max_account_id` использует one_or_none(),
            # на duplicate `users.max_account_id` (нет UNIQUE constraint
            # в текущей schema) бросит MultipleResultsFound. Любые другие
            # DB ошибки пробрасываются вверх (FastAPI 500) с raw stack —
            # это правильно: «duplicate integrity» — не маска любого
            # falure'а, а конкретный case.
            logger.exception(
                "miniapp auth: duplicate users.max_account_id for max=%s — "
                "DATA INTEGRITY ERROR, нужен migration с UNIQUE partial index",
                account_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="duplicate_max_account_integrity",
            )

        if resolved is None:
            # Codex R1 MAJOR #6: admin alert на создание MAX-only tenant'а.
            # Boris's design (R5 channel-linking sprint): auto-merge запрещён,
            # юзер с TG-аккаунтом и MAX-аккаунтом — отдельные tenants до
            # explicit channel linking. Но для нового MAX-юзера без TG
            # это OK. Alert чтобы можно было через support слить если
            # это случайно existing TG-юзер (например, Boris вручную
            # SQL-merge, как 2026-05-05).
            #
            # Codex R3 MAJOR new: race condition защита. Mini-app при
            # первой загрузке параллельно fire'ит 4-5 fetch'ей
            # (summary/plans/menu/schedule/...), все попадают сюда для
            # нового MAX-юзера. ``ensure_max_user_bundle`` использует
            # deterministic PK (`f"user_max_{account_id}"`), параллельные
            # INSERT'ы получают IntegrityError. Catch + retry resolve.
            from sreda.services.signup_abuse import SignupBlocked as _SignupBlocked
            try:
                onboarding = ensure_max_user_bundle(
                    session,
                    max_account_id=account_id,
                    max_chat_id=max_user.max_chat_id,
                    display_name=(max_user.first_name or "").strip() or None,
                )
                session.commit()
            except _SignupBlocked as exc:
                # Phase 2C: signup abuse rejection — 429 с reason
                session.rollback()
                logger.info(
                    "miniapp auth max: signup blocked id=%s reason=%s",
                    account_id, exc.reason,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"signup_blocked:{exc.reason}",
                )
            except IntegrityError:
                session.rollback()
                logger.info(
                    "miniapp auth: lazy provision race for max=%s — "
                    "another request created tenant первым, re-resolving",
                    account_id,
                )
                resolved = resolve_tenant_from_max_account_id(session, account_id)
                if resolved is None:
                    # Резолв failed even after rollback — что-то еще
                    # сломано (не race), пропускаем как provision_failed.
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="provision_failed_after_race",
                    )
                tenant_id, user_id = resolved
                # skip the rest of the new-user branch (logging, alert)
                onboarding = None
            except Exception:
                session.rollback()
                logger.exception(
                    "miniapp auth: lazy provision failed for max=%s",
                    account_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="provision_failed",
                )
            # Skip block ниже если race-fallback уже set'нул tenant_id/user_id
            # из resolve (onboarding=None означает что provision сделал
            # другой parallel request).
            if onboarding is not None:
                logger.info(
                    "miniapp auth: lazily provisioned max=%s tenant=%s user=%s new=%s",
                    account_id, onboarding.tenant_id, onboarding.user_id,
                    onboarding.is_new_user,
                )
                if onboarding.is_new_user:
                    # codex R2 MAJOR fix: ``asyncio.create_task`` в sync FastAPI
                    # dependency не работает (нет running event loop в worker
                    # thread). Использовать ``background_tasks.add_task`` —
                    # FastAPI выполнит после response в правильном контексте.
                    # Best-effort: alert не блокирует юзера и не ломает auth.
                    try:
                        from sreda.services.admin_alerts import alert_admin_async
                        name = (max_user.first_name or "").strip() or "unknown"
                        background_tasks.add_task(
                            alert_admin_async,
                            f"🟢 New MAX tenant via mini-app lazy-provision\n"
                            f"max_account_id={account_id} name={name}\n"
                            f"tenant={onboarding.tenant_id}\n"
                            f"⚠ Если это existing TG-юзер — нужен manual merge через "
                            f"channel_link или SQL",
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "schedule admin alert на new max tenant failed",
                            exc_info=True,
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
            # #187 Phase 4a — soft-delete gate ДО durable-мутации
            # max_chat_id-refresh (session.commit ниже). Удалён existing
            # MAX-тенант → 410 БЕЗ записи max_chat_id. Гейт только для уже
            # резолвленного тенанта; lazy-provision/race-ветки (onboarding не
            # None / resolved-after-race) сюда не попадают — тенант только что
            # создан, всегда активен.
            _gate_miniapp_tenant_active(session, tenant_id)
            # Codex R1 MAJOR #4: refresh max_chat_id если изменился.
            # Сценарий: Boris вручную SQL-merge'нул tenant'ы; max_chat_id
            # был известен на момент merge'а. Если юзер удалит и пересоздаст
            # MAX-аккаунт (новый chat_id), без refresh outbound delivery
            # пойдёт на старый chat_id и упадёт.
            if max_user.max_chat_id:
                try:
                    from sreda.db.models.core import User
                    user_row = session.get(User, user_id)
                    if (
                        user_row is not None
                        and user_row.max_chat_id != max_user.max_chat_id
                    ):
                        logger.info(
                            "miniapp auth: refreshing max_chat_id user=%s "
                            "old=%r new=%s",
                            user_id, user_row.max_chat_id, max_user.max_chat_id,
                        )
                        user_row.max_chat_id = max_user.max_chat_id
                        session.commit()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "miniapp auth: max_chat_id refresh failed user=%s",
                        user_id, exc_info=True,
                    )
                    session.rollback()

    # #187 Phase 4a — soft-delete gate уже применён ДО мутаций auth-слоя:
    # для existing-тенанта (обе ветки TG/MAX) через _gate_miniapp_tenant_active
    # сразу после резолва; lazy-provisioned тенант только что создан и всегда
    # активен. Удалённый тенант → 410 БЕЗ stamp/refresh-мутаций.

    # Resolve workspace_id for connect link creation (channel-agnostic)
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
        workspace_id=workspace_id,
        channel=channel,
        account_id=account_id,
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
    # 2026-05-05 (Boris directive): conditional SDK injection per platform.
    # В МАКС-WebView'е не грузим TG SDK (telegram.org cross-origin) и
    # наоборот. Снижает cold-start time на ~50KB external script + блокирующий
    # HTTPS connect к telegram.org из MAX-app (наблюдалось 100+с задержки).
    # ``?platform=`` query param передаётся MAX из business.max.ru
    # registration URL и TG mini-app menu (если когда-то начнём).
    platform = (request.query_params.get("platform") or "telegram").lower()
    if platform not in ("telegram", "max"):
        platform = "telegram"  # fallback для безопасности
    template = _jinja_env.get_template("subscriptions.html")
    # Embed a server-side build stamp so we can confirm what code the
    # client actually received (iOS Telegram WebView caches aggressively
    # and bug reports sometimes involve stale HTML).
    build_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    html = template.render(
        base_url=base_url,
        build_stamp=build_stamp,
        platform=platform,
    )
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
    now = datetime.now(UTC)

    # Query plans and subscriptions directly
    plans_by_key = _plans_by_key(session)

    # #181 Phase B: EDS Monitor fully retired. extra_price_rub kept only for
    # response-shape stability; the EDS plan rows are dropped by migration so
    # this falls back to the historic default.
    extra_price_rub = 2990

    # Build active skills list (for main screen cards)
    active_skills: list[dict] = []
    available_skills: list[dict] = []

    # EDS Monitor — #181: retired skill, fully removed. No EDS card is emitted
    # into active_skills / available_skills.

    # Simple skills (one plan → one subscription → one card). Voice
    # transcription was removed in 2026-04 — it's now a capability
    # bundled with agents (see SkillManifestBase.includes_voice), not
    # a standalone subscription. Add any future simple agent here with
    # one tuple and no per-agent branching.
    # #200 (Фаза 0): резолвим простой скил по feature_key, НЕ по зашитому plan_key.
    # Подписка может быть на любом плане фичи (sreda_free / housewife_base /
    # grandfathered — после слияния все на sreda_free); карточка + plan_key берутся
    # из ПЛАНА активной подписки (для not-subscribed — из канонического public-плана).
    _simple_skills: list[tuple[str, str, str]] = [
        # (feature_key, default_title, icon)
        ("housewife_assistant", "Помощник домохозяйки", "🏠"),
    ]
    plans_by_id = {p.id: p for p in plans_by_key.values()}
    for feature_key, default_title, icon in _simple_skills:
        sub = _active_sub_by_feature(session, ctx.tenant_id, feature_key, now)
        if sub is not None:
            # Карточка и plan_key — из плана АКТИВНОЙ подписки (subscribe/cancel
            # должны целиться в него, а не в legacy housewife_assistant_base).
            plan = plans_by_id.get(sub.plan_id)
            price = plan.price_rub if plan else 0
            active_skills.append({
                "feature_key": feature_key,
                "title": (plan.title if plan else None) or default_title,
                "icon": icon,
                "summary_line": "Бесплатно" if price == 0 else f"{price:,} ₽/мес".replace(",", " "),
                "is_active": True,
                "plan_key": plan.plan_key if plan else feature_key,
                "description": (plan.description if plan else "") or "",
                "price_rub": price,
                "active_until": _iso(sub.active_until),
            })
        else:
            # Канонический public+active план фичи (после слияния — sreda_free).
            plan = _canonical_plan_for_feature(plans_by_key, feature_key)
            if plan is None:
                continue
            available_skills.append({
                "feature_key": feature_key,
                "plan_key": plan.plan_key,
                "title": plan.title or default_title,
                "icon": icon,
                "description": plan.description or "",
                "price_rub": plan.price_rub,
                "is_active": False,
            })

    # EDS subscriptions detail — #181 Phase B: retired and removed. Always empty.
    eds_subscriptions: list[dict] = []

    # #181 Phase B: next-payment figures are derived from the renewable non-EDS
    # subscriptions (voice / housewife). When nothing renews → 0 / null and the
    # SPA hides the payment banner.
    next_amount_rub, next_due = billing.next_payment_for_display(ctx.tenant_id)

    return {
        "next_payment_due_at": _iso(next_due),
        "next_amount_rub": next_amount_rub,
        "active_skills": active_skills,
        "available_skills": available_skills,
        "eds_subscriptions": eds_subscriptions,
        "extra_price_rub": extra_price_rub,
    }


def _plans_by_key(session: Session) -> dict[str, SubscriptionPlan]:
    """Load all plans indexed by plan_key."""
    plans = session.query(SubscriptionPlan).all()
    return {p.plan_key: p for p in plans}


def _active_sub_by_feature(
    session: Session, tenant_id: str, feature_key: str, now: datetime,
) -> TenantSubscription | None:
    """#200: активная подписка фичи через feature_key (не через зашитый plan_key).

    Партиальный unique-index гарантирует ≤1 active подписку на (tenant, feature_key),
    но подписка может быть на ЛЮБОМ плане фичи (sreda_free / housewife_base / grandfathered).
    """
    subs = (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == tenant_id,
            TenantSubscription.feature_key == feature_key,
        )
        .order_by(TenantSubscription.id)  # детерминизм (страховка, если active + scheduled_for_cancel)
        .all()
    )
    for sub in subs:
        if _is_active(sub, now):
            return sub
    return None


def _canonical_plan_for_feature(
    plans_by_key: dict[str, SubscriptionPlan], feature_key: str,
) -> SubscriptionPlan | None:
    """#200: какой план рекламировать not-subscribed-пользователю — public+active план
    фичи с наименьшим sort_order (sreda_free=0 → канонический free после слияния)."""
    candidates = [
        p for p in plans_by_key.values()
        if p.feature_key == feature_key and p.is_active and p.is_public
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (p.sort_order or 0, p.plan_key))


def _is_active(sub: TenantSubscription | None, now: datetime) -> bool:
    if sub is None or not sub.quantity or sub.quantity <= 0:
        return False
    if sub.status not in {"active", "scheduled_for_cancel"}:
        return False
    # #200: active_until is None = бессрочная (free/grandfathered) — НЕ отбрасывать.
    if sub.active_until is None:
        return True
    active_until = sub.active_until
    if active_until.tzinfo is None:
        active_until = active_until.replace(tzinfo=UTC)
    return active_until > now


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
            # Generic guard: a retired skill's plans are dropped from the public
            # catalog. EDS Monitor (#181) is fully removed — its rows no longer
            # exist; this keeps the guard for any future skill retirement.
            if not is_feature_disabled(p.feature_key)
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

    # #181 Phase B: EDS Monitor retired — old EDS plan_keys answer the disabled
    # tombstone (200, no mutation) instead of routing to the removed mutators.
    if plan_key in _EDS_PLAN_KEYS:
        return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}

    # Generic simple-skill path: any plan_key that exists in subscription_plans
    # with a feature_key can be (un)subscribed via start_simple_subscription.
    # Covers voice, housewife, any future simple skill without per-skill
    # branching.
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

    # #181 Phase B: EDS Monitor retired — disabled tombstone for old EDS keys.
    if plan_key in _EDS_PLAN_KEYS:
        return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}

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
    # #181 Phase B: resume only ever applied to the EDS base subscription, which
    # is retired. Disabled tombstone (200, no mutation).
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}


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


# #181 Phase B: EDS Monitor is fully retired (engine, billing read-path and
# DB tables removed). The /api/v1/eds/* routes below stay as hardcoded
# tombstones so old Mini App clients / deep-links get a clean "отключено"
# answer (200, no mutation) instead of a 404/500.


@router.get("/api/v1/eds/accounts")
def list_eds_accounts(
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"accounts": []}


@router.post("/api/v1/eds/accounts/{account_id}/cancel")
def cancel_eds_account(
    account_id: str,
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}


@router.post("/api/v1/eds/accounts/{account_id}/restore")
def restore_eds_account(
    account_id: str,
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}


@router.post("/api/v1/eds/slot/remove")
def remove_eds_slot(
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}


@router.post("/api/v1/eds/slot/restore")
def restore_eds_slot(
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE}


# ---------------------------------------------------------------------------
# JSON API — EDS connect (retired tombstone)
# ---------------------------------------------------------------------------


@router.post("/api/v1/eds/connect")
def eds_connect(
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE, "connect_url": None}


@router.post("/api/v1/eds/add-and-connect")
def eds_add_and_connect(
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    return {"ok": False, "message": DISABLED_FEATURE_MESSAGE, "connect_url": None}


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


def _task_dict(task, *, checklist_counts: dict | None = None) -> dict:
    """Serialize one Task row for the /schedule/week response.

    Time fields come back as ``"HH:MM"`` strings (LLM tools accept the
    same shape). ``has_reminder`` + ``reminder_offset_minutes`` let
    the Mini App render the 🔔 icon + "напомнить за N мин" label
    without an extra FK fetch per row.

    ``checklist_counts`` (R-33 inline accordion): optional dict
    {checklist_id: (pending, total)} — позволяет рендерить «N/M» counter
    рядом с linked task без N+1 query'ев. Caller batch-loads counters
    для linked checklist_id'ов одним GROUP BY.
    """
    cl_id = getattr(task, "checklist_id", None)
    cl_pending = None
    cl_total = None
    if cl_id and checklist_counts and cl_id in checklist_counts:
        cl_pending, cl_total = checklist_counts[cl_id]
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
        # R-33 (2026-05-15): optional 1-to-1 link к checklist. Mini-app
        # renders inline accordion с items if not None.
        "checklist_id": cl_id,
        "checklist_pending": cl_pending,
        "checklist_total": cl_total,
    }


def _batch_load_checklist_counts(
    session, *, tenant_id: str, user_id: str, checklist_ids: list[str],
) -> dict[str, tuple[int, int]]:
    """R-33 (2026-05-15): batch-fetch (pending, total) для linked checklist'ов.

    Single GROUP BY query вместо N+1 — для inline accordion в расписании,
    чтобы counter «3/3» был сразу в schedule response.

    Returns {checklist_id: (pending_count, total_count)}.
    """
    if not checklist_ids:
        return {}
    from sreda.db.models.checklists import Checklist, ChecklistItem
    from sqlalchemy import func, case

    # Ownership filter via Checklist join — defense-in-depth
    rows = (
        session.query(
            ChecklistItem.checklist_id,
            func.count().label("total"),
            func.sum(
                case((ChecklistItem.status == "pending", 1), else_=0)
            ).label("pending"),
        )
        .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
        .filter(
            Checklist.tenant_id == tenant_id,
            Checklist.user_id == user_id,
            ChecklistItem.checklist_id.in_(checklist_ids),
        )
        .group_by(ChecklistItem.checklist_id)
        .all()
    )
    result: dict[str, tuple[int, int]] = {}
    for row in rows:
        result[row.checklist_id] = (int(row.pending or 0), int(row.total or 0))
    # Defaults для linked-but-empty checklists (no items rows)
    for cid in checklist_ids:
        result.setdefault(cid, (0, 0))
    return result


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
    else:
        inbox_rows = []

    # R-33 (2026-05-15): batch-load checklist counts для всех linked tasks
    # одним GROUP BY — для inline accordion counter «N/M» без N+1 queries.
    all_linked_ids: set[str] = set()
    for t in inbox_rows:
        if getattr(t, "checklist_id", None):
            all_linked_ids.add(t.checklist_id)
    for day_tasks in per_day.values():
        for t in day_tasks:
            if getattr(t, "checklist_id", None):
                all_linked_ids.add(t.checklist_id)
    checklist_counts = _batch_load_checklist_counts(
        session,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        checklist_ids=list(all_linked_ids),
    )

    inbox = [
        _task_dict(t, checklist_counts=checklist_counts) for t in inbox_rows
    ]

    days = []
    cursor = week_start
    while cursor <= week_end:
        days.append({
            "date": cursor.isoformat(),
            "label": _day_label(cursor),
            "is_past": cursor < today,
            "tasks": [
                _task_dict(t, checklist_counts=checklist_counts)
                for t in per_day.get(cursor, [])
            ],
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


@router.get("/api/v1/checklist/{checklist_id}")
def get_checklist_endpoint(
    checklist_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """R-33 (2026-05-15): single checklist + items + linked_task_id (если есть).

    Used by Mini App для navigation `#/checklists/{id}` (detail view).
    Back-link «↩ к расписанию» рендерится если linked_task_id != null.

    Response:
        {
          "id": "checklist_xxx",
          "title": "Крой",
          "status": "active" | "archived",
          "pending": 4, "done": 2, "total": 6,
          "items": [{"id": "clitem_yyy", "title": "...", "status": "...", "position": N}, ...],
          "linked_task_id": "task_zzz" | null
        }

    Ownership: 404 if checklist missing or wrong tenant/user.
    """
    from sreda.db.models.checklists import Checklist, ChecklistItem
    from sreda.db.models.tasks import Task

    cl = (
        session.query(Checklist)
        .filter(
            Checklist.id == checklist_id,
            Checklist.tenant_id == ctx.tenant_id,
            Checklist.user_id == ctx.user_id,
        )
        .one_or_none()
    )
    if cl is None:
        raise HTTPException(status_code=404, detail="checklist_not_found")

    items = (
        session.query(ChecklistItem)
        .filter(ChecklistItem.checklist_id == checklist_id)
        .order_by(ChecklistItem.position, ChecklistItem.created_at)
        .all()
    )

    linked_task = (
        session.query(Task)
        .filter(
            Task.checklist_id == checklist_id,
            Task.tenant_id == ctx.tenant_id,
            Task.user_id == ctx.user_id,
        )
        .one_or_none()
    )

    pending = sum(1 for it in items if it.status == "pending")
    done = sum(1 for it in items if it.status == "done")

    return {
        "id": cl.id,
        "title": cl.title,
        "status": cl.status,
        "pending": pending,
        "done": done,
        "total": len(items),
        "items": [
            {
                "id": it.id,
                "title": it.title,
                "status": it.status,
                "position": it.position,
            }
            for it in items
        ],
        "linked_task_id": linked_task.id if linked_task else None,
    }


@router.post("/api/v1/checklist/{checklist_id}/archive")
def archive_checklist_endpoint(
    checklist_id: str,
    session: Session = Depends(get_session),
    ctx: MiniAppContext = Depends(_require_miniapp_auth),
) -> dict:
    """R-31 (2026-05-15): archive checklist from Mini App.

    User feedback (tg_755682022): «дать возможность удалять списки дел...
    добавить иконку слева в титул списка. Удалять с подтверждением без
    перезагрузки страницы».

    Semantics:
    - «Delete» в UI = soft-archive в backend (status='archived'). Items
      сохраняются в БД для post-hoc analysis / future restore (Boris's
      directive: «не удаляй users' data сразу»).
    - Idempotent: already-archived → 200 ``{"ok": True, "already_archived": True}``
      (R6 review fix — не возвращаем 404 для idempotent retry).
    - Ownership guard: checklist должен принадлежать (ctx.tenant_id, ctx.user_id).
      Чужой / nonexistent → 404.

    R-33 phase will extend: при archive linked checklist → автоматически
    unlink `task.checklist_id = NULL`. R-31 (этот endpoint) deploys
    первым; Task.checklist_id column ещё не существует, поэтому unlink
    логика добавится в R-33.
    """
    from sreda.db.models.checklists import Checklist
    from sreda.services.checklists import ChecklistService

    # Ownership check
    cl = (
        session.query(Checklist)
        .filter(
            Checklist.id == checklist_id,
            Checklist.tenant_id == ctx.tenant_id,
            Checklist.user_id == ctx.user_id,
        )
        .one_or_none()
    )
    if cl is None:
        raise HTTPException(status_code=404, detail="checklist_not_found")

    if cl.status == "archived":
        # R6 review: idempotent — useful for retry / double-click scenarios.
        return {"ok": True, "already_archived": True}

    # R-33 (2026-05-15): auto-unlink linked task before archive. После
    # R-33 migration `Task.checklist_id` существует. Если checklist linked
    # с task'ом → set task.checklist_id = NULL чтобы task survived (R-31
    # soft-delete semantics: task → undated, без details checklist).
    # Mini-app schedule view для unlinked task снова показывает full title
    # без 📋 button.
    from sreda.db.models.tasks import Task
    linked_task = (
        session.query(Task)
        .filter(
            Task.checklist_id == checklist_id,
            Task.tenant_id == ctx.tenant_id,
            Task.user_id == ctx.user_id,
        )
        .one_or_none()
    )
    if linked_task is not None:
        linked_task.checklist_id = None
        # Flush before archive_list (which commits) — keeps changes atomic.
        session.flush()

    svc = ChecklistService(session)
    archived = svc.archive_list(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, list_id=checklist_id,
    )
    if archived is None:
        # Race condition: archive_list lost it (concurrent delete or status flip)
        raise HTTPException(status_code=404, detail="checklist_not_found")

    return {"ok": True, "already_archived": False}


# ────────────────────────────────────────────────────────────────────
# Channel linking endpoints (Phase 7 of MAX integration sprint, 2026-05-04)
#
# Architecture (Boris's option C, post-probe pivot):
# - Юзер в mini-app source-channel (TG или MAX) → POST start
# - Backend генерит token, return deep_link для target-channel mini-app
# - Юзер открывает deep-link в target → mini-app target открывается с
#   initData.start_param=lnk_<token>
# - Mini-app target POST'ит consume с full initData (target-signed)
# - Backend validates target initData → extracts max_account_id или
#   telegram_id → atomic consume + link
# - Source mini-app polling на status endpoint видит used_at → success
#
# Deps:
# - ``platform_bridge.js`` (Phase 8) шлёт ``?platform=`` query всегда
# - ``services.channel_linking`` уже готов
# - ``services.max_auth.validate_max_init_data`` (TG-pattern guess —
#   verify в Phase 11 live test)
# ────────────────────────────────────────────────────────────────────


def _gate_channel_link_tenant_active(session: Session, tenant_id: str) -> None:
    """#187 Phase 4a — soft-delete gate для channel-link auth-пути.

    Зовётся из ``_resolve_platform_auth`` СРАЗУ после резолва существующего
    тенанта (но ДО мутаций в эндпойнтах consume/cancel/start). Удалён → 410
    Gone reason ``tenant_deleted`` (симметрично ``_require_miniapp_auth``;
    НЕ 401/404 чтобы фронт не уходил в re-provision-петлю). Новый юзер
    (резолв=None) сюда не попадает — гейт только для существующего тенанта.
    """
    from sreda.services.tenant_lifecycle import is_tenant_active

    if not is_tenant_active(session, tenant_id):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="tenant_deleted",
        )


def _resolve_platform_auth(
    request: Request,
    settings,
    session: Session | None = None,
) -> tuple[str, dict]:
    """Validate initData по claimed `?platform=` query param.

    Returns ``(platform, validated_payload)`` где payload — dict с
    {tenant_id, user_id, account_id, chat_id, start_param} extracted
    from initData. Fails-explicit (R2#6): no sniffing fallback, no
    defaulting на TG.
    """
    platform = request.query_params.get("platform")
    if not platform:
        raise HTTPException(
            status_code=400, detail="platform query param required",
        )

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("tma "):
        raise HTTPException(status_code=401, detail="missing_auth")
    init_data_raw = auth_header[4:]

    if platform == "telegram":
        _bot_registry = TelegramBotRegistry.from_settings(settings)
        try:
            _resolved_bot_key, tg_user = validate_telegram_init_data_any_bot(
                init_data_raw, _bot_registry,
            )
        except TelegramInitDataError as exc:
            raise HTTPException(status_code=401, detail="invalid_init_data") from exc
        payload = {
            "telegram_id": tg_user.telegram_id,
            "first_name": tg_user.first_name,
            "username": tg_user.username,
            "start_param": tg_user.start_param,
            "bot_key": _resolved_bot_key,
        }
        if session is not None:
            resolved = resolve_tenant_from_telegram_id(session, tg_user.telegram_id)
            if resolved is not None:
                tenant_id, user_id = resolved
                payload["tenant_id"] = tenant_id
                payload["user_id"] = user_id
                _gate_channel_link_tenant_active(session, tenant_id)
        return ("telegram", payload)

    if platform == "max":
        from sreda.services.max_auth import (
            MaxInitDataError, validate_max_init_data,
        )
        if not settings.max_bot_token:
            raise HTTPException(status_code=500, detail="max_bot_token_not_configured")
        try:
            max_user = validate_max_init_data(init_data_raw, settings.max_bot_token)
        except MaxInitDataError as exc:
            raise HTTPException(status_code=401, detail="invalid_init_data") from exc
        payload = {
            "max_user_id": max_user.max_user_id,
            "max_chat_id": max_user.max_chat_id,
            "first_name": max_user.first_name,
            "username": max_user.username,
            "start_param": max_user.start_param,
        }
        if session is not None:
            resolved = resolve_tenant_from_max_account_id(session, max_user.max_user_id)
            if resolved is not None:
                tenant_id, user_id = resolved
                payload["tenant_id"] = tenant_id
                payload["user_id"] = user_id
                _gate_channel_link_tenant_active(session, tenant_id)
        return ("max", payload)

    raise HTTPException(status_code=400, detail=f"unknown platform: {platform!r}")


def _validate_token_format(raw_token: str) -> None:
    """Validate opaque channel-link token before hashing/DB lookup."""
    try:
        from sreda.services.channel_linking import TOKEN_PATTERN
    except ImportError:
        token_pattern = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
    else:
        token_pattern = TOKEN_PATTERN
    if not isinstance(raw_token, str) or token_pattern.fullmatch(raw_token) is None:
        raise HTTPException(status_code=400, detail="invalid_token_format")


def _channel_link_token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def _json_body(request: Request) -> dict:
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    return body if isinstance(body, dict) else {}


def _consume_outcome_dict(outcome) -> dict:
    return {
        "ok": bool(outcome.success),
        "error": outcome.error,
        "tenant_id": outcome.tenant_id,
        "idempotent": outcome.idempotent,
    }


@router.post("/api/v1/channel-link/start")
async def channel_link_start(
    request: Request,
    session: Session = Depends(get_session),
):
    """Initiate a channel-linking attempt. Юзер сейчас в source channel
    (mini-app), хочет привязать opposite channel.

    Возвращает opaque token + deep_link для target-channel bot.
    """
    settings = get_settings()
    platform, payload = _resolve_platform_auth(request, settings, session)

    # Resolve tenant_id из source platform
    if platform == "telegram":
        tenant_id = payload.get("tenant_id")
        source_user_id = payload.get("user_id")
        # bot_key is set by _resolve_platform_auth for the telegram branch.
        source_bot_key: str = payload.get("bot_key") or "sreda"
        if tenant_id is None or source_user_id is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
    else:  # max
        from sreda.services.onboarding import find_user_by_max_account_id
        user = find_user_by_max_account_id(session, payload["max_user_id"])
        if user is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
        tenant_id = user.tenant_id
        source_user_id = user.id
        source_bot_key = "sreda"  # MAX platform; no per-bot dispatch here

    from sreda.services.channel_linking import (
        ChannelLinkRateLimitedError, start_link,
    )

    # Server-side recheck: if tenant already has the target channel linked,
    # reject before creating a new token. UI hide is insufficient.
    if platform == "telegram":
        target_channel = "max"
    else:
        target_channel = "telegram"

    from sreda.services.channel_linking import is_account_already_linked
    if is_account_already_linked(session, tenant_id=tenant_id,
                                 target_channel=target_channel):
        raise HTTPException(409, "already_linked")

    # Resolve per-bot username/shortname for the deep-link.
    # When the target is telegram we use the SAME bot the user opened the
    # Mini App from — they should land back in the same bot's Mini App.
    _bot_registry = TelegramBotRegistry.from_settings(settings)
    if target_channel == "telegram":
        # Telegram source → land back in the SAME bot the user opened from.
        # MAX source → there is no per-bot Telegram context, so use the SYSTEM
        # DEFAULT Telegram bot (honors a future primary flip; Codex R2 high —
        # do NOT hardcode "sreda").
        _tg_bot_key = (
            source_bot_key if platform == "telegram"
            else _bot_registry.system_default_bot_key
        )
        _link_bot_cfg = _bot_registry.resolve(_tg_bot_key)
        _tg_link_username = _link_bot_cfg.username
        _tg_link_shortname = _link_bot_cfg.miniapp_shortname
    else:
        # Target is MAX (source telegram) — Telegram username/shortname unused.
        _link_bot_cfg = _bot_registry.resolve(_bot_registry.system_default_bot_key)
        _tg_link_username = _link_bot_cfg.username
        _tg_link_shortname = _link_bot_cfg.miniapp_shortname

    try:
        result = start_link(
            session,
            tenant_id=tenant_id,
            source_channel=platform,
            source_user_id=source_user_id,
            tg_bot_username=_tg_link_username,
            tg_miniapp_shortname=_tg_link_shortname,
        )
    except ChannelLinkRateLimitedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        if "tg_bot_username" in detail or "tg_miniapp_shortname" in detail:
            raise HTTPException(status_code=500, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc

    # Audit log → admin chat (R5 hardening)
    try:
        from sreda.services.admin_alerts import alert_admin_async
        await alert_admin_async(
            f"🟡 Channel link initiated: tenant={tenant_id} "
            f"source={platform} target={result.target_channel}"
        )
    except Exception:  # noqa: BLE001
        logger.debug("admin alert failed (non-fatal)", exc_info=True)

    # Send deep-link to source channel chat — простой UX: юзер видит
    # ссылку прямо в bot чате, тапает напрямую (system-level URL routing
    # → MAX/TG app), без copy/paste workflow в miniapp.
    target_label = "MAX" if result.target_channel == "max" else "Telegram"
    msg_text = (
        f"Чтобы привязать аккаунт {target_label}, открой эту ссылку "
        f"(она действует 5 минут):\n\n{result.deep_link}\n\n"
        f"Откроется бот в {target_label} — подтверди связь там."
    )
    try:
        from sreda.db.models.core import User as _User
        if platform == "telegram":
            source_user_row = session.get(_User, source_user_id)
            tg_chat_id = source_user_row.telegram_account_id if source_user_row else None
            if tg_chat_id:
                tg_client = telegram_client_for(source_bot_key, _bot_registry)
                await tg_client.send_message(chat_id=str(tg_chat_id), text=msg_text)
        else:  # platform == "max"
            from sreda.integrations.max.client import MaxClient
            source_user_row = session.get(_User, source_user_id)
            max_chat_id = source_user_row.max_chat_id if source_user_row else None
            if max_chat_id and settings.max_bot_token:
                max_client = MaxClient(token=settings.max_bot_token)
                await max_client.send_message(
                    recipient={"chat_id": max_chat_id}, text=msg_text,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("channel-link: send deep-link to source chat failed: %s", exc)

    return {
        "id": result.id,
        "target_channel": result.target_channel,
        "expires_at": result.expires_at.isoformat(),
        "sent_to_chat": True,  # frontend signal close miniapp без card
    }


@router.post("/api/v1/channel-link/consume")
async def channel_link_consume(
    request: Request,
    session: Session = Depends(get_session),
):
    """Consume a linking token. Юзер сейчас в target channel mini-app
    (открыл deep-link от source mini-app), backend получает target initData
    с start_param=lnk_<token>.

    Body должен содержать explicit ``raw_token``. Target identity берём
    только из validated initData, не из client-supplied JSON.
    """
    settings = get_settings()
    platform, payload = _resolve_platform_auth(request, settings, session)

    body = await _json_body(request)
    raw_token = body.get("raw_token") if isinstance(body, dict) else None

    if not raw_token:
        raise HTTPException(
            status_code=400, detail="missing_raw_token",
        )
    _validate_token_format(raw_token)

    # Determine target_account_id from validated initData
    if platform == "max":
        target_account_id = payload["max_user_id"]
        target_chat_id = payload["max_chat_id"]
    else:
        target_account_id = payload["telegram_id"]
        target_chat_id = None

    from sreda.services.channel_linking import consume_link
    outcome = consume_link(
        session,
        raw_token=raw_token,
        target_channel=platform,
        target_account_id=target_account_id,
        target_chat_id=target_chat_id,
    )

    # Audit log → admin chat
    try:
        from sreda.services.admin_alerts import alert_admin_async
        if outcome.success:
            await alert_admin_async(
                f"✅ Channel linked: tenant={outcome.tenant_id} "
                f"target={platform}={target_account_id}"
            )
        elif outcome.error == "collision":
            await alert_admin_async(
                f"⚠ Channel link collision: tenant={outcome.tenant_id} "
                f"target={platform}={target_account_id}"
            )
    except Exception:  # noqa: BLE001
        logger.debug("admin alert failed (non-fatal)", exc_info=True)

    return _consume_outcome_dict(outcome)


@router.post("/api/v1/channel-link/cancel")
async def channel_link_cancel(
    request: Request,
    session: Session = Depends(get_session),
):
    """Invalidate a pending token from the target-side mini-app."""
    settings = get_settings()
    platform, _payload = _resolve_platform_auth(request, settings, session)
    body = await _json_body(request)
    raw_token = body.get("raw_token") if isinstance(body, dict) else None
    if not raw_token:
        raise HTTPException(status_code=400, detail="missing_raw_token")
    _validate_token_format(raw_token)

    from sreda.db.models.channel_linking import ChannelLinkToken

    token_hash = _channel_link_token_hash(raw_token)

    # #187 Phase 4a — soft-delete пред-чтение SOURCE-тенанта ДО мутации used_at.
    # _resolve_platform_auth гейтит только TARGET (запрашивающего), НЕ владельца
    # токена. Без этого активный target мог бы отменить токен, чей source-тенант
    # уже soft-deleted → durable-мутация (used_at) для удалённого тенанта,
    # нарушая инвариант «нет мутаций удалённого». Читаем ТОЛЬКО колонку
    # ``tenant_id`` (скаляр, не ORM-сущность — как в consume_link), чтобы не
    # засорять identity-map naive ``expires_at`` строкой. Удалён → отклонить БЕЗ
    # мутации, тот же контракт-ответ что на невалидный/уже-использованный токен
    # ({"ok": False}). Нет строки (None) / активен — обычный путь.
    from sreda.services.tenant_lifecycle import is_tenant_active

    _src_tenant_id = session.execute(
        select(ChannelLinkToken.tenant_id).where(
            ChannelLinkToken.token_hash == token_hash
        )
    ).scalar_one_or_none()
    if _src_tenant_id is not None and not is_tenant_active(session, _src_tenant_id):
        return {"ok": False}

    result = session.execute(
        update(ChannelLinkToken)
        .where(
            ChannelLinkToken.token_hash == token_hash,
            ChannelLinkToken.target_channel == platform,
            ChannelLinkToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )
    session.commit()
    return {"ok": bool(result.rowcount)}


@router.get("/api/v1/channel-link/account-status")
async def channel_link_account_status(
    request: Request,
    session: Session = Depends(get_session),
):
    """Return whether current user's opposite-channel account is linked."""
    settings = get_settings()
    platform, payload = _resolve_platform_auth(request, settings, session)

    from sreda.db.models.core import User
    from sreda.services.onboarding import find_user_by_max_account_id

    if platform == "telegram":
        user_id = payload.get("user_id")
        user = session.get(User, user_id) if user_id else None
        if user is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
        return {
            "linked": user.max_account_id is not None,
            "target_channel": "max",
        }

    user = find_user_by_max_account_id(session, payload["max_user_id"])
    if user is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    return {
        "linked": user.tg_account_hash is not None,
        "target_channel": "telegram",
    }


@router.get("/api/v1/channel-link/status")
async def channel_link_status(
    request: Request,
    id: str,
    session: Session = Depends(get_session),
):
    """Polling endpoint для source-side mini-app. Возвращает текущий
    used_at статус токена. Frontend пуллит каждые 2с до used_at != null
    или истечения expires_at.

    Ownership check: caller must own the token (same tenant_id).
    Returns 404 instead of 403 to avoid leaking cross-tenant token
    existence.
    """
    settings = get_settings()
    platform, payload = _resolve_platform_auth(request, settings, session)

    # Resolve caller's tenant_id for ownership check.
    caller_tenant_id: str | None = None
    if platform == "telegram":
        caller_tenant_id = payload.get("tenant_id")
    else:  # max
        from sreda.services.onboarding import find_user_by_max_account_id

        user = find_user_by_max_account_id(session, payload["max_user_id"])
        caller_tenant_id = user.tenant_id if user else None

    if not caller_tenant_id:
        raise HTTPException(status_code=401, detail="tenant_not_resolved")

    from sreda.db.models.channel_linking import ChannelLinkToken
    row = session.get(ChannelLinkToken, id)
    if row is None or row.tenant_id != caller_tenant_id:
        raise HTTPException(status_code=404, detail="not_found")

    return {
        "id": row.id,
        "used_at": row.used_at.isoformat() if row.used_at else None,
        "expires_at": row.expires_at.isoformat(),
        "consumed": row.used_at is not None,
    }
