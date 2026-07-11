"""Re-arm the Telegram webhook — EMERGENCY / rollback tool only.

WARNING: This script sets a Telegram webhook, which DISABLES long-poll
(getUpdates returns 409 Conflict). The production inbound path is long-poll
via sreda-telegram-poller@*.service. Running this accidentally will break
prod for all users until the poller is manually restarted.

This script REFUSES to run if any sreda-telegram-poller@*.service or the
legacy sreda-telegram-poller.service is enabled or active on the system.
Use --force-webhook-mode to override the guard (ONLY when intentionally
switching the entire system from long-poll to webhook mode).

Typical rollback sequence (long-poll → webhook):

    sudo systemctl disable --now sreda-telegram-poller@sreda.service
    sudo systemctl disable --now sreda-telegram-poller@sreda_home.service
    # verify both are inactive:
    systemctl is-active sreda-telegram-poller@sreda.service
    systemctl is-active sreda-telegram-poller@sreda_home.service
    # then set webhook:
    sudo /opt/sreda/.venv/bin/python -m sreda.scripts.restore_webhook --force-webhook-mode

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

import subprocess
import sys

import httpx

from sreda.config.settings import get_settings
from sreda.services.webhook_security import (
    is_webhook_deployed,
    is_webhook_secret_configured,
    normalized_webhook_secret,
)


# Дефолтный прод-URL (док/совместимость). #341: фактический URL берётся из
# SREDA_TELEGRAM_WEBHOOK_URL — тот же дискриминатор, что арматирует route-гейт.
WEBHOOK_URL = "https://bot.sredaspace.ru/webhooks/telegram/sreda"
TELEGRAM_IP = "62.113.41.104"
ALLOWED_UPDATES = '["message","edited_message","callback_query"]'
MAX_CONNECTIONS = "4"

# All systemd units (templated + legacy) that run long-poll polling.
# If any of these is enabled OR active, we refuse to set a webhook by default.
_POLLER_UNITS = [
    "sreda-telegram-poller@sreda.service",
    "sreda-telegram-poller@sreda_home.service",
    "sreda-telegram-poller.service",  # legacy non-template unit (pre-cutover)
]


def _is_unit_enabled_or_active(unit: str) -> bool:
    """Return True if the systemd unit is enabled OR active."""
    for sub in ("is-enabled", "is-active"):
        rc = subprocess.run(
            ["systemctl", sub, unit],
            capture_output=True, text=True,
        )
        result = (rc.stdout + rc.stderr).lower()
        # 'not-found' means the unit file doesn't exist at all — not enabled/active.
        if "not-found" in result:
            continue
        if rc.returncode == 0:
            return True
    return False


def _any_poller_active() -> list[str]:
    """Return list of poller units that are currently enabled or active."""
    active: list[str] = []
    for unit in _POLLER_UNITS:
        if _is_unit_enabled_or_active(unit):
            active.append(unit)
    return active


def main() -> int:
    force_webhook = "--force-webhook-mode" in sys.argv[1:]

    # ------------------------------------------------------------------
    # Guard: refuse if any long-poll poller is running
    # ------------------------------------------------------------------
    active_pollers = _any_poller_active()
    if active_pollers and not force_webhook:
        print(
            "\n"
            "ERROR: The following Telegram long-poll units are enabled or active:\n"
            + "".join(f"  - {u}\n" for u in active_pollers)
            + "\n"
            "Setting a webhook while long-poll is running will cause getUpdates\n"
            "to return 409 Conflict, breaking all inbound messages.\n"
            "\n"
            "To set webhook intentionally:\n"
            "  1. Stop and disable all poller units:\n"
            "       sudo systemctl disable --now sreda-telegram-poller@sreda.service\n"
            "       sudo systemctl disable --now sreda-telegram-poller@sreda_home.service\n"
            "  2. Verify both are inactive:\n"
            "       systemctl is-active sreda-telegram-poller@sreda.service\n"
            "       systemctl is-active sreda-telegram-poller@sreda_home.service\n"
            "  3. Re-run with --force-webhook-mode:\n"
            "       python -m sreda.scripts.restore_webhook --force-webhook-mode\n",
            file=sys.stderr,
        )
        return 1

    if force_webhook and active_pollers:
        print(
            f"WARNING: --force-webhook-mode set but pollers still active: {active_pollers}\n"
            "Proceeding anyway — this WILL cause 409 Conflict on the pollers.",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Validate settings
    # ------------------------------------------------------------------
    settings = get_settings()
    if not settings.telegram_bot_token:
        print("SREDA_TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1
    # #341 (F1, Codex R-codex R2 MAJOR): webhook-режим требует
    # SREDA_TELEGRAM_WEBHOOK_URL — тот же дискриминатор, что арматирует
    # route-гейт running-app. Без него роут остался бы на permissive fallback,
    # а этот скрипт зарегистрировал бы внешний webhook → fail-open.
    if not is_webhook_deployed(
        bot_token=settings.telegram_bot_token,
        webhook_url=settings.telegram_webhook_url,
    ):
        print(
            "SREDA_TELEGRAM_WEBHOOK_URL is not set; refusing to set webhook — "
            "webhook mode requires it so the running app's route gate is armed "
            "(otherwise route stays permissive fallback, #341)",
            file=sys.stderr,
        )
        return 1
    # #341: пробельный/пустой secret трактуется как ОТСУТСТВУЮЩИЙ (нормализация).
    if not is_webhook_secret_configured(settings.telegram_webhook_secret_token):
        print(
            "SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN is not set (or whitespace); "
            "refusing to set webhook without it (would accept hostile inbound)",
            file=sys.stderr,
        )
        return 1

    # ------------------------------------------------------------------
    # Set webhook — URL и secret из того же дискриминатора, что видит route-гейт.
    # ------------------------------------------------------------------
    response = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
        data={
            "url": settings.telegram_webhook_url,
            "ip_address": TELEGRAM_IP,
            "secret_token": normalized_webhook_secret(
                settings.telegram_webhook_secret_token
            ),
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
