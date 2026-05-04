"""Admin alerts — отправка критических нотификаций владельцу инстанса.

Используется при serious issues которые юзер не должен замечать (или
заметит как «бот не отвечает»), но владельцу инстанса (Boris) надо
узнать сразу же. Канал — `settings.admin_telegram_chat_id`, default =
личный чат Boris с ботом (352612382).

Прецедент: 2026-05-04 утром, embedding endpoint потерялся в .env во
время SQLite→PG миграции, fallback на FakeEmbeddingClient → recall
тихо вернул [] всем юзерам ~3-4 дня. Никаких алертов в логах не было,
обнаружили только когда юзер вручную пожаловался. Этот модуль —
страхующая «жёлтая кнопка» от подобного silent degradation.

Дизайн:
- ``alert_admin_async`` принимает текст, шлёт через TelegramClient
  best-effort. **Любая ошибка отправки swallowed** — нельзя чтобы
  альерт-фейл уронил приложение.
- Без admin_telegram_chat_id или telegram_bot_token — silent skip
  (только log critical), приложение продолжает работать.
- Sync-обёртки нет намеренно: оба call site (lifespan + poller) уже
  async-контексты. Если когда-то понадобится sync-вызов — добавить
  через `asyncio.to_thread` или `loop.run_until_complete`, не через
  голый `asyncio.run`.
"""

from __future__ import annotations

import logging

from sreda.config.settings import get_settings
from sreda.integrations.telegram.client import TelegramClient


logger = logging.getLogger(__name__)


async def alert_admin_async(text: str) -> bool:
    """Send a critical alert to the configured admin Telegram chat.

    Returns True if message was delivered, False otherwise. Failure
    never raises — alert system itself failing should not crash the
    main application.

    Text может быть многострочным; Telegram примет до 4096 chars.
    """
    settings = get_settings()
    chat_id = settings.admin_telegram_chat_id
    token = settings.telegram_bot_token

    if not chat_id:
        logger.warning("alert_admin: admin_telegram_chat_id not configured; skipping")
        return False
    if not token:
        logger.warning("alert_admin: telegram_bot_token not configured; skipping")
        return False

    # Truncate если безумно длинный — Telegram limit 4096 chars.
    if len(text) > 4000:
        text = text[:3990] + "\n…[truncated]"

    try:
        client = TelegramClient(token=token)
        await client.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "alert_admin: failed to deliver (chat=%s): %s",
            chat_id, exc,
        )
        return False


