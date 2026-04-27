from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from sreda.db.models.core import Assistant, User, Workspace
from sreda.db.repositories.seed import SeedRepository

CONNECT_EDS_CALLBACK = "onboarding:connect_eds"


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


def ensure_telegram_user_bundle(session: Session, payload: dict) -> TelegramOnboardingResult:
    chat_id = _extract_chat_id(payload)
    if chat_id is None:
        return TelegramOnboardingResult(False, None, None, None, None, None)

    existing_user = find_user_by_chat_id(session, chat_id)
    if existing_user is not None:
        assistant = (
            session.query(Assistant)
            .filter(Assistant.tenant_id == existing_user.tenant_id)
            .order_by(Assistant.id.asc())
            .first()
        )
        workspace_id = assistant.workspace_id if assistant is not None else None
        if workspace_id is None:
            workspace = (
                session.query(Workspace)
                .filter(Workspace.tenant_id == existing_user.tenant_id)
                .order_by(Workspace.id.asc())
                .first()
            )
            workspace_id = workspace.id if workspace is not None else None
        return TelegramOnboardingResult(
            False,
            chat_id,
            existing_user.tenant_id,
            workspace_id,
            existing_user.id,
            assistant.id if assistant is not None else None,
        )

    display_name = _extract_display_name(payload) or f"Пользователь {chat_id}"
    return ensure_telegram_user_bundle_by_id(
        session, telegram_id=chat_id, display_name=display_name
    )


def ensure_telegram_user_bundle_by_id(
    session: Session,
    *,
    telegram_id: str,
    display_name: str | None = None,
) -> TelegramOnboardingResult:
    """Ensure a tenant/user/assistant bundle exists for a Telegram account.

    Unlike ``ensure_telegram_user_bundle``, this variant does not require
    a webhook payload — used by Mini App auth to lazily provision a bundle
    when the user opens the WebApp before ever sending /start. The
    WebApp's initData hash is signed by Telegram, so the telegram_id is
    trustworthy at this point.

    Returns the same ``TelegramOnboardingResult`` shape. ``is_new_user``
    is True only when a new bundle was actually created.
    """
    if not telegram_id:
        return TelegramOnboardingResult(False, None, None, None, None, None)

    existing_user = find_user_by_chat_id(session, telegram_id)
    if existing_user is not None:
        assistant = (
            session.query(Assistant)
            .filter(Assistant.tenant_id == existing_user.tenant_id)
            .order_by(Assistant.id.asc())
            .first()
        )
        workspace_id = assistant.workspace_id if assistant is not None else None
        if workspace_id is None:
            workspace = (
                session.query(Workspace)
                .filter(Workspace.tenant_id == existing_user.tenant_id)
                .order_by(Workspace.id.asc())
                .first()
            )
            workspace_id = workspace.id if workspace is not None else None
        return TelegramOnboardingResult(
            False,
            telegram_id,
            existing_user.tenant_id,
            workspace_id,
            existing_user.id,
            assistant.id if assistant is not None else None,
        )

    display_name = display_name or f"Пользователь {telegram_id}"
    tenant_id = f"tenant_tg_{telegram_id}"
    workspace_id = f"workspace_tg_{telegram_id}"
    user_id = f"user_tg_{telegram_id}"
    assistant_id = f"assistant_tg_{telegram_id}"

    SeedRepository(session).ensure_tenant_bundle(
        tenant_id=tenant_id,
        tenant_name=display_name,
        workspace_id=workspace_id,
        workspace_name=display_name,
        user_id=user_id,
        telegram_account_id=telegram_id,
        assistant_id=assistant_id,
        assistant_name="Среда",
        eds_monitor_enabled=False,
    )

    return TelegramOnboardingResult(
        True, telegram_id, tenant_id, workspace_id, user_id, assistant_id
    )


def build_post_approve_message() -> str:
    """Сообщение после admin-approve (2026-04-27 simplified).

    Юзер только что прошёл pending-цепочку из 11 сообщений (включая
    представление Среды и обзор всех функций). Здесь просто
    подтверждаем что доступ открыт и спрашиваем имя — без кнопок,
    без расспросов про семью / диеты / другие данные. LLM сама
    спросит дальше, когда уместно.
    """
    return (
        "✅ Готово! Модератор открыл доступ — рада знакомству.\n\n"
        "Прежде чем приступим, подскажи, как к тебе обращаться? "
        "Имя или ник, как удобно. Это нужно, чтобы напоминания и "
        "сообщения были по-человечески, а не «уважаемый пользователь»."
    )


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
