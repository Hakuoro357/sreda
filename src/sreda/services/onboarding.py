from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.core import Assistant, Tenant, User, Workspace
from sreda.db.repositories.seed import insert_only_bundle
from sreda.services.signup_abuse import SignupAbuseGuard, SignupBlocked

logger = logging.getLogger(__name__)

CONNECT_EDS_CALLBACK = "onboarding:connect_eds"


# Phase 2C: free-tier subscription auto-grant.
# `_auto_approve_and_grant_free_tier` вызывается ПОСЛЕ tenant bundle
# created, mark tenant approved + insert sreda_free subscription.
# Tenant becomes immediately usable (no manual admin approve).

def _auto_approve_and_grant_free_tier(session: Session, tenant_id: str) -> None:
    """Idempotent: marks tenant approved + grants sreda_free sub.

    Called после `SeedRepository.ensure_tenant_bundle()` для new tenant.
    Skip silently если sreda_free plan не существует (например на legacy
    DB до миграции 0041) — caller log warning.
    """
    now = datetime.now(timezone.utc)

    # Mark approved (idempotent — skip if already set)
    tenant = session.get(Tenant, tenant_id)
    if tenant is not None and tenant.approved_at is None:
        tenant.approved_at = now

    # Resolve sreda_free plan
    free_plan = (
        session.query(SubscriptionPlan)
        .filter(SubscriptionPlan.plan_key == "sreda_free")
        .one_or_none()
    )
    if free_plan is None:
        logger.warning(
            "auto_approve: sreda_free plan не существует — пропускаю "
            "subscription grant для tenant=%s. Проверь migration 0041 "
            "applied.", tenant_id,
        )
        session.commit()
        return

    # Idempotent: skip if tenant уже has active sub on housewife_assistant
    existing = (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == tenant_id,
            TenantSubscription.feature_key == "housewife_assistant",
            TenantSubscription.status == "active",
        )
        .first()
    )
    if existing is not None:
        session.commit()
        return

    # Insert sreda_free subscription. Event listener
    # `_sync_subscription_feature_key` populates feature_key из plan.
    session.add(TenantSubscription(
        id="sub_" + uuid.uuid4().hex[:24],
        tenant_id=tenant_id,
        plan_id=free_plan.id,
        feature_key=free_plan.feature_key,
        status="active",
        starts_at=now,
        active_until=None,
        cancel_at_period_end=False,
        quantity=1,
        next_cycle_quantity=1,
        created_at=now,
        updated_at=now,
    ))
    session.commit()


def _grant_free_tier_insert_only(session: Session, tenant_id: str) -> None:
    """#138 Ф5-5b: free-tier грант для СВЕЖЕГО тенанта — INSERT-only, без
    pre-SELECT tenant_subscriptions (под identity-роль такой SELECT недоступен).

    Вызывается ТОЛЬКО из провижн-пути нового юзера (``created=True``) — тенант
    только что создан в той же транзакции, активной подписки быть не может,
    idempotency-проверка не нужна. plan_id/feature_key: на PG — через DEFINER
    ``sreda_free_plan()`` (R2 Codex: identity без прямого SELECT даже на публичный
    справочник тарифов); на sqlite — ORM. approved_at уже проставлен в INSERT
    tenants (insert_only_bundle) — UPDATE не требуется.
    """
    now = datetime.now(timezone.utc)
    plan_id, feature_key = _resolve_free_plan(session)
    if plan_id is None:
        # R2-2 (Codex high R2 MAJOR): НЕ warning+return (иначе одобренный тенант
        # без подписки, а повторный вход найдёт existing-юзера и грант не выдаст).
        # Провижн-ошибка → исключение → provision откатывает ВЕСЬ бандл.
        raise RuntimeError(
            f"provision: sreda_free plan отсутствует (tenant={tenant_id}); "
            "проверь migration 0041 — провижн прерван, бандл откачен"
        )
    session.add(TenantSubscription(
        id="sub_" + uuid.uuid4().hex[:24],
        tenant_id=tenant_id,
        plan_id=plan_id,
        feature_key=feature_key,
        status="active",
        starts_at=now,
        active_until=None,
        cancel_at_period_end=False,
        quantity=1,
        next_cycle_quantity=1,
        created_at=now,
        updated_at=now,
    ))
    # R2 (Codex high CRIT-3): без commit — коммитит provision_new_tenant_bundle
    # одной транзакцией (атомарность гард+бандл+грант).


def _resolve_free_plan(session: Session) -> tuple[str | None, str | None]:
    """(plan_id, feature_key) тарифа sreda_free. PG — DEFINER sreda_free_plan()
    (identity без SELECT-гранта на subscription_plans); sqlite — ORM."""
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        row = session.execute(
            text("SELECT plan_id, feature_key FROM sreda_free_plan()")
        ).first()
        return (row[0], row[1]) if row else (None, None)
    plan = (
        session.query(SubscriptionPlan)
        .filter(SubscriptionPlan.plan_key == "sreda_free")
        .one_or_none()
    )
    return (plan.id, plan.feature_key) if plan is not None else (None, None)


def provision_new_tenant_bundle(
    session: Session,
    *,
    guard: SignupAbuseGuard,
    channel: str,
    external_id: str,
    tenant_id: str,
    display_name: str,
    workspace_id: str,
    user_id: str,
    assistant_id: str,
    telegram_account_id: str | None = None,
    max_account_id: str | None = None,
    max_chat_id: str | None = None,
    last_bot_key: str | None = None,
) -> bool:
    """#138 Ф5-5b: АТОМАРНЫЙ провижн НОВОГО юзера под identity-скоупом.

    Всё в ОДНОЙ транзакции ``session`` (identity/privileged), ОДИН commit в конце
    (R2 Codex high CRIT-3): анти-абьюз-гард (advisory-лок + DEFINER-счётчики +
    запись попытки) → bundle (INSERT-only, approved_at+last_bot_key в INSERT) →
    free-tier подписка (при реальной вставке). Advisory-лок держится до commit —
    cap сериализован, cap-checked-and-granted атомарно.

    ``guard`` строится вызывающим на APP-сессии (читает runtime_config — identity
    не может); значения лимитов уже внутри guard. Бросает SignupBlocked (session
    откатится при выходе из privileged-контекста). Возвращает ``created``.
    """
    allowed, reason = guard.check_inside_tx(session, channel, external_id)
    if not allowed:
        raise SignupBlocked(reason)
    created = insert_only_bundle(
        session,
        tenant_id=tenant_id,
        tenant_name=display_name,
        workspace_id=workspace_id,
        workspace_name=display_name,
        user_id=user_id,
        telegram_account_id=telegram_account_id,
        max_account_id=max_account_id,
        max_chat_id=max_chat_id,
        last_bot_key=last_bot_key,
        assistant_id=assistant_id,
        assistant_name="Среда",
    )
    if created:
        _grant_free_tier_insert_only(session, tenant_id)
    session.commit()  # единственный commit — атомарность гард+бандл+грант
    return created


def _stamp_last_bot_key(session: Session, user: User, bot_key: str | None) -> None:
    """Record the bot the user is CURRENTLY messaging on (#109).

    Async producers (reminder / proactive / onboarding workers) read
    ``user.last_bot_key`` via ``resolve_outbox_routings`` to deliver
    notifications to the user's current bot rather than the bot frozen at
    reminder-creation time. Without this, users who migrated from
    ``@sreda01_bot`` (``"sreda"``) to ``@sreda_home_bot`` (``"sreda_home"``)
    lost their async notifications on the abandoned bot.

    Only writes (and commits) when the value actually changes — keeps the
    happy path a single SELECT + no-op for users who stay on one bot.
    Caller passes the TG ``bot_key`` of the inbound; MAX inbound paths do
    not call this (bug is TG-specific; the routing guard ignores non-TG
    keys anyway).
    """
    if not bot_key:
        return
    if user.last_bot_key != bot_key:
        user.last_bot_key = bot_key
        session.commit()


def find_user_by_chat_id(session: Session, chat_id: str | int) -> User | None:
    """Резолв юзера по telegram chat_id через hash-lookup.

    Раньше: ``filter(User.telegram_account_id == chat_id)``.
    Теперь: считаем HMAC-SHA256 от chat_id под salt'ом и ищем по
    `tg_account_hash` (индексированная колонка).

    152-ФЗ Часть 1: plaintext chat_id больше нигде в коде не равняется
    напрямую к колонке БД — только через hash. Это нужно, чтобы в
    дампе БД не было plain Telegram-идентификаторов.

    Возвращает None, если salt не сконфигурирован (RuntimeError ловится
    выше — это критичная ошибка деплоя, а не пользовательская).
    """
    if chat_id is None:
        return None
    cid = str(chat_id).strip()
    if not cid:
        return None
    from sreda.services.tg_account_hash import hash_tg_account

    tg_hash = hash_tg_account(cid)
    return (
        session.query(User)
        .filter(User.tg_account_hash == tg_hash)
        .one_or_none()
    )


@dataclass(slots=True)
class TelegramOnboardingResult:
    is_new_user: bool
    chat_id: str | None
    tenant_id: str | None
    workspace_id: str | None
    user_id: str | None
    assistant_id: str | None


def _serve_existing_bundle_reads(
    resolved, *, tg_bot_key: str | None = None, update_max_chat_id: str | None = None
) -> tuple[str | None, str | None]:
    """#138 Ф5-5c: обслуживание СУЩЕСТВУЮЩЕГО юзера под правильными ролями (чтобы
    после флипа DSN на sreda_app не сломаться о RLS).

    tenant-чтения (assistant/workspace) + ``_stamp_last_bot_key`` (UPDATE users
    СВОЕЙ строки) — под ``tenant_session(tenant_id)``: app-роль + tenant_ctx →
    политика ``p_{t}_tenant`` (FOR ALL) пускает свой тенант. Репаир pending/no-sub
    тенанта (``_auto_approve``: UPDATE tenants — app НЕ может, ``p_tenants_self`` =
    SELECT-only) — под ``privileged_session("admin")`` (maintenance, FOR ALL), и
    ТОЛЬКО если реально pending/без активной free-подписки (для здорового юзера —
    ноль записей). Возвращает (workspace_id, assistant_id)."""
    from sreda.db.session import privileged_session, tenant_session

    tid = resolved.tenant_id
    with tenant_session(tid) as ts:
        if tg_bot_key is not None or update_max_chat_id is not None:
            user = ts.get(User, resolved.user_id)
            if user is not None and tg_bot_key is not None:
                _stamp_last_bot_key(ts, user, tg_bot_key)
            if user is not None and update_max_chat_id is not None \
                    and user.max_chat_id != update_max_chat_id:
                user.max_chat_id = update_max_chat_id  # UPDATE users own — RLS ok
                ts.commit()
        assistant = (
            ts.query(Assistant)
            .filter(Assistant.tenant_id == tid)
            .order_by(Assistant.id.asc())
            .first()
        )
        workspace_id = assistant.workspace_id if assistant is not None else None
        if workspace_id is None:
            ws = (
                ts.query(Workspace)
                .filter(Workspace.tenant_id == tid)
                .order_by(Workspace.id.asc())
                .first()
            )
            workspace_id = ws.id if ws is not None else None
        assistant_id = assistant.id if assistant is not None else None
        has_active_sub = (
            ts.query(TenantSubscription)
            .filter(
                TenantSubscription.tenant_id == tid,
                TenantSubscription.feature_key == "housewife_assistant",
                TenantSubscription.status == "active",
            )
            .first()
            is not None
        )
    # Репаир pending/no-sub — редкий legacy-случай (тенанты до approved_at-в-INSERT).
    if resolved.approved_at is None or not has_active_sub:
        with privileged_session("admin") as ps:
            _auto_approve_and_grant_free_tier(ps, tid)
    return workspace_id, assistant_id


def ensure_telegram_user_bundle(
    session: Session,
    payload: dict,
    *,
    bot_key: str = "sreda",
) -> TelegramOnboardingResult:
    """Ensure bundle for a Telegram update payload.

    #138 Ф5-5c: делегирует в ``_by_id`` (детекция существующего юзера идёт через
    identity-DEFINER там, а не прямым app-SELECT — иначе после флипа DSN
    существующий юзер не резолвится и уезжает в provision-путь).
    """
    chat_id = _extract_chat_id(payload)
    if chat_id is None:
        return TelegramOnboardingResult(False, None, None, None, None, None)

    display_name = _extract_display_name(payload) or f"Пользователь {chat_id}"
    return ensure_telegram_user_bundle_by_id(
        session, telegram_id=chat_id, display_name=display_name, bot_key=bot_key,
    )


def ensure_telegram_user_bundle_by_id(
    session: Session,
    *,
    telegram_id: str,
    display_name: str | None = None,
    bot_key: str = "sreda",
) -> TelegramOnboardingResult:
    """Ensure a tenant/user/assistant bundle exists for a Telegram account.

    Unlike ``ensure_telegram_user_bundle``, this variant does not require
    a webhook payload — used by Mini App auth to lazily provision a bundle
    when the user opens the WebApp before ever sending /start. The
    WebApp's initData hash is signed by Telegram, so the telegram_id is
    trustworthy at this point.

    Phase 6: ``bot_key`` resolves the per-bot ``signup_open`` policy from
    ``TelegramBotRegistry``. Unknown ``bot_key`` → fail-closed (KeyError
    from registry propagates). Existing tenants are always served
    regardless of ``bot_key`` (Model B shared tenant).

    Returns the same ``TelegramOnboardingResult`` shape. ``is_new_user``
    is True only when a new bundle was actually created.
    """
    if not telegram_id:
        return TelegramOnboardingResult(False, None, None, None, None, None)

    # #138 Ф5-5c: детекция существующего юзера — через identity-DEFINER (работает
    # после флипа DSN), НЕ прямым app-SELECT (find_user_by_chat_id вернул бы 0 строк
    # под RLS без ctx → существующий юзер уехал бы в provision-путь → ложный
    # rate-limit). tenant-чтения/репаир — в _serve_existing_bundle_reads под
    # правильными ролями. Phase 2: pending/no-sub тенанты чинятся там же.
    from sreda.services.identity_resolve import (
        AmbiguousExternalIdentity,
        resolve_external_identity,
    )
    try:
        resolved = resolve_external_identity("telegram", telegram_id)
    except AmbiguousExternalIdentity:
        resolved = None  # tg_account_hash unique → не должно быть; трактуем как unresolved
    if resolved is not None:
        workspace_id, assistant_id = _serve_existing_bundle_reads(
            resolved, tg_bot_key=bot_key,
        )
        return TelegramOnboardingResult(
            False, telegram_id, resolved.tenant_id, workspace_id,
            resolved.user_id, assistant_id,
        )

    # Phase 2C: signup abuse guard ПЕРЕД bundle creation. Raises
    # SignupBlocked если kill-switch / global cap / rate-limit hit.
    # Caller (handler) catches SignupBlocked → sends UPGRADE_COPY[reason].
    #
    # Phase 6: resolve per-bot signup_open from registry. Fail-closed on
    # unknown bot_key (KeyError from registry propagates to caller).
    # Existing tenants (found above) are NOT gated — only new signups.
    from sreda.config.bot_registry import TelegramBotRegistry
    from sreda.config.settings import get_settings as _get_settings
    _bot_signup_open: bool = True
    try:
        _registry = TelegramBotRegistry.from_settings(_get_settings())
        _bot_signup_open = _registry.resolve(bot_key).signup_open
    except KeyError:
        # Unknown bot_key → fail-closed: block the signup.
        logger.warning(
            "ensure_telegram_user_bundle_by_id: unknown bot_key=%r — "
            "failing closed (signup blocked)", bot_key,
        )
        raise SignupBlocked("signups_closed")

    # Гард строится на APP-сессии (читает runtime_config — identity не может).
    guard = SignupAbuseGuard.from_runtime_config(
        session, bot_signup_open=_bot_signup_open,
    )

    display_name = display_name or f"Пользователь {telegram_id}"
    tenant_id = f"tenant_tg_{telegram_id}"
    workspace_id = f"workspace_tg_{telegram_id}"
    user_id = f"user_tg_{telegram_id}"
    assistant_id = f"assistant_tg_{telegram_id}"

    # #138 Ф5-5b: identity-фаза (гард+бандл+грант) — ОДНА privileged-транзакция
    # под ролью sreda_identity. approved_at (авто-одобрение) + last_bot_key (#109)
    # в INSERT — прежние UPDATE tenants/users для нового юзера не нужны.
    # Инертно до флипа DSN (privileged_session фолбэкает на общий движок).
    from sreda.db.session import privileged_session

    with privileged_session("inbound-provision") as isess:
        created = provision_new_tenant_bundle(
            isess,
            guard=guard,
            channel="telegram",
            external_id=telegram_id,
            tenant_id=tenant_id,
            display_name=display_name,
            workspace_id=workspace_id,
            user_id=user_id,
            assistant_id=assistant_id,
            telegram_account_id=telegram_id,
            last_bot_key=bot_key,
        )

    # R2 (Codex high MAJOR-4): is_new_user = фактический created (гонку проиграл → не новый).
    return TelegramOnboardingResult(
        created, telegram_id, tenant_id, workspace_id, user_id, assistant_id
    )


_WELCOME_SENT_KEY = "welcome_sent_at"
_HOUSEWIFE_FEATURE_KEY = "housewife_assistant"


def is_welcome_sent(session: Session, tenant_id: str, user_id: str) -> bool:
    """True если post-approve welcome уже отправлен этому юзеру.

    Phase 2 fix 2026-05-08 (Codex MAJOR): hardened idempotency для
    welcome-flow. Раньше использовали `is_new_user` flag — но если
    HTTP-send падал, `is_new_user=False` на retry → welcome потерян.
    Теперь — флаг в БД (`tenant_user_skill_configs.skill_params_json
    -> welcome_sent_at`), который выставляется ТОЛЬКО на успешный send.
    """
    from sreda.db.repositories.user_profile import UserProfileRepository

    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, _HOUSEWIFE_FEATURE_KEY)
    if config is None:
        return False
    params = repo.decode_skill_params(config)
    return bool(params.get(_WELCOME_SENT_KEY))


def mark_welcome_sent(session: Session, tenant_id: str, user_id: str) -> None:
    """Записать факт успешной отправки welcome (idempotency anchor).

    Должна вызываться ПОСЛЕ успешного `client.send_message`. Если
    HTTP падает — флаг остаётся False, следующий inbound заретраит.
    Merge с существующими skill_params (онбординг tour state и др.),
    чтобы не сбросить их случайно.
    """
    from sreda.db.repositories.user_profile import UserProfileRepository

    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, _HOUSEWIFE_FEATURE_KEY)
    existing = repo.decode_skill_params(config) if config is not None else {}
    existing[_WELCOME_SENT_KEY] = datetime.now(timezone.utc).isoformat()
    repo.upsert_skill_config(
        tenant_id, user_id, _HOUSEWIFE_FEATURE_KEY,
        source="system",
        skill_params=existing,
    )
    session.commit()


# ---------------------------------------------------------------------------
# Post-tour name prompt idempotency (Codex r2 MINOR — 2026-05-09).
# Когда юзер на post-approve welcome тапнул «Расскажи поподробнее» и
# прошёл tour до done step'а, шлём отдельный name prompt. Юзер может
# back-navigate в done (prev → memory → next done) или re-trigger через
# повторный pb:done — в обоих случаях не нужно spam'ить prompt.
_POST_TOUR_NAME_PROMPT_SENT_KEY = "post_tour_name_prompt_sent_at"


def is_post_tour_name_prompt_sent(
    session: Session, tenant_id: str, user_id: str,
) -> bool:
    """True если post-tour name prompt уже отправлен этому юзеру."""
    from sreda.db.repositories.user_profile import UserProfileRepository

    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, _HOUSEWIFE_FEATURE_KEY)
    if config is None:
        return False
    params = repo.decode_skill_params(config)
    return bool(params.get(_POST_TOUR_NAME_PROMPT_SENT_KEY))


def mark_post_tour_name_prompt_sent(
    session: Session, tenant_id: str, user_id: str,
) -> None:
    """Записать факт успешной отправки post-tour name prompt'а."""
    from sreda.db.repositories.user_profile import UserProfileRepository

    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, _HOUSEWIFE_FEATURE_KEY)
    existing = repo.decode_skill_params(config) if config is not None else {}
    existing[_POST_TOUR_NAME_PROMPT_SENT_KEY] = (
        datetime.now(timezone.utc).isoformat()
    )
    repo.upsert_skill_config(
        tenant_id, user_id, _HOUSEWIFE_FEATURE_KEY,
        source="system",
        skill_params=existing,
    )
    session.commit()


def is_user_named(session: Session, tenant_id: str, user_id: str) -> bool:
    """True если у юзера уже выставлен display_name (он представился).

    Используется для skip post-tour name prompt'а: если юзер ответил
    именем ДО tour'а (Codex r3 MINOR scenario: «welcome → user answers
    name → later taps tour → done»), повторный prompt будет noise.

    ⚠ Codex r4 prod-breaking fix (2026-05-09): display_name живёт на
    `TenantUserProfile`, НЕ на `User`. r3 версия упала бы с
    AttributeError на `user.display_name`. Профиль может отсутствовать
    для нового юзера — read-only query (без get_or_create).
    """
    from sreda.db.models.user_profile import TenantUserProfile

    profile = session.query(TenantUserProfile).filter(
        TenantUserProfile.tenant_id == tenant_id,
        TenantUserProfile.user_id == user_id,
    ).first()
    if profile is None:
        return False
    return bool((profile.display_name or "").strip())


def build_post_approve_message() -> str:
    """Welcome после auto-grant'а на signup (Phase 2C).

    2026-05-22 (#60): первый onboarding step — выбор persona. После
    выбора юзер может открыть tour через `pb:voice`.
    """
    from sreda.services.housewife_persona import build_persona_choice_message

    return build_persona_choice_message()


def build_post_approve_keyboard_tg() -> dict:
    """Telegram inline_keyboard для post-approve welcome.

    Две кнопки выбора persona. После выбора callback-handler сохранит
    preset в `TenantUserSkillConfig.skill_params_json` и покажет кнопку
    `pb:voice` для tour.
    """
    from sreda.services.housewife_persona import build_persona_choice_keyboard_tg

    return build_persona_choice_keyboard_tg()


def build_post_approve_keyboard_max() -> list[dict]:
    """MAX inline_keyboard attachment для post-approve welcome.

    Тот же persona-choice контент что TG version, но в MAX-формате.
    """
    from sreda.services.housewife_persona import build_persona_choice_keyboard_max

    return build_persona_choice_keyboard_max()


# 2026-05-09 (Boris feedback): build_post_tour_name_prompt() удалён —
# name prompt теперь часть _DONE text inline (см. pending_bot._DONE).
# Helpers is_post_tour_name_prompt_sent / mark_post_tour_name_prompt_sent
# выше остаются для возможной будущей фичи conditional name prompt.


def _extract_chat_id(payload: dict) -> str | None:
    for key in ("message", "edited_message"):
        message = payload.get(key)
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if chat_id is not None:
            return str(chat_id)
    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        message = callback_query.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict):
                chat_id = chat.get("id")
                if chat_id is not None:
                    return str(chat_id)
    return None


def _extract_display_name(payload: dict) -> str | None:
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    for key in ("first_name", "username", "title"):
        value = chat.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ────────────────────────────────────────────────────────────────────
# MAX channel onboarding (Phase 4 of MAX integration sprint)
# ────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MaxOnboardingResult:
    """Mirror TelegramOnboardingResult для MAX channel."""

    is_new_user: bool
    max_account_id: str | None
    max_chat_id: str | None
    tenant_id: str | None
    workspace_id: str | None
    user_id: str | None
    assistant_id: str | None


def find_user_by_max_account_id(
    session: Session, max_account_id: str | int
) -> User | None:
    """Резолв юзера по MAX user_id через indexed колонку.

    В отличие от Telegram-lookup'а (через HMAC hash из 152-ФЗ Часть 1),
    MAX user_id — plain identifier в схеме 0035. Лукап по существующему
    индексу `ix_users_max_account_id`.
    """
    if max_account_id is None:
        return None
    aid = str(max_account_id).strip()
    if not aid:
        return None
    return (
        session.query(User)
        .filter(User.max_account_id == aid)
        .one_or_none()
    )


def ensure_max_user_bundle(
    session: Session,
    *,
    max_account_id: str | int,
    max_chat_id: str | int | None = None,
    display_name: str | None = None,
) -> MaxOnboardingResult:
    """Ensure tenant/user/workspace/assistant bundle for a MAX account.

    Phase 4 of MAX integration. Mirrors `ensure_telegram_user_bundle_by_id`
    но для MAX-specific identifiers.

    Race condition (R1#5): tenant row держится в одной транзакции
    через ``session.add → session.commit``; параллельные вызовы
    `ensure_max_user_bundle` для одного `max_account_id` корректно
    обработаются благодаря unique constraint на User PK + index lookup
    `find_user_by_max_account_id` ПЕРЕД создание нового tenant'а.

    Channel linking (R3): этот метод вызывается ТОЛЬКО когда юзер
    впервые пишет в MAX. Если юзер уже зарегистрирован через TG и хочет
    связать каналы — flow идёт через ``services.channel_linking``,
    `ensure_max_user_bundle` не вызывается.

    Returns ``MaxOnboardingResult`` с флагом ``is_new_user``.
    """
    aid = str(max_account_id).strip() if max_account_id is not None else ""
    if not aid:
        return MaxOnboardingResult(False, None, None, None, None, None, None)

    chat_id_str: str | None = None
    if max_chat_id is not None:
        chat_id_str = str(max_chat_id).strip() or None

    # #138 Ф5-5c: детекция существующего через identity-DEFINER (после флипа DSN
    # прямой app-SELECT вернул бы 0). channel linking уже мог связать max_account_id
    # к существующему tenant'у. Неоднозначность (max_account_id НЕ unique) → тихо не
    # резолвим (дроп/дубль решает вызывающий; max_inbound ловит Ambiguous отдельно).
    from sreda.services.identity_resolve import (
        AmbiguousExternalIdentity,
        resolve_external_identity,
    )
    try:
        resolved = resolve_external_identity("max", aid)
    except AmbiguousExternalIdentity:
        resolved = None
    if resolved is not None:
        workspace_id, assistant_id = _serve_existing_bundle_reads(
            resolved, update_max_chat_id=chat_id_str,
        )
        return MaxOnboardingResult(
            False, aid, chat_id_str, resolved.tenant_id, workspace_id,
            resolved.user_id, assistant_id,
        )

    # Гард на APP-сессии (читает runtime_config — identity не может).
    guard = SignupAbuseGuard.from_runtime_config(session)

    display_name = display_name or f"Пользователь {aid}"
    tenant_id = f"tenant_max_{aid}"
    workspace_id = f"workspace_max_{aid}"
    user_id = f"user_max_{aid}"
    assistant_id = f"assistant_max_{aid}"

    # #138 Ф5-5b: identity-фаза (гард+бандл+грант) — ОДНА privileged-транзакция.
    # max_chat_id/approved_at в INSERT (прежний post-commit patch убран). Инертно
    # до флипа DSN.
    from sreda.db.session import privileged_session

    with privileged_session("inbound-provision") as isess:
        created = provision_new_tenant_bundle(
            isess,
            guard=guard,
            channel="max",
            external_id=aid,
            tenant_id=tenant_id,
            display_name=display_name,
            workspace_id=workspace_id,
            user_id=user_id,
            assistant_id=assistant_id,
            max_account_id=aid,
            max_chat_id=chat_id_str or None,
        )

    # R2 (Codex high MAJOR-4): is_new_user = фактический created (гонку проиграл → не новый).
    return MaxOnboardingResult(
        created, aid, chat_id_str, tenant_id, workspace_id, user_id, assistant_id
    )
