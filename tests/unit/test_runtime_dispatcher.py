from sreda.runtime.dispatcher import dispatch_telegram_action
from sreda.services.onboarding import TelegramOnboardingResult


def _onboarding() -> TelegramOnboardingResult:
    return TelegramOnboardingResult(
        is_new_user=False,
        chat_id="100000003",
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        user_id="user_1",
        assistant_id="assistant_1",
    )


def test_dispatch_telegram_action_maps_status_command() -> None:
    payload = {
        "message": {
            "chat": {"id": 100000003, "type": "private"},
            "text": "/status",
        }
    }

    action = dispatch_telegram_action(
        payload=payload,
        bot_key="sreda",
        onboarding=_onboarding(),
        inbound_message_id="in_1",
    )

    assert action is not None
    assert action.action_type == "status.show"
    assert action.external_chat_id == "100000003"
    assert action.source_type == "telegram_message"


def test_dispatch_telegram_action_maps_subscriptions_callback() -> None:
    payload = {
        "callback_query": {
            "id": "cb_1",
            "data": "billing:subscriptions",
            "message": {"chat": {"id": 100000003, "type": "private"}},
        }
    }

    action = dispatch_telegram_action(
        payload=payload,
        bot_key="sreda",
        onboarding=_onboarding(),
        inbound_message_id="in_2",
    )

    assert action is not None
    assert action.action_type == "subscriptions.show"
    assert action.source_type == "telegram_callback"
    assert action.source_value == "billing:subscriptions"


def test_dispatch_telegram_action_ignores_mutation_callback() -> None:
    payload = {
        "callback_query": {
            "id": "cb_2",
            "data": "billing:connect_plan:eds_monitor_base",
            "message": {"chat": {"id": 100000003, "type": "private"}},
        }
    }

    action = dispatch_telegram_action(
        payload=payload,
        bot_key="sreda",
        onboarding=_onboarding(),
        inbound_message_id="in_3",
    )

    assert action is None
