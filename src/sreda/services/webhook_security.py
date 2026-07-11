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

ЕДИНЫЙ набор предикатов (Codex R-codex MAJOR B): все call-site (роуты,
startup-гейт, health-воркер, регистрация) обязаны ветвиться на ЭТИ функции,
а НЕ на сырую truthiness полей — иначе пробельный secret/token обходит гейт
в одном месте и не обходит в другом (рассинхрон дискриминатора).
"""

from __future__ import annotations


def _norm(value: str | None) -> str:
    """Нормализует конфиг-значение: None/пробельное → ''."""
    return (value or "").strip()


def is_webhook_deployed(*, bot_token: str | None, webhook_url: str | None) -> bool:
    """True, если канал развёрнут в webhook-режиме: заданы (непробельно)
    ОБА bot_token и webhook_url. ЕДИНЫЙ дискриминатор «развёрнут» для всех
    call-site (роут/startup/health/регистрация)."""
    return bool(_norm(bot_token)) and bool(_norm(webhook_url))


def normalized_webhook_secret(secret: str | None) -> str:
    """Нормализованный secret ('' если None/пробельный). Пробельный secret
    трактуется как ОТСУТСТВУЮЩИЙ во ВСЕХ проверках/сравнениях."""
    return _norm(secret)


def is_webhook_secret_configured(secret: str | None) -> bool:
    """True, если secret реально задан (непробельный)."""
    return bool(normalized_webhook_secret(secret))


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

    Значения нормализуются (`.strip()`): пробельный secret не считается
    настроенным, пробельные token/url не считаются «развёрнуто».
    """
    return is_webhook_deployed(
        bot_token=bot_token, webhook_url=webhook_url
    ) and not is_webhook_secret_configured(secret)


def assert_webhook_secrets_configured(settings) -> None:
    """Startup-гейт: падаем на старте, если любой канал развёрнут в
    webhook-режиме (token+url) без webhook-secret.

    Вызывается из FastAPI lifespan ДО регистрации webhook. RuntimeError
    останавливает старт (fail-closed) — оператор обязан настроить secret
    прежде чем принимать внешний inbound. Сообщение стабильно (тесты
    матчат конкретный текст, не «любой RuntimeError»).
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
