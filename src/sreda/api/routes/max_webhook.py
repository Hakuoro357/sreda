"""MAX (российский мессенджер) webhook route.

Phase 5 + 11 live-fix (2026-05-04) of MAX integration.

Auth: **path-based secret** — URL `/api/max/webhook/{secret}`. MAX
не отправляет secret-token в header (probe Phase 11: incoming headers
содержат только connection/content-*/host/user-agent/x-forwarded-*/x-real-ip,
никакого X-*-Secret-* нет, хотя мы передаём `secret_token` в
`POST /subscriptions`). Стандартный workaround для webhook'ов без
header-auth — secret в path/query (как Stripe legacy / GitHub).

URL формируется в lifespan (см. `main.py`):
``f"{max_webhook_url}/{max_webhook_secret_token}"``. Поэтому каждый
restart auto-register перезаписывает текущий URL у MAX. Path mismatch
→ 404 (не 401 — чтобы не палить наличие endpoint'а сканеру).

Inbound payload — single update объект, не array (отличие от MAX
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


def _verify_path_secret(secret_in_path: str, request: Request) -> None:
    """Constant-time check path secret matches env."""
    expected = get_settings().max_webhook_secret_token
    if not expected:
        # No secret configured — reject everything since path requires one.
        logger.warning(
            "max webhook hit but max_webhook_secret_token не настроен; "
            "rejecting all requests"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not hmac.compare_digest(secret_in_path, expected):
        logger.warning(
            "max webhook rejected: path secret mismatch (client=%s, ua=%s)",
            request.client.host if request.client else "?",
            request.headers.get("user-agent", "?"),
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.post(
    "/webhook/{secret}",
    status_code=status.HTTP_202_ACCEPTED,
)
async def max_webhook(
    secret: str,
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
    _verify_path_secret(secret, request)
    inbound_message_id = await handle_max_update(
        payload, bot_key="sreda_max", background_tasks=background_tasks,
    )
    return {"ok": True, "request_id": inbound_message_id}
