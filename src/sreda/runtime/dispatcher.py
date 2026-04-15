from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sreda.services.billing import (
    ADD_EDS_ACCOUNT_CALLBACK,
    CONNECT_BASE_CALLBACK,
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
from sreda.services.onboarding import CONNECT_EDS_CALLBACK, TelegramOnboardingResult


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
