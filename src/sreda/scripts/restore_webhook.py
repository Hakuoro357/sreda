"""Re-arm the Telegram webhook on rollback.

Usage during rollback from long-poll → webhook (см. plan
mellow-discovering-conway.md):

    sudo systemctl stop sreda-telegram-poller
    sudo systemctl disable sreda-telegram-poller
    sudo /opt/sreda/.venv/bin/python -m sreda.scripts.restore_webhook

Why a Python helper instead of inline ``curl + grep | cut``:

* Secret extraction via ``grep | cut`` is fragile on values that contain
  ``=``, spaces, quoting. Pydantic settings already parse ``/etc/sreda/.env``
  the same way prod does — reuse that path so we never accidentally
  surface the wrong secret.
* Fail-fast on ``ok=false``: ``raise_for_status()`` plus an explicit
  check on the JSON body. Plain curl + grep would print the failure
  but exit 0, hiding the broken rollback.
"""

from __future__ import annotations

import sys

import httpx

from sreda.config.settings import get_settings


WEBHOOK_URL = "https://bot.sredaspace.ru/webhooks/telegram/sreda"
TELEGRAM_IP = "62.113.41.104"
ALLOWED_UPDATES = '["message","edited_message","callback_query"]'
MAX_CONNECTIONS = "4"


def main() -> int:
    settings = get_settings()
    if not settings.telegram_bot_token:
        print("SREDA_TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1
    if not settings.telegram_webhook_secret_token:
        print(
            "SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN is not set; refusing to "
            "set webhook without it (would accept hostile inbound)",
            file=sys.stderr,
        )
        return 1

    response = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
        data={
            "url": WEBHOOK_URL,
            "ip_address": TELEGRAM_IP,
            "secret_token": settings.telegram_webhook_secret_token,
            "max_connections": MAX_CONNECTIONS,
            # drop_pending_updates=false — keep updates queued at TG so
            # rollback is non-destructive.
            "drop_pending_updates": "false",
            "allowed_updates": ALLOWED_UPDATES,
        },
        timeout=10,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"setWebhook HTTP {exc.response.status_code}: {exc.response.text}",
              file=sys.stderr)
        return 1
    body = response.json()
    if not body.get("ok"):
        print(f"setWebhook ok=false: {body}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
