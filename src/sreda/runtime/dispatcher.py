from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass

from sreda.services.billing import (
    ADD_EDS_ACCOUNT_CALLBACK,
    CANCEL_VOICE_CALLBACK,
    CONNECT_BASE_CALLBACK,
    CONNECT_VOICE_CALLBACK,
    REMOVE_EDS_ACCOUNT_CALLBACK,
    REMOVE_EDS_ACCOUNT_SELECT_PREFIX,
    RENEW_CALLBACK,
    RESTORE_EDS_ACCOUNT_CALLBACK,
    RESTORE_EDS_ACCOUNT_SELECT_PREFIX,
    STATUS_CALLBACK,
    SUBSCRIPTIONS_CALLBACK,
)
from sreda.services.eds_account_verification import (
    RETRY_CONNECT_EXTRA_CALLBACK,
    RETRY_CONNECT_PRIMARY_CALLBACK,
)
from sreda.services.onboarding import (
    CONNECT_EDS_CALLBACK,
    MaxOnboardingResult,
    TelegramOnboardingResult,
)


logger = logging.getLogger(__name__)


def to_outbox_channel(action_channel_type: str) -> str:
    """Map ``ActionEnvelope.channel_type`` → ``OutboxMessage.channel_type``.

    Action envelopes carry surface-specific suffixes (``telegram_dm`` /
    ``max_dm``) so AgentThread может различать DM vs group thread на
    одном канале. OutboxMessage хранит **bare** channel-id (``telegram`` /
    ``max``) — это то на что route'ит ``OutboxDeliveryWorker._send_now*``.

    Идемпотентен — если что-то уже bare (``"telegram"``, ``"max"``),
    возвращаем как есть.

    Unknown channel types — log warning + passthrough. Не raise чтобы
    не убить весь turn если кто-то завёл новый канал и забыл здесь
    добавить prefix; warning видно в логах + monitor подхватит row
    с unknown channel_type который worker не сможет route'ить
    (status='pending' навсегда → unprocessed_outbox alert).
    """
    if action_channel_type.startswith("telegram"):
        return "telegram"
    if action_channel_type.startswith("max"):
        return "max"
    logger.warning(
        "to_outbox_channel: unknown channel %r — passthrough; "
        "outbox worker может не знать как route'ить эту row",
        action_channel_type,
    )
    return action_channel_type


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    action_type: str
    tenant_id: str
    workspace_id: str
    assistant_id: str | None
    user_id: str | None
    channel_type: str
    external_chat_id: str
    bot_key: str
    inbound_message_id: str | None
    source_type: str
    source_value: str | None
    params: dict

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def outbox_channel(self) -> str:
        """``OutboxMessage.channel_type`` — derived from ``channel_type``."""
        return to_outbox_channel(self.channel_type)


def dispatch_telegram_action(
    *,
    payload: dict,
    bot_key: str,
    onboarding: TelegramOnboardingResult,
    inbound_message_id: str | None,
) -> ActionEnvelope | None:
    if not onboarding.chat_id or not onboarding.tenant_id or not onboarding.workspace_id:
        return None

    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        callback_data = callback_query.get("data")
        if not isinstance(callback_data, str):
            return None
        resolved = _resolve_callback_action(callback_data)
        if resolved is None:
            return None
        action_type, params = resolved
        return ActionEnvelope(
            action_type=action_type,
            tenant_id=onboarding.tenant_id,
            workspace_id=onboarding.workspace_id,
            assistant_id=onboarding.assistant_id,
            user_id=onboarding.user_id,
            channel_type="telegram_dm",
            external_chat_id=onboarding.chat_id,
            bot_key=bot_key,
            inbound_message_id=inbound_message_id,
            source_type="telegram_callback",
            source_value=callback_data,
            params=params,
        )

    message_text = _extract_message_text(payload)
    if not message_text:
        return None
    resolved_command = _resolve_command_action(message_text.strip())
    if resolved_command is None:
        return None
    action_type, params = resolved_command
    return ActionEnvelope(
        action_type=action_type,
        tenant_id=onboarding.tenant_id,
        workspace_id=onboarding.workspace_id,
        assistant_id=onboarding.assistant_id,
        user_id=onboarding.user_id,
        channel_type="telegram_dm",
        external_chat_id=onboarding.chat_id,
        bot_key=bot_key,
        inbound_message_id=inbound_message_id,
        source_type="telegram_message",
        source_value=message_text.strip(),
        params=params,
    )


def dispatch_max_action(
    *,
    payload: dict,
    bot_key: str,
    onboarding: MaxOnboardingResult,
    inbound_message_id: str | None,
) -> ActionEnvelope | None:
    """Build ActionEnvelope for an incoming MAX update.

    Mirror semantics ``dispatch_telegram_action``:
    - free-form text → ``conversation.chat`` (LLM)
    - commands ``/help``, ``/status``, ``подписки`` etc. → resolved
      to typed action via ``_resolve_command_action``
    - callback_query for MAX (если в будущем будут inline-buttons) — TBD,
      сейчас возвращаем None

    Differences from TG dispatch:
    - text extracted из ``message.body.text`` (см. probe Phase 0); TG
      кладёт в ``message.text`` напрямую.
    - ``channel_type='max_dm'`` → AgentThread separate from TG-thread даже
      для same tenant (connected accounts видят одинаковый context но
      разные threads — это OK, история объединяется через recall_memory).
    - ``external_chat_id`` = ``onboarding.max_chat_id`` (string,
      max_chat_id в БД хранится как строка).
    """
    if not (
        onboarding.max_chat_id
        and onboarding.tenant_id
        and onboarding.workspace_id
    ):
        return None

    update_type = payload.get("update_type")

    # message_callback — юзер тапнул inline-button. Mirror TG callback path:
    # извлекаем ``payload.callback.payload`` (это TG-эквивалент
    # ``callback_query.data``), резолвим через _resolve_callback_action.
    # Inline-handlers (rem_done/rem_snooze/pb/btn_reply) рутятся ВНЕ
    # dispatcher'а — см. ``services.max_inbound._handle_max_callback``
    # (это symmetric с TG ``services.telegram_bot._handle_callback``).
    if update_type == "message_callback":
        callback = payload.get("callback")
        if not isinstance(callback, dict):
            return None
        callback_data = callback.get("payload")
        if not isinstance(callback_data, str):
            return None
        resolved = _resolve_callback_action(callback_data)
        if resolved is None:
            return None
        action_type, params = resolved
        return ActionEnvelope(
            action_type=action_type,
            tenant_id=onboarding.tenant_id,
            workspace_id=onboarding.workspace_id,
            assistant_id=onboarding.assistant_id,
            user_id=onboarding.user_id,
            channel_type="max_dm",
            external_chat_id=str(onboarding.max_chat_id),
            bot_key=bot_key,
            inbound_message_id=inbound_message_id,
            source_type="max_callback",
            source_value=callback_data,
            params=params,
        )

    if update_type not in ("message_created", "bot_started"):
        # Прочее (bot_added/removed и т.д.) пропускаем.
        return None

    message_text = _extract_max_message_text(payload)
    if not message_text:
        # bot_started без текста ИЛИ unsupported message-type (image/voice/file).
        # Текущее поведение — silent skip → status='ignored' в caller'е.
        # TODO (R1 review #5): добавить TODO-action для bot_started
        # (canned welcome) — separate task. (R1 review #6): для
        # voice/image отправлять «I can only process text for now» вместо
        # silent drop — separate task.
        logger.info(
            "max dispatch: no extractable text (update_type=%s) — ignored",
            update_type,
        )
        return None

    # _resolve_command_action ВКЛЮЧАЕТ free-form-text fallback на
    # ``conversation.chat`` (см. dispatcher.py — последняя строка функции:
    # `return "conversation.chat", {"text": message_text.strip()}`).
    # Возвращает None ТОЛЬКО когда text пустой, что мы уже отфильтровали.
    # Именование `_resolve_command_action` исторически misleading; не
    # переименовываем сейчас чтобы не задеть TG path.
    resolved_command = _resolve_command_action(message_text.strip())
    if resolved_command is None:
        # Defensive — fallthrough если когда-то семантика поменяется.
        return None
    action_type, params = resolved_command
    return ActionEnvelope(
        action_type=action_type,
        tenant_id=onboarding.tenant_id,
        workspace_id=onboarding.workspace_id,
        assistant_id=onboarding.assistant_id,
        user_id=onboarding.user_id,
        channel_type="max_dm",
        external_chat_id=str(onboarding.max_chat_id),
        bot_key=bot_key,
        inbound_message_id=inbound_message_id,
        source_type="max_message",
        source_value=message_text.strip(),
        params=params,
    )


def _extract_max_message_text(payload: dict) -> str | None:
    """Extract text из MAX message_created payload.

    Probe Phase 0 закрепил: text в ``message.body.text``. Возвращаем
    stripped не-пустой текст или None. Идемпотентно к None/missing keys.
    """
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    body = message.get("body")
    if not isinstance(body, dict):
        return None
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _resolve_callback_action(callback_data: str) -> tuple[str, dict] | None:
    if callback_data.startswith(REMOVE_EDS_ACCOUNT_SELECT_PREFIX):
        account_id = callback_data.removeprefix(REMOVE_EDS_ACCOUNT_SELECT_PREFIX)
        if not _is_valid_entity_id(account_id):
            return None
        return "eds.account.remove", {"tenant_eds_account_id": account_id}
    if callback_data.startswith(RESTORE_EDS_ACCOUNT_SELECT_PREFIX):
        account_id = callback_data.removeprefix(RESTORE_EDS_ACCOUNT_SELECT_PREFIX)
        if not _is_valid_entity_id(account_id):
            return None
        return "eds.account.restore", {"tenant_eds_account_id": account_id}
    # Billing: buy-extra CTA from quota-exhausted replies (Phase 4.5).
    if callback_data.startswith("billing:buy_extra:"):
        feature_key = callback_data.removeprefix("billing:buy_extra:")
        if not _is_valid_entity_id(feature_key):
            return None
        return "billing.buy_extra", {"feature_key": feature_key}

    # Profile proposal confirm / reject (Phase 2e hybrid-UX).
    if callback_data.startswith("profile:confirm:"):
        proposal_id = callback_data.removeprefix("profile:confirm:")
        if not _is_valid_entity_id(proposal_id):
            return None
        return "profile.confirm_update", {"proposal_id": proposal_id}
    if callback_data.startswith("profile:reject:"):
        proposal_id = callback_data.removeprefix("profile:reject:")
        if not _is_valid_entity_id(proposal_id):
            return None
        return "profile.reject_update", {"proposal_id": proposal_id}

    action_type = _ACTION_BY_CALLBACK.get(callback_data)
    if action_type is None:
        return None
    params = dict(_CALLBACK_PARAMS.get(callback_data, {}))
    return action_type, params


def _resolve_command_action(message_text: str) -> tuple[str, dict] | None:
    normalized_full = message_text.strip().lower()
    action_type = _ACTION_BY_COMMAND.get(normalized_full)
    if action_type is not None:
        return action_type, {}

    parts = message_text.split(maxsplit=1)
    if not parts:
        return None

    command = parts[0].strip().lower()
    if command == "/claim":
        claim_id = parts[1].strip() if len(parts) > 1 else ""
        return "claim.lookup", {"claim_id": claim_id} if claim_id else {}

    if command == "/throttle":
        rest = parts[1].strip() if len(parts) > 1 else ""
        return "profile.set_throttle", {"minutes": rest}

    if command == "/buy_extra":
        rest = parts[1].strip() if len(parts) > 1 else ""
        return "billing.buy_extra", {"feature_key": rest}

    if command == "/tz":
        tz = parts[1].strip() if len(parts) > 1 else ""
        return "profile.set_timezone", {"timezone": tz}

    if command == "/quiet":
        # ``/quiet`` (no args)       → show current profile
        # ``/quiet off``/``/quiet clear`` → clear all windows
        # ``/quiet 22-8``             → single window 22..08 every day
        # Handler validates/reports bad syntax.
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not rest:
            return "profile.show", {}
        return "profile.set_quiet_hours", {"args_raw": rest}

    if command == "/skill":
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not rest:
            return "skills.list", {}
        tokens = rest.split()
        feature_key = tokens[0].lower()
        if len(tokens) == 1:
            return "skill.show", {"feature_key": feature_key}
        # ``/skill <key> priority <level>``
        if len(tokens) >= 3 and tokens[1].lower() == "priority":
            return "skill.set_priority", {
                "feature_key": feature_key,
                "priority": tokens[2].lower(),
            }
        # Unrecognized subcommand — still route to ``skill.show`` so the
        # handler can render a help message pointing to correct syntax.
        return "skill.show", {"feature_key": feature_key}

    action_type = _ACTION_BY_COMMAND.get(command)
    if action_type is not None:
        return action_type, {}

    # Fallback for free-form text (Phase 3): any non-command message
    # routes to the conversational LLM handler. Slash-commands we don't
    # recognize still fall through here — the LLM will respond with
    # "я не знаю такой команды", which is usually a better UX than
    # silently ignoring.
    if not message_text.strip():
        return None
    return "conversation.chat", {"text": message_text.strip()}


def _extract_message_text(payload: dict) -> str | None:
    for key in ("message", "edited_message"):
        message = payload.get(key)
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


_ACTION_BY_COMMAND = {
    "/help": "help.show",
    "помощь": "help.show",
    "/status": "status.show",
    "мой статус": "status.show",
    "/subscriptions": "subscriptions.show",
    "подписки": "subscriptions.show",
    "/profile": "profile.show",
    "/skills": "skills.list",
    "/stats": "stats.show",
}

_ACTION_BY_CALLBACK = {
    STATUS_CALLBACK: "status.show",
    SUBSCRIPTIONS_CALLBACK: "subscriptions.show",
    CONNECT_BASE_CALLBACK: "subscription.connect_base",
    ADD_EDS_ACCOUNT_CALLBACK: "subscription.add_eds",
    RENEW_CALLBACK: "subscription.renew_cycle",
    CONNECT_EDS_CALLBACK: "eds.connect.start",
    RETRY_CONNECT_PRIMARY_CALLBACK: "eds.connect.retry",
    RETRY_CONNECT_EXTRA_CALLBACK: "eds.connect.retry",
    REMOVE_EDS_ACCOUNT_CALLBACK: "eds.slot.remove_free",
    RESTORE_EDS_ACCOUNT_CALLBACK: "eds.slot.restore_free",
    CONNECT_VOICE_CALLBACK: "subscription.connect_voice",
    CANCEL_VOICE_CALLBACK: "subscription.cancel_voice",
}

_CALLBACK_PARAMS = {
    CONNECT_EDS_CALLBACK: {"slot_type": "available_slot"},
    RETRY_CONNECT_PRIMARY_CALLBACK: {"slot_type": "primary"},
    RETRY_CONNECT_EXTRA_CALLBACK: {"slot_type": "extra"},
}

_ENTITY_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _is_valid_entity_id(value: str) -> bool:
    """Reject garbage after ``removeprefix`` — only allow safe identifiers."""
    return bool(value and _ENTITY_ID_RE.match(value))
