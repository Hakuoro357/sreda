"""MAX (российский мессенджер) webhook route.

Phase 5 of MAX integration. Mirror шаблон ``api/routes/telegram_webhook.py``.

Auth (per MAX docs 2026-05-04):
- При регистрации в payload ``POST /subscriptions`` мы посылаем поле
  ``secret`` (не ``secret_token`` как в TG — это специфика MAX).
  ``MaxClient.set_webhook`` мапит наш kwarg ``secret_token`` → ``secret``.
- MAX echo'ит это значение в каждом webhook POST'е через header
  ``X-Max-Bot-Api-Secret``. Сравнение constant-time через hmac.compare_digest.
- Mismatch → 401 (Unauthorized).

Inbound payload — single update объект (не array — отличие от MAX
``GET /updates`` которое возвращает list).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status

from sreda.config.settings import get_settings
from sreda.services.max_inbound import handle_max_update


router = APIRouter(prefix="/api/max", tags=["max"])
logger = logging.getLogger(__name__)


def _verify_max_secret(
    secret_header: str | None = Header(
        default=None, alias="X-Max-Bot-Api-Secret",
    ),
) -> None:
    """Verify X-Max-Bot-Api-Secret header (per MAX docs).

    Per MAX docs: «Если secret указан при создании подписки, он
    передаётся в заголовке X-Max-Bot-Api-Secret каждого Webhook-запроса».

    Если в env нет ``max_webhook_secret_token`` — ничего не проверяем
    (dev fallback). В prod секрет обязан быть настроен.
    """
    expected = get_settings().max_webhook_secret_token
    if not expected:
        logger.warning(
            "max webhook accepted без secret check — "
            "max_webhook_secret_token не настроен; "
            "это OK для dev, в prod ставит admin alert."
        )
        return
    if secret_header is None or not hmac.compare_digest(
        secret_header, expected
    ):
        logger.warning("max webhook rejected: X-Max-Bot-Api-Secret mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.post(
    "/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_verify_max_secret)],
)
async def max_webhook(
    payload: dict,
    background_tasks: BackgroundTasks,
) -> dict:
    """Thin wrapper над ``handle_max_update`` (mirror TG webhook шаблон).

    MAX шлёт single update per webhook call (не array), формат подтверждён
    в Phase 0 probe — `update_type=bot_started` или `message_created`.

    Возвращаем ``{"ok": True, "request_id": <inbound_message_id>}``.
    202 ACCEPTED — durable ingest committed, heavy work detached.
    """
    inbound_message_id = await handle_max_update(
        payload, bot_key="sreda_max", background_tasks=background_tasks,
    )
    return {"ok": True, "request_id": inbound_message_id}
