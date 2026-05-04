"""MAX (российский мессенджер) webhook route.

Phase 5 of MAX integration. Mirror шаблон ``api/routes/telegram_webhook.py``
со специфичными deltas:

- Endpoint: ``/api/max/webhook`` (per plan; nginx уже route'ит /api на uvicorn)
- Auth: header ``X-Sreda-MAX-Webhook-Secret`` сравнивается с
  ``settings.max_webhook_secret_token``. При регистрации (lifespan через
  ``MaxClient.set_webhook(secret_token=...)``) MAX server шлёт это значение
  в каждом webhook POST.
- Если probe (Phase 11 live test) покажет что MAX не поддерживает
  ``secret_token`` field в /subscriptions → fallback на HMAC-SHA256(body,
  key=bot_token). Сейчас пишем код для primary path (header check).
- Inbound payload — single update объект, не array (отличие от MAX
  ``GET /updates`` which возвращает list).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status

from sreda.config.settings import get_settings
from sreda.services.max_inbound import handle_max_update


router = APIRouter(prefix="/api/max", tags=["max"])
logger = logging.getLogger(__name__)


def _verify_max_secret_token(
    secret_header: str | None = Header(
        default=None,
        alias="X-Sreda-MAX-Webhook-Secret",
    ),
) -> None:
    """Verify secret token header.

    При первом deploy лучше использовать настоящий secret в env
    (``SREDA_MAX_WEBHOOK_SECRET_TOKEN``). Без secret в env — request
    accepted (dev fallback) с warning log; в prod admin-alert через
    health-check Phase 9.
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
        logger.warning("max webhook rejected: secret token mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.post(
    "/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_verify_max_secret_token)],
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
