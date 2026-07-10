import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status

from sreda.api.deps import enforce_telegram_rate_limit
from sreda.config.bot_registry import TelegramBotRegistry
from sreda.config.settings import get_settings
from sreda.schemas.api import TelegramWebhookAccepted
from sreda.services.telegram_inbound import handle_telegram_update
from sreda.services.webhook_security import webhook_secret_missing_while_deployed

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram"])
logger = logging.getLogger(__name__)


def _resolve_bot_key(bot_key: str) -> str:
    """Fail-closed bot_key validation against the registry.

    Rejects unknown bot_keys with HTTP 404 BEFORE any secret-token check or
    processing.  This prevents arbitrary-bot_key spoofing where an attacker
    crafts a URL like ``/webhooks/telegram/evil_bot`` and (if the secret
    check were bypassed or absent) routes inbound traffic to an unregistered
    bot_key.

    Per-bot webhook secrets are out of scope (BotConfig has no
    webhook_secret field; webhook is not the prod inbound path).  The goal
    here is solely to stop unknown bot_key strings from reaching the handler.

    Raises
    ------
    HTTPException(404)
        When *bot_key* is not in the registry built from current settings.
    """
    registry = TelegramBotRegistry.from_settings(get_settings())
    try:
        registry.resolve(bot_key)
    except KeyError:
        logger.warning("telegram webhook rejected: unknown bot_key=%r", bot_key)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown bot_key: {bot_key!r}",
        )
    return bot_key


def _verify_telegram_secret_token(
    secret_token_header: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> None:
    settings = get_settings()
    expected = settings.telegram_webhook_secret_token
    if not expected:
        # #341 (F1 paritet): fail-closed, если TG развёрнут в webhook-режиме
        # (telegram_bot_token+telegram_webhook_url заданы), но secret пуст.
        # Прод-вход TG = long-poll → telegram_webhook_url не задан → этот гейт
        # инертен, dev/long-poll fallback (accept) сохраняется. Явная настройка
        # webhook-режима без секрета = отклоняем неаутентифицированный inbound.
        if webhook_secret_missing_while_deployed(
            bot_token=settings.telegram_bot_token,
            webhook_url=settings.telegram_webhook_url,
            secret=expected,
        ):
            logger.error(
                "telegram webhook rejected: secret не настроен, но webhook-режим "
                "развёрнут (telegram_bot_token+telegram_webhook_url) — "
                "fail-closed (#341)"
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        # Dev-fallback: secret не настроен и webhook-режим не заявлен (long-poll)
        # — принимаем, чтобы локальные/тестовые сетапы работали.
        return
    if secret_token_header is None or not hmac.compare_digest(
        secret_token_header, expected
    ):
        logger.warning("telegram webhook rejected: secret token mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.post(
    "/{bot_key}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TelegramWebhookAccepted,
    # Dependency order matters:
    #   1. Rate-limit — reject hostile traffic cheaply before any crypto work.
    #   2. Secret-token check — HMAC comparison (inexpensive but auth-critical).
    # bot_key registry check runs inside the handler via _resolve_bot_key
    # (path parameter dependency), which is evaluated before the handler body.
    dependencies=[
        Depends(enforce_telegram_rate_limit),
        Depends(_verify_telegram_secret_token),
    ],
)
async def telegram_webhook(
    bot_key: str,
    payload: dict,
    background_tasks: BackgroundTasks,
    _validated_bot_key: str = Depends(_resolve_bot_key),
) -> TelegramWebhookAccepted:
    """Thin wrapper over the channel-agnostic durable-ingest handler.

    ``await handle_telegram_update`` runs synchronously BEFORE we
    return 202 — without that guarantee, Telegram would consider the
    update delivered and never retry, so any crash between ack and
    persist would silently lose updates. The heavy LLM/voice/outbox
    work is detached inside ``handle_telegram_update`` via
    ``asyncio.create_task``, so the durable-ingest path itself stays
    inside the ~1s webhook timeout budget.

    The same ``handle_telegram_update`` is also called by the
    long-polling worker (``sreda.workers.telegram_long_poll``) for each
    update from ``getUpdates``, which is the path we are migrating to
    in spring 2026 to escape the inbound-TCP fragility we saw on
    bot.sredaspace.ru.

    ``bot_key`` is validated against the registry by ``_resolve_bot_key``
    before this handler body runs (Phase 9 fail-closed anti-spoof guard).
    """
    inbound_message_id = await handle_telegram_update(
        payload, bot_key=bot_key, background_tasks=background_tasks,
    )
    return TelegramWebhookAccepted(ok=True, request_id=inbound_message_id)
