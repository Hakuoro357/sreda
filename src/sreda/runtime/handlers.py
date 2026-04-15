"""Action handlers — pure functions dispatched by the assistant graph.

Each handler takes ``(session, action, context)`` and returns a list of
``RuntimeReply``. Handlers are free to raise ``ActionRuntimeError`` when
they hit a structured failure (e.g. the EDS connect link service refuses
to issue a session) — the graph's ``execute_action`` node catches these
and routes to ``persist_error``.

Previously these lived as ``_execute_*`` methods on ``ActionRuntimeService``
(~500 lines). Extracting them as module-level pure functions lets the
graph reference them through a static ``HANDLERS`` registry and makes
unit-testing trivial.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.repositories.user_profile import (
    NOTIFICATION_PRIORITIES,
    UserProfileRepository,
)
from sreda.features.app_registry import get_feature_registry
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.tools import build_memory_tools
from sreda.services.billing import (
    BillingService,
    CONNECT_BASE_CALLBACK,
    STATUS_CALLBACK,
    SUBSCRIPTIONS_CALLBACK,
)
from sreda.services.claim_lookup import ClaimLookupService
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService
from sreda.services.embeddings import get_embeddings_client
from sreda.services.llm import get_chat_llm

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeReply:
    text: str
    reply_markup: dict | None
    # Which skill produced this reply. ``None`` for platform-core
    # replies (help, status, subscriptions, profile, skills, claim).
    # Set by skill-provided handlers so the delivery worker can look up
    # per-skill ``notification_priority`` for quiet-hours / mute policy.
    feature_key: str | None = None


class ActionRuntimeError(Exception):
    """Structured failure from a handler or policy-guard.

    The error code is persisted as-is in ``agent_runs.error_code``; the
    message is sanitized by the privacy guard before going to the DB
    and to the user."""

    def __init__(self, code: str, message: str, *, reply_markup: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reply_markup = reply_markup


HandlerFn = Callable[[Session, ActionEnvelope, dict[str, Any]], list[RuntimeReply]]


# ---------------------------------------------------------------------------
# Individual handlers — one per action_type
# ---------------------------------------------------------------------------


def execute_help_show(session: Session, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
    text, reply_markup = BillingService(session).build_help_message()
    return [RuntimeReply(text=text, reply_markup=reply_markup)]


def execute_status_show(session: Session, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
    text, reply_markup = BillingService(session).build_status_message(action.tenant_id)
    return [RuntimeReply(text=text, reply_markup=reply_markup)]


def execute_subscriptions_show(session: Session, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
    text, reply_markup = BillingService(session).build_subscriptions_message(action.tenant_id)
    return [RuntimeReply(text=text, reply_markup=reply_markup)]


def execute_claim_lookup(session: Session, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
    claim_id = str(action.params.get("claim_id") or "").strip()
    service = ClaimLookupService(session)
    result = service.lookup_local_claim(action.tenant_id, claim_id)
    if result is None:
        return [
            RuntimeReply(
                text=(
                    f"Заявка #{claim_id} пока не найдена в локальном состоянии Среды.\n\n"
                    "Если она появилась недавно, попробуй еще раз позже."
                ),
                reply_markup=_status_subscriptions_markup(),
            )
        ]
    return [
        RuntimeReply(
            text=service.build_claim_reply(result),
            reply_markup=_status_subscriptions_markup(),
        )
    ]


def execute_subscription_connect_base(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    result = BillingService(session).start_base_subscription(action.tenant_id)
    return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]


def execute_subscription_add_eds(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    result = BillingService(session).add_extra_eds_account(action.tenant_id)
    replies = [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]
    replies.extend(_build_connect_replies(session, action, slot_type="extra"))
    return replies


def execute_subscription_renew_cycle(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    result = BillingService(session).renew_cycle(action.tenant_id)
    return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]


def execute_eds_connect_start(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    slot_type = str(action.params.get("slot_type") or "available_slot")
    resolved_slot_type = _resolve_slot_type(session, action.tenant_id, slot_type)
    return _build_connect_replies(session, action, slot_type=resolved_slot_type)


def execute_eds_connect_retry(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    slot_type = str(action.params.get("slot_type") or "")
    return _build_connect_replies(session, action, slot_type=slot_type)


def execute_eds_slot_remove_free(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    result = BillingService(session).remove_extra_account_at_period_end(action.tenant_id)
    return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]


def execute_eds_slot_restore_free(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    result = BillingService(session).restore_extra_account_slot(action.tenant_id)
    return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]


def execute_eds_account_remove(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    tenant_eds_account_id = str(action.params.get("tenant_eds_account_id") or "").strip()
    if not tenant_eds_account_id:
        raise ActionRuntimeError(
            "tenant_eds_account_missing",
            "Не удалось определить кабинет для отключения.",
            reply_markup=_subscriptions_markup(),
        )
    result = BillingService(session).schedule_connected_eds_account_cancel(
        action.tenant_id, tenant_eds_account_id
    )
    return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]


def execute_eds_account_restore(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    tenant_eds_account_id = str(action.params.get("tenant_eds_account_id") or "").strip()
    if not tenant_eds_account_id:
        raise ActionRuntimeError(
            "tenant_eds_account_missing",
            "Не удалось определить кабинет для возврата.",
            reply_markup=_subscriptions_markup(),
        )
    result = BillingService(session).restore_connected_eds_account_cancel(
        action.tenant_id, tenant_eds_account_id
    )
    return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]


# ---------------------------------------------------------------------------
# Profile / skill-config handlers (Phase 2)
# ---------------------------------------------------------------------------


_QUIET_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _require_user_id(action: ActionEnvelope) -> str:
    if not action.user_id:
        raise ActionRuntimeError(
            "runtime_user_missing",
            "Не удалось определить пользователя для этой команды.",
        )
    return action.user_id


def _parse_quiet_arg(raw: str) -> list[dict[str, Any]] | None:
    """Parse ``/quiet`` argument into a list of quiet-hour windows.

    Returns ``None`` for syntactically invalid input so the handler can
    reply with a help message."""
    arg = raw.strip().lower()
    if arg in {"off", "clear", "-"}:
        return []
    match = _QUIET_RE.match(arg)
    if not match:
        return None
    from_hour, to_hour = int(match.group(1)), int(match.group(2))
    if not (0 <= from_hour <= 23 and 0 <= to_hour <= 23):
        return None
    return [
        {"from_hour": from_hour, "to_hour": to_hour, "weekdays": [0, 1, 2, 3, 4, 5, 6]}
    ]


def _format_quiet_hours(windows: list[dict[str, Any]]) -> str:
    if not windows:
        return "не настроены"
    parts = []
    for w in windows:
        fh = int(w.get("from_hour", 0))
        th = int(w.get("to_hour", 0))
        weekdays = w.get("weekdays") or list(range(7))
        wd_part = "ежедневно" if sorted(weekdays) == list(range(7)) else _format_weekdays(weekdays)
        parts.append(f"{fh:02d}:00–{th:02d}:00 ({wd_part})")
    return "; ".join(parts)


_WEEKDAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _format_weekdays(weekdays: list[int]) -> str:
    return ", ".join(_WEEKDAY_NAMES[d] for d in weekdays if 0 <= d <= 6)


def execute_profile_show(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    repo = UserProfileRepository(session)
    profile = repo.get_or_create_profile(action.tenant_id, user_id)
    session.commit()
    quiet_text = _format_quiet_hours(UserProfileRepository.decode_quiet_hours(profile))
    tags = UserProfileRepository.decode_interest_tags(profile)
    tags_text = ", ".join(tags) if tags else "не заданы"

    # Render per-skill configs (from what user has set + what registry exposes).
    registry = get_feature_registry()
    manifests = {m.feature_key: m for m in registry.iter_manifests()}
    configs_by_key = {
        c.feature_key: c for c in repo.list_skill_configs(action.tenant_id, user_id)
    }
    all_keys = sorted(set(manifests.keys()) | set(configs_by_key.keys()))

    lines = [
        "🏷 Профиль",
        f"• Имя: {profile.display_name or '—'}",
        f"• Часовой пояс: {profile.timezone}",
        f"• Стиль общения: {profile.communication_style}",
        f"• Тихие часы: {quiet_text}",
        f"• Интересы: {tags_text}",
    ]
    if all_keys:
        lines.append("")
        lines.append("🔌 Скилы")
        for key in all_keys:
            manifest = manifests.get(key)
            title = manifest.title if manifest else key
            config = configs_by_key.get(key)
            priority = config.notification_priority if config else "normal"
            lines.append(f"• {title} ({key}) — приоритет: {priority}")
    text = "\n".join(lines)
    return [RuntimeReply(text=text, reply_markup=None)]


def execute_profile_set_quiet_hours(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    raw = str(action.params.get("args_raw") or "").strip()
    windows = _parse_quiet_arg(raw)
    if windows is None:
        raise ActionRuntimeError(
            "quiet_hours_invalid",
            "Не понял формат. Используй: /quiet 22-8 или /quiet off",
        )
    repo = UserProfileRepository(session)
    repo.update_profile(
        action.tenant_id,
        user_id,
        source="user_command",
        actor_user_id=user_id,
        quiet_hours=windows,
    )
    session.commit()
    if not windows:
        text = "✅ Тихие часы сняты — сообщения будут приходить без задержки."
    else:
        text = "✅ Тихие часы: " + _format_quiet_hours(windows)
    return [RuntimeReply(text=text, reply_markup=None)]


def execute_skills_list(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    repo = UserProfileRepository(session)
    registry = get_feature_registry()
    manifests = sorted(registry.iter_manifests(), key=lambda m: m.feature_key)
    configs_by_key = {
        c.feature_key: c for c in repo.list_skill_configs(action.tenant_id, user_id)
    }

    if not manifests:
        return [RuntimeReply(text="Скилы пока не зарегистрированы.", reply_markup=None)]

    lines = ["🔌 Скилы (/skill <key> — подробнее):"]
    for manifest in manifests:
        config = configs_by_key.get(manifest.feature_key)
        priority = config.notification_priority if config else "normal"
        lines.append(f"• {manifest.title} ({manifest.feature_key}) — приоритет: {priority}")
    return [RuntimeReply(text="\n".join(lines), reply_markup=None)]


def execute_skill_show(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    feature_key = str(action.params.get("feature_key") or "").strip().lower()
    if not feature_key:
        raise ActionRuntimeError(
            "skill_key_missing",
            "Используй: /skill <key> или /skill <key> priority <urgent|normal|low|mute>",
        )
    registry = get_feature_registry()
    manifest = registry.get_manifest(feature_key)
    if manifest is None:
        raise ActionRuntimeError(
            "skill_unknown",
            f"Скил {feature_key!r} не найден. /skills — список доступных.",
        )
    repo = UserProfileRepository(session)
    config = repo.get_skill_config(action.tenant_id, user_id, feature_key)
    priority = config.notification_priority if config else "normal"
    token_budget = (
        f"{config.token_budget_daily}" if config and config.token_budget_daily > 0 else "не ограничен"
    )

    lines = [
        f"🔌 {manifest.title} ({feature_key})",
        f"• Описание: {manifest.description}",
        f"• Приоритет уведомлений: {priority}",
        f"• Дневной лимит токенов: {token_budget}",
        "",
        f"Изменить: /skill {feature_key} priority <urgent|normal|low|mute>",
    ]
    return [RuntimeReply(text="\n".join(lines), reply_markup=None)]


def _validate_proposed_field(field_name: str, proposed_value: Any) -> tuple[str, Any] | None:
    """Validate an agent-proposed profile field update.

    Returns ``(normalized_field, normalized_value)`` on success, or
    ``None`` if the field/value is invalid. Keeps a single place where
    we enumerate which profile fields can be changed via the hybrid-UX
    path (agent proposes → user confirms)."""
    if field_name == "timezone":
        if not isinstance(proposed_value, str):
            return None
        try:
            ZoneInfo(proposed_value)
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return field_name, proposed_value
    if field_name == "communication_style":
        if proposed_value not in {"terse", "casual", "formal"}:
            return None
        return field_name, proposed_value
    if field_name == "display_name":
        if not isinstance(proposed_value, str) or not 1 <= len(proposed_value) <= 128:
            return None
        return field_name, proposed_value
    # Quiet hours / skill configs not supported via proposal path (too
    # structured; users use direct commands). Agents that want those
    # changes should prompt the user via chat instead of confirm-button.
    return None


def _confirm_keyboard(proposal_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Подтвердить", "callback_data": f"profile:confirm:{proposal_id}"},
                {"text": "Отменить", "callback_data": f"profile:reject:{proposal_id}"},
            ]
        ]
    }


def execute_profile_propose_update(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    """Create a pending ``TenantUserProfileProposal`` and return a
    Telegram message with Подтвердить/Отменить buttons.

    This handler is the "agent tool" entry point — in Phase 4+ the LLM
    will call it through a structured tool; for now tests invoke it
    directly to exercise the confirm flow."""
    user_id = _require_user_id(action)
    field_name = str(action.params.get("field_name") or "").strip()
    proposed_value = action.params.get("proposed_value")
    justification = action.params.get("justification")

    normalized = _validate_proposed_field(field_name, proposed_value)
    if normalized is None:
        raise ActionRuntimeError(
            "profile_proposal_invalid",
            "Не удалось сохранить предложение: поле или значение некорректны.",
        )
    field_name, proposed_value = normalized

    repo = UserProfileRepository(session)
    proposal = repo.create_proposal(
        action.tenant_id,
        user_id,
        field_name=field_name,
        proposed_value=proposed_value,
        justification=str(justification) if justification else None,
    )
    session.commit()

    lines = [
        "🤖 Предлагаю обновить профиль:",
        f"• {field_name} → {proposed_value}",
    ]
    if justification:
        lines.append(f"\n{justification}")
    return [
        RuntimeReply(
            text="\n".join(lines),
            reply_markup=_confirm_keyboard(proposal.id),
        )
    ]


def execute_profile_confirm_update(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    from datetime import datetime, timezone

    user_id = _require_user_id(action)
    proposal_id = str(action.params.get("proposal_id") or "").strip()
    if not proposal_id:
        raise ActionRuntimeError(
            "proposal_id_missing",
            "Не могу обработать подтверждение — не указан идентификатор предложения.",
        )
    repo = UserProfileRepository(session)
    proposal = repo.get_proposal(proposal_id)
    if proposal is None:
        raise ActionRuntimeError(
            "proposal_not_found",
            "Это предложение уже недоступно.",
        )
    if proposal.tenant_id != action.tenant_id or proposal.user_id != user_id:
        raise ActionRuntimeError(
            "proposal_access_denied",
            "Это предложение не твоё.",
        )
    if proposal.status != "pending":
        raise ActionRuntimeError(
            "proposal_already_resolved",
            f"Это предложение уже обработано ({proposal.status}).",
        )
    if UserProfileRepository.is_proposal_expired(proposal, datetime.now(timezone.utc)):
        repo.mark_proposal_status(proposal.id, status="expired")
        session.commit()
        raise ActionRuntimeError(
            "proposal_expired",
            "Срок действия предложения истёк.",
        )

    field_name = proposal.field_name
    value = UserProfileRepository.decode_proposed_value(proposal)

    update_kwargs: dict[str, Any] = {
        "source": "agent_tool_confirmed",
        "actor_user_id": user_id,
    }
    if field_name == "timezone":
        update_kwargs["tz"] = value
    elif field_name == "communication_style":
        update_kwargs["communication_style"] = value
    elif field_name == "display_name":
        update_kwargs["display_name"] = value
    else:
        raise ActionRuntimeError(
            "proposal_field_unsupported",
            f"Поле {field_name!r} больше не поддерживается.",
        )

    repo.update_profile(action.tenant_id, user_id, **update_kwargs)
    repo.mark_proposal_status(proposal.id, status="confirmed")
    session.commit()
    return [
        RuntimeReply(
            text=f"✅ Профиль обновлён: {field_name} = {value}",
            reply_markup=None,
        )
    ]


def execute_profile_reject_update(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    proposal_id = str(action.params.get("proposal_id") or "").strip()
    if not proposal_id:
        raise ActionRuntimeError(
            "proposal_id_missing",
            "Не могу обработать отмену — не указан идентификатор предложения.",
        )
    repo = UserProfileRepository(session)
    proposal = repo.get_proposal(proposal_id)
    if proposal is None:
        raise ActionRuntimeError(
            "proposal_not_found",
            "Это предложение уже недоступно.",
        )
    if proposal.tenant_id != action.tenant_id or proposal.user_id != user_id:
        raise ActionRuntimeError(
            "proposal_access_denied",
            "Это предложение не твоё.",
        )
    if proposal.status != "pending":
        return [
            RuntimeReply(
                text=f"Это предложение уже обработано ({proposal.status}).",
                reply_markup=None,
            )
        ]
    repo.mark_proposal_status(proposal.id, status="rejected")
    session.commit()
    return [
        RuntimeReply(
            text="✖ Предложение отменено — профиль не изменён.",
            reply_markup=None,
        )
    ]


def execute_profile_set_timezone(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    raw = str(action.params.get("timezone") or "").strip()
    if not raw:
        raise ActionRuntimeError(
            "timezone_missing",
            "Используй: /tz <IANA zone>, например /tz Europe/Moscow",
        )
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ActionRuntimeError(
            "timezone_invalid",
            f"Не знаю такой часовой пояс: {raw!r}. Примеры: UTC, Europe/Moscow, Asia/Vladivostok.",
        ) from exc

    repo = UserProfileRepository(session)
    repo.update_profile(
        action.tenant_id,
        user_id,
        source="user_command",
        actor_user_id=user_id,
        tz=raw,
    )
    session.commit()
    return [RuntimeReply(text=f"✅ Часовой пояс: {raw}", reply_markup=None)]


def execute_skill_set_priority(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    user_id = _require_user_id(action)
    feature_key = str(action.params.get("feature_key") or "").strip().lower()
    priority = str(action.params.get("priority") or "").strip().lower()
    if not feature_key:
        raise ActionRuntimeError(
            "skill_key_missing",
            "Используй: /skill <key> priority <urgent|normal|low|mute>",
        )
    if priority not in NOTIFICATION_PRIORITIES:
        raise ActionRuntimeError(
            "skill_priority_invalid",
            "Приоритет должен быть одним из: urgent, normal, low, mute.",
        )
    registry = get_feature_registry()
    if registry.get_manifest(feature_key) is None:
        raise ActionRuntimeError(
            "skill_unknown",
            f"Скил {feature_key!r} не найден. /skills — список доступных.",
        )
    repo = UserProfileRepository(session)
    repo.upsert_skill_config(
        action.tenant_id,
        user_id,
        feature_key,
        source="user_command",
        actor_user_id=user_id,
        notification_priority=priority,
    )
    session.commit()
    text = f"✅ Приоритет {feature_key}: {priority}"
    return [RuntimeReply(text=text, reply_markup=None)]


# ---------------------------------------------------------------------------
# Conversation (LLM-driven) handler (Phase 3)
# ---------------------------------------------------------------------------


_CONVERSATION_SYSTEM_PROMPT = """\
Ты — Среда, персональный AI-ассистент пользователя в Telegram. Говоришь на русском, если пользователь не переходит на другой язык.

Поведение:
- Отвечай кратко и по делу, без воды.
- Если пользователь делится стабильным фактом о себе (семья, работа, место жительства, долгосрочные предпочтения) — зови инструмент ``save_core_fact``, записывай факт одним предложением в словах пользователя.
- Если пользователь делится событием или настроением ("сегодня устал", "вчера ругался с коллегой") — зови ``save_episode``, короткое summary.
- Если нужна дополнительная память — зови ``recall_memory`` с поисковым запросом.
- НЕ сохраняй моментальные запросы ("помоги с X"), мнения, которые могут меняться, или сомнения.
- Используй уже известные факты ниже, чтобы отвечать без переспрашивания.
"""


def _format_profile_for_prompt(profile: dict[str, Any]) -> str:
    if not profile:
        return "Профиль ещё не заполнен."
    parts = []
    if profile.get("display_name"):
        parts.append(f"Имя: {profile['display_name']}")
    if profile.get("timezone") and profile["timezone"] != "UTC":
        parts.append(f"Часовой пояс: {profile['timezone']}")
    if profile.get("communication_style"):
        parts.append(f"Стиль общения: {profile['communication_style']}")
    tags = profile.get("interest_tags") or []
    if tags:
        parts.append(f"Интересы: {', '.join(tags)}")
    return "\n".join(parts) if parts else "Профиль заполнен минимально."


def _format_memories_for_prompt(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "Пока ничего не помню о пользователе."
    lines = []
    for mem in memories:
        tier = mem.get("tier", "?")
        content = mem.get("content", "")
        lines.append(f"- [{tier}] {content}")
    return "\n".join(lines)


def execute_conversation_chat(
    session: Session, action: ActionEnvelope, context: dict[str, Any]
) -> list[RuntimeReply]:
    """LLM-driven conversational handler with memory tool-loop.

    Dispatched from free-form user text (anything not matching a
    slash-command). Builds a system prompt with user's profile +
    relevant memories, binds the memory tools, then loops on LLM
    tool calls until the model returns a plain assistant message.
    """
    from langchain_core.messages import (  # local import — LLM path only
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    user_id = _require_user_id(action)
    user_text = str(action.params.get("text") or "").strip()
    if not user_text:
        raise ActionRuntimeError(
            "conversation_text_missing",
            "Пустое сообщение — нечего обрабатывать.",
        )

    llm = context.get("_llm_client") or get_chat_llm()
    if llm is None:
        return [
            RuntimeReply(
                text=(
                    "LLM пока не подключён (нет SREDA_MIMO_API_KEY). "
                    "Используй команды /help, /profile, /skills."
                ),
                reply_markup=None,
            )
        ]

    embedding_client = context.get("_embedding_client") or get_embeddings_client(
        allow_fake=True
    )
    profile = context.get("_profile") or {}
    memories = context.get("_memories") or []

    system_text = (
        _CONVERSATION_SYSTEM_PROMPT
        + "\n\n[ПРОФИЛЬ]\n"
        + _format_profile_for_prompt(profile)
        + "\n\n[ПАМЯТЬ — релевантные факты]\n"
        + _format_memories_for_prompt(memories)
    )

    tools = build_memory_tools(
        session=session,
        tenant_id=action.tenant_id,
        user_id=user_id,
        embedding_client=embedding_client,
    )
    tools_by_name = {t.name: t for t in tools}

    llm_with_tools = llm.bind_tools(tools)
    messages: list[Any] = [
        SystemMessage(content=system_text),
        HumanMessage(content=user_text),
    ]

    # Tool-call loop. Cap at 5 iterations so a runaway LLM can't pin us.
    final_ai: AIMessage | None = None
    for _iter in range(5):
        ai_msg: AIMessage = llm_with_tools.invoke(messages)
        messages.append(ai_msg)
        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if not tool_calls:
            final_ai = ai_msg
            break
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("args") or {}
            tc_id = tc.get("id", "")
            tool = tools_by_name.get(name)
            if tool is None:
                messages.append(
                    ToolMessage(content=f"error:unknown_tool:{name}", tool_call_id=tc_id)
                )
                continue
            try:
                result = tool.invoke(args)
            except Exception as exc:  # noqa: BLE001
                logger.exception("tool %s failed", name)
                result = f"error:{type(exc).__name__}"
            messages.append(ToolMessage(content=str(result), tool_call_id=tc_id))
    else:
        # Loop exhausted without a final plain message — synthesize one.
        final_ai = AIMessage(
            content="Не смог сформировать ответ — слишком много шагов с инструментами."
        )

    text = (final_ai.content or "").strip() or "..."
    return [RuntimeReply(text=text, reply_markup=None)]


# ---------------------------------------------------------------------------
# Registry — used by the graph's ``execute_action`` node and as the single
# source of truth for "which action_types are supported".
# ---------------------------------------------------------------------------

HANDLERS: dict[str, HandlerFn] = {
    "help.show": execute_help_show,
    "status.show": execute_status_show,
    "subscriptions.show": execute_subscriptions_show,
    "claim.lookup": execute_claim_lookup,
    "subscription.connect_base": execute_subscription_connect_base,
    "subscription.add_eds": execute_subscription_add_eds,
    "subscription.renew_cycle": execute_subscription_renew_cycle,
    "eds.connect.start": execute_eds_connect_start,
    "eds.connect.retry": execute_eds_connect_retry,
    "eds.slot.remove_free": execute_eds_slot_remove_free,
    "eds.slot.restore_free": execute_eds_slot_restore_free,
    "eds.account.remove": execute_eds_account_remove,
    "eds.account.restore": execute_eds_account_restore,
    "profile.show": execute_profile_show,
    "profile.set_quiet_hours": execute_profile_set_quiet_hours,
    "profile.set_timezone": execute_profile_set_timezone,
    "profile.propose_update": execute_profile_propose_update,
    "profile.confirm_update": execute_profile_confirm_update,
    "profile.reject_update": execute_profile_reject_update,
    "conversation.chat": execute_conversation_chat,
    "skills.list": execute_skills_list,
    "skill.show": execute_skill_show,
    "skill.set_priority": execute_skill_set_priority,
}


# ---------------------------------------------------------------------------
# Shared helpers (used by multiple handlers)
# ---------------------------------------------------------------------------


def _build_connect_replies(
    session: Session, action: ActionEnvelope, *, slot_type: str
) -> list[RuntimeReply]:
    connect_service = EDSConnectService(session, get_settings())
    try:
        link = connect_service.create_connect_link(
            tenant_id=action.tenant_id,
            workspace_id=action.workspace_id,
            user_id=action.user_id,
            slot_type=slot_type,
        )
    except ConnectSessionError as exc:
        raise ActionRuntimeError(
            exc.code, exc.message, reply_markup=_subscriptions_markup()
        ) from exc

    return [
        RuntimeReply(
            text=(
                "Сейчас откроется защищенная одноразовая страница для подключения личного кабинета EDS.\n\n"
                "Логин и пароль передаются по защищенному соединению и сохраняются в системе только в зашифрованном виде.\n\n"
                "Чтобы ввести данные для подключения, нажмите кнопку ниже."
            ),
            reply_markup={
                "inline_keyboard": [
                    [_build_connect_open_button(link.url)],
                    [{"text": "Отменить", "callback_data": STATUS_CALLBACK}],
                ]
            },
        )
    ]


def _resolve_slot_type(session: Session, tenant_id: str, slot_type: str) -> str:
    if slot_type in {"primary", "extra"}:
        return slot_type
    summary = BillingService(session).get_summary(tenant_id)
    return "primary" if not summary.connected_accounts else "extra"


def _subscriptions_markup() -> dict:
    return {"inline_keyboard": [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]}


def _status_subscriptions_markup() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
            [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
        ]
    }


def connect_reply_markup(base_active: bool) -> dict:
    """Exported for ``policy.py`` — markup for the "connect base" CTA."""
    if base_active:
        return _status_subscriptions_markup()
    return {
        "inline_keyboard": [
            [{"text": "Подключить EDS Monitor", "callback_data": CONNECT_BASE_CALLBACK}],
            [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
        ]
    }


def subscriptions_markup() -> dict:
    """Exported for ``policy.py``."""
    return _subscriptions_markup()


def status_subscriptions_markup() -> dict:
    """Exported for ``policy.py``."""
    return _status_subscriptions_markup()


def _build_connect_open_button(url: str) -> dict:
    if url.startswith("https://"):
        return {"text": "Ввести логин и пароль от EDS", "web_app": {"url": url}}
    return {"text": "Ввести логин и пароль от EDS", "url": url}
