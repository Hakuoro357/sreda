"""Webhook secret configuration gate (#341, F1 из аудита #336).

CRITICAL security. Закрывает fail-open: если webhook-бот РАЗВЁРНУТ, но
webhook-secret не настроен, `_verify_*_secret`-зависимости пускали любой
неаутентифицированный inbound (перехват данных через подмену payload).

Дискриминатор «бот развёрнут» = заданы ОБА `*_bot_token` + `*_webhook_url`
(та же связка, что гейтит startup-регистрацию MAX-webhook в main.py).
Концепта «prod» / `APP_ENV` / `is_prod` в коде НЕТ — не изобретаем.
Чистый локальный dev (нет token+url) сохраняет permissive dev-fallback.

Telegram: прод-вход = long-poll (webhook-роут = rollback-путь), поэтому у TG
нет обязательного webhook. `telegram_webhook_url` (opt-in) выступает тем же
дискриминатором: пока он не задан (long-poll deploy), TG-гейт инертен.
"""

from __future__ import annotations


def webhook_secret_missing_while_deployed(
    *,
    bot_token: str | None,
    webhook_url: str | None,
    secret: str | None,
) -> bool:
    """True, если бот развёрнут в webhook-режиме (token+url), но secret пуст.

    Именно этот случай — активная fail-open дыра: роут смонтирован всегда,
    поддельный inbound без секрета проходит. Возвращает False, когда:
    - secret задан (нормальный fail-closed путь сравнения), ЛИБО
    - token/url не заданы (dev/long-poll — permissive fallback сохраняется).
    """
    return bool(bot_token) and bool(webhook_url) and not secret


def assert_webhook_secrets_configured(settings) -> None:
    """Startup-гейт: падаем на старте, если любой канал развёрнут в
    webhook-режиме (token+url) без webhook-secret.

    Вызывается из FastAPI lifespan ДО регистрации webhook. RuntimeError
    останавливает старт (fail-closed) — оператор обязан настроить secret
    прежде чем принимать внешний inbound.
    """
    problems: list[str] = []

    if webhook_secret_missing_while_deployed(
        bot_token=settings.max_bot_token,
        webhook_url=settings.max_webhook_url,
        secret=settings.max_webhook_secret_token,
    ):
        problems.append(
            "MAX: заданы max_bot_token+max_webhook_url, но "
            "SREDA_MAX_WEBHOOK_SECRET_TOKEN пуст — webhook принимал бы "
            "неаутентифицированный inbound (#341)"
        )

    if webhook_secret_missing_while_deployed(
        bot_token=settings.telegram_bot_token,
        webhook_url=settings.telegram_webhook_url,
        secret=settings.telegram_webhook_secret_token,
    ):
        problems.append(
            "Telegram: заданы telegram_bot_token+telegram_webhook_url, но "
            "SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN пуст — webhook принимал бы "
            "неаутентифицированный inbound (#341)"
        )

    if problems:
        raise RuntimeError(
            "Webhook secret misconfiguration (fail-closed startup gate): "
            + "; ".join(problems)
        )
