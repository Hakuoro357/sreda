from __future__ import annotations

from dataclasses import asdict, dataclass

from sreda.services.billing import STATUS_CALLBACK, SUBSCRIPTIONS_CALLBACK
from sreda.services.onboarding import TelegramOnboardingResult


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
        action_type = _ACTION_BY_CALLBACK.get(callback_data)
        if action_type is None:
            return None
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
            params={},
        )

    message_text = _extract_message_text(payload)
    if not message_text:
        return None
    normalized = message_text.strip().lower()
    action_type = _ACTION_BY_COMMAND.get(normalized)
    if action_type is None:
        return None
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
        params={},
    )


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
    "/status": "status.show",
    "/subscriptions": "subscriptions.show",
}

_ACTION_BY_CALLBACK = {
    STATUS_CALLBACK: "status.show",
    SUBSCRIPTIONS_CALLBACK: "subscriptions.show",
}
