from __future__ import annotations

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.integrations.telegram.client import TelegramClient
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService
from sreda.services.eds_account_verification import (
    RETRY_CONNECT_EXTRA_CALLBACK,
    RETRY_CONNECT_PRIMARY_CALLBACK,
)
from sreda.services.billing import (
    ADD_EDS_ACCOUNT_CALLBACK,
    BillingService,
    CANCEL_BASE_CALLBACK,
    CONNECT_BASE_CALLBACK,
    REMOVE_EDS_ACCOUNT_CALLBACK,
    RENEW_CALLBACK,
    STATUS_CALLBACK,
    SUBSCRIPTIONS_CALLBACK,
)
from sreda.services.onboarding import (
    CONNECT_EDS_CALLBACK,
    TelegramOnboardingResult,
    build_connect_eds_message,
    build_welcome_message,
)


async def handle_telegram_interaction(
    session: Session,
    *,
    bot_key: str,
    payload: dict,
    telegram_client: TelegramClient,
    onboarding: TelegramOnboardingResult,
) -> None:
    if onboarding.chat_id is None:
        return

    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        await _handle_callback(
            session,
            telegram_client=telegram_client,
            chat_id=onboarding.chat_id,
            callback_query=callback_query,
            onboarding=onboarding,
        )
        return

    if onboarding.is_new_user:
        text, reply_markup = build_welcome_message()
        await telegram_client.send_message(
            chat_id=onboarding.chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return

    message_text = _extract_message_text(payload)
    if not message_text:
        return

    await _handle_command(
        session,
        telegram_client=telegram_client,
        chat_id=onboarding.chat_id,
        tenant_id=onboarding.tenant_id,
        message_text=message_text,
    )


async def _handle_callback(
    session: Session,
    *,
    telegram_client: TelegramClient,
    chat_id: str,
    callback_query: dict,
    onboarding: TelegramOnboardingResult,
) -> None:
    callback_data = callback_query.get("data")
    callback_id = callback_query.get("id")
    if callback_id:
        await telegram_client.answer_callback_query(str(callback_id), text="Готово")

    if not isinstance(callback_data, str):
        return

    billing = BillingService(session)
    tenant_id = onboarding.tenant_id
    if tenant_id and callback_data in {CONNECT_EDS_CALLBACK, RETRY_CONNECT_PRIMARY_CALLBACK, RETRY_CONNECT_EXTRA_CALLBACK}:
        requested_slot_type = "primary" if callback_data == RETRY_CONNECT_PRIMARY_CALLBACK else "extra"
        if callback_data == CONNECT_EDS_CALLBACK:
            requested_slot_type = "primary" if billing.get_summary(tenant_id).connected_count == 0 else "extra"
        await _handle_connect_callback(
            session,
            telegram_client=telegram_client,
            chat_id=chat_id,
            tenant_id=tenant_id,
            workspace_id=onboarding.workspace_id,
            user_id=onboarding.user_id,
            requested_slot_type=requested_slot_type,
        )
        return

    if callback_data == STATUS_CALLBACK and tenant_id:
        text, reply_markup = billing.build_status_message(tenant_id)
        await telegram_client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return
    if callback_data == SUBSCRIPTIONS_CALLBACK and tenant_id:
        text, reply_markup = billing.build_subscriptions_message(tenant_id)
        await telegram_client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return
    if callback_data == CONNECT_BASE_CALLBACK and tenant_id:
        result = billing.start_base_subscription(tenant_id)
        await telegram_client.send_message(
            chat_id=chat_id,
            text=result.message_text,
            reply_markup=result.reply_markup,
        )
        return
    if callback_data == ADD_EDS_ACCOUNT_CALLBACK and tenant_id:
        result = billing.add_extra_eds_account(tenant_id)
        await telegram_client.send_message(
            chat_id=chat_id,
            text=result.message_text,
            reply_markup=result.reply_markup,
        )
        return
    if callback_data == REMOVE_EDS_ACCOUNT_CALLBACK and tenant_id:
        result = billing.remove_extra_account_at_period_end(tenant_id)
        await telegram_client.send_message(
            chat_id=chat_id,
            text=result.message_text,
            reply_markup=result.reply_markup,
        )
        return
    if callback_data == RENEW_CALLBACK and tenant_id:
        result = billing.renew_cycle(tenant_id)
        await telegram_client.send_message(
            chat_id=chat_id,
            text=result.message_text,
            reply_markup=result.reply_markup,
        )
        return
    if callback_data == CANCEL_BASE_CALLBACK and tenant_id:
        result = billing.cancel_base_at_period_end(tenant_id)
        await telegram_client.send_message(
            chat_id=chat_id,
            text=result.message_text,
            reply_markup=result.reply_markup,
        )
        return
    if callback_data == "events:latest":
        await telegram_client.send_message(
            chat_id=chat_id,
            text="Раздел последних событий уже зарезервирован. Как только появятся данные EDS, я покажу их здесь.",
            reply_markup={"inline_keyboard": [[{"text": "Мой статус", "callback_data": STATUS_CALLBACK}]]},
        )


async def _handle_command(
    session: Session,
    *,
    telegram_client: TelegramClient,
    chat_id: str,
    tenant_id: str | None,
    message_text: str,
) -> None:
    command = message_text.strip().lower()
    billing = BillingService(session)

    if command in {"/help", "помощь"}:
        text, reply_markup = billing.build_help_message()
        await telegram_client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return
    if command in {"/status", "мой статус"} and tenant_id:
        text, reply_markup = billing.build_status_message(tenant_id)
        await telegram_client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return
    if command in {"/subscriptions", "подписки"} and tenant_id:
        text, reply_markup = billing.build_subscriptions_message(tenant_id)
        await telegram_client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return
    if command in {"/events", "последние события"}:
        await telegram_client.send_message(
            chat_id=chat_id,
            text="Раздел последних событий уже зарезервирован. Как только появятся данные EDS, я покажу их здесь.",
            reply_markup={"inline_keyboard": [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]},
        )
        return


def _extract_message_text(payload: dict) -> str | None:
    for key in ("message", "edited_message"):
        message = payload.get(key)
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


async def _handle_connect_callback(
    session: Session,
    *,
    telegram_client: TelegramClient,
    chat_id: str,
    tenant_id: str,
    workspace_id: str | None,
    user_id: str | None,
    requested_slot_type: str,
) -> None:
    billing = BillingService(session)
    summary = billing.get_summary(tenant_id)
    if not summary.base_active:
        await telegram_client.send_message(
            chat_id=chat_id,
            text=build_connect_eds_message(
                base_active=False,
                connected_count=summary.connected_count,
                allowed_count=summary.allowed_count,
            ),
            reply_markup=_build_connect_reply_markup(False),
        )
        return
    if summary.connected_count >= summary.allowed_count:
        await telegram_client.send_message(
            chat_id=chat_id,
            text="Сейчас все оплаченные кабинеты уже заняты.\n\nЕсли нужен еще один кабинет, сначала добавь его в подписках.",
            reply_markup={"inline_keyboard": [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]},
        )
        return

    connect_service = EDSConnectService(session, get_settings())
    try:
        link = connect_service.create_connect_link(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            slot_type=requested_slot_type,
        )
    except ConnectSessionError as exc:
        await telegram_client.send_message(
            chat_id=chat_id,
            text=exc.message,
            reply_markup={"inline_keyboard": [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]},
        )
        return

    await telegram_client.send_message(
        chat_id=chat_id,
        text=(
            "Открывай защищенную страницу по кнопке ниже.\n\n"
            "Ссылка действует 15 минут и только для одного использования."
        ),
        reply_markup={
            "inline_keyboard": [
                [_build_connect_open_button(link.url)],
                [{"text": "Отменить", "callback_data": STATUS_CALLBACK}],
            ]
        },
    )


def _build_connect_reply_markup(base_active: bool) -> dict:
    if base_active:
        return {
            "inline_keyboard": [
                [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
            ]
        }
    return {
        "inline_keyboard": [
            [{"text": "Подключить EDS Monitor", "callback_data": CONNECT_BASE_CALLBACK}],
            [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
        ]
    }


def _build_connect_open_button(url: str) -> dict:
    if url.startswith("https://"):
        return {"text": "Открыть Mini App", "web_app": {"url": url}}
    return {"text": "Открыть защищенную страницу", "url": url}
