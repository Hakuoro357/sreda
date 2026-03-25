from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from sreda.db.models.core import User
from sreda.db.repositories.seed import SeedRepository
from sreda.services.billing import STATUS_CALLBACK, SUBSCRIPTIONS_CALLBACK

CONNECT_EDS_CALLBACK = "onboarding:connect_eds"


@dataclass(slots=True)
class TelegramOnboardingResult:
    is_new_user: bool
    chat_id: str | None
    tenant_id: str | None
    workspace_id: str | None
    user_id: str | None
    assistant_id: str | None


def ensure_telegram_user_bundle(session: Session, payload: dict) -> TelegramOnboardingResult:
    chat_id = _extract_chat_id(payload)
    if chat_id is None:
        return TelegramOnboardingResult(False, None, None, None, None, None)

    existing_user = session.query(User).filter(User.telegram_account_id == chat_id).one_or_none()
    if existing_user is not None:
        return TelegramOnboardingResult(
            False,
            chat_id,
            existing_user.tenant_id,
            f"workspace_tg_{chat_id}",
            existing_user.id,
            f"assistant_tg_{chat_id}",
        )

    display_name = _extract_display_name(payload) or f"Пользователь {chat_id}"
    tenant_id = f"tenant_tg_{chat_id}"
    workspace_id = f"workspace_tg_{chat_id}"
    user_id = f"user_tg_{chat_id}"
    assistant_id = f"assistant_tg_{chat_id}"

    SeedRepository(session).ensure_tenant_bundle(
        tenant_id=tenant_id,
        tenant_name=display_name,
        workspace_id=workspace_id,
        workspace_name=display_name,
        user_id=user_id,
        telegram_account_id=chat_id,
        assistant_id=assistant_id,
        assistant_name="Среда",
        eds_monitor_enabled=False,
    )

    return TelegramOnboardingResult(True, chat_id, tenant_id, workspace_id, user_id, assistant_id)


def build_welcome_message() -> tuple[str, dict]:
    text = (
        "Привет! Я Среда.\n\n"
        "Я помогу следить за важными изменениями и управлять подписками вокруг EDS.\n"
        "Сейчас можно посмотреть статус, открыть подписки и подключить EDS Monitor."
    )
    reply_markup = {
        "inline_keyboard": [
            [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
            [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
            [
                {
                    "text": "Подключить EDS",
                    "callback_data": CONNECT_EDS_CALLBACK,
                }
            ]
        ]
    }
    return text, reply_markup


def build_connect_eds_message(*, base_active: bool, connected_count: int, allowed_count: int) -> str:
    if not base_active:
        return (
            "Сначала подключи подписку EDS Monitor.\n\n"
            "После этого можно будет привязать кабинет EDS."
        )
    return (
        "Подключение EDS скоро будет доступно через защищенную веб-страницу.\n\n"
        f"Сейчас подключено кабинетов: {connected_count} из {allowed_count}.\n"
        "Следующий шаг — открыть защищенную форму и передать логин и пароль от кабинета."
    )


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
