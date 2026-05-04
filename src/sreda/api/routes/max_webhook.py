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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from sreda.config.settings import get_settings
from sreda.services.max_inbound import handle_max_update


router = APIRouter(prefix="/api/max", tags=["max"])
logger = logging.getLogger(__name__)


# Phase 11 live-debug (2026-05-04): MAX в первом prod-сообщении шлёт webhook,
# но мы не знаем точный header в котором приходит secret_token. Логируем все
# request headers на каждом запросе ПОКА не закрепим контракт; потом этот
# блок убираем.
# Candidate header names per docs/probe — разные мессенджеры используют
# разные конвенции. MAX docs не специфицируют, поэтому пробуем все три
# наиболее вероятные.
_CANDIDATE_SECRET_HEADERS = (
    "x-sreda-max-webhook-secret",   # как мы изначально предположили
    "x-bot-api-secret-token",       # TG-style
    "x-max-bot-api-secret-token",   # MAX-namespaced TG-style
    "secret-token",                  # bare
    "x-secret-token",                # bare с x-prefix
)


def _verify_max_secret_token(request: Request) -> None:
    """Verify secret token header.

    Tries multiple candidate header names (см. _CANDIDATE_SECRET_HEADERS)
    т.к. точный header который MAX использует не задокументирован.
    Если ни один не совпал — 401 + лог всех headers для диагностики.
    """
    expected = get_settings().max_webhook_secret_token
    if not expected:
        logger.warning(
            "max webhook accepted без secret check — "
            "max_webhook_secret_token не настроен; "
            "это OK для dev, в prod ставит admin alert."
        )
        return

    headers_lower = {k.lower(): v for k, v in request.headers.items()}
    for name in _CANDIDATE_SECRET_HEADERS:
        candidate = headers_lower.get(name)
        if candidate and hmac.compare_digest(candidate, expected):
            return  # match → accept

    # No candidate matched — log full headers (без значений для приватности
    # values, чтобы не утекли потенциальные tokens в логи) для диагностики.
    header_keys = sorted(headers_lower.keys())
    logger.warning(
        "max webhook rejected: secret token mismatch. "
        "incoming header keys=%s",
        header_keys,
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.post(
    "/webhook",
    status_code=status.HTTP_202_ACCEPTED,
)
async def max_webhook(
    payload: dict,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict:
    """Thin wrapper над ``handle_max_update`` (mirror TG webhook шаблон).

    MAX шлёт single update per webhook call (не array), формат подтверждён
    в Phase 0 probe — `update_type=bot_started` или `message_created`.

    Возвращаем ``{"ok": True, "request_id": <inbound_message_id>}``.
    202 ACCEPTED — durable ingest committed, heavy work detached.
    """
    _verify_max_secret_token(request)
    inbound_message_id = await handle_max_update(
        payload, bot_key="sreda_max", background_tasks=background_tasks,
    )
    return {"ok": True, "request_id": inbound_message_id}
