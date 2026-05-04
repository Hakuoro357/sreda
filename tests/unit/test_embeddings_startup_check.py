"""Lock-in tests for embeddings startup-check (2026-05-04 lesson).

Catя incident: SREDA_EMBEDDINGS_* потерялись в .env во время
SQLite→PG миграции, fallback на FakeEmbeddingClient(64d) тихо ломал
recall_memory для всех юзеров на 3-4 дня. Никаких алертов в логах,
ни в наблюдаемости — обнаружили только когда юзер жаловался.

После этого fix:
* allow_fake=True убран из production paths (graph.py, handlers.py)
* startup-check (`assert_embeddings_configured_or_alert`) шлёт alert
  в admin Telegram chat при проблеме
* alert_admin_async — best-effort delivery, любая ошибка swallowed
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from sreda.services import admin_alerts, embeddings


# ---------------------------------------------------------------------------
# alert_admin_async — best-effort delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_admin_no_chat_id_skips(monkeypatch, caplog):
    """admin_telegram_chat_id is None → return False, no exception."""
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "")
    # Force settings re-read — pydantic-settings caches per process
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING):
        result = await admin_alerts.alert_admin_async("test")

    assert result is False
    assert any("admin_telegram_chat_id not configured" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_alert_admin_no_bot_token_skips(monkeypatch, caplog):
    """telegram_bot_token is None → return False, no exception."""
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "352612382")
    monkeypatch.delenv("SREDA_TELEGRAM_BOT_TOKEN", raising=False)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING):
        result = await admin_alerts.alert_admin_async("test")

    assert result is False


@pytest.mark.asyncio
async def test_alert_admin_swallows_send_errors(monkeypatch, caplog):
    """TelegramClient.send_message raises → return False, log warning,
    NO exception."""
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "352612382")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "fake-token")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    fake_client_send = AsyncMock(side_effect=RuntimeError("network down"))
    with patch(
        "sreda.services.admin_alerts.TelegramClient",
    ) as mock_client_cls:
        mock_client_cls.return_value.send_message = fake_client_send
        with caplog.at_level(logging.WARNING):
            result = await admin_alerts.alert_admin_async("test")

    assert result is False
    assert any("failed to deliver" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_alert_admin_truncates_long_text(monkeypatch):
    """>4000 chars text → truncated с маркером."""
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "352612382")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "fake")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    fake_send = AsyncMock(return_value={"ok": True})
    with patch(
        "sreda.services.admin_alerts.TelegramClient",
    ) as mock_cls:
        mock_cls.return_value.send_message = fake_send
        big = "x" * 5000
        await admin_alerts.alert_admin_async(big)

    sent_text = fake_send.call_args.kwargs.get("text") or fake_send.call_args.args[1]
    # 3990 head + "\n…[truncated]" marker = ~4003. Должно влезть в TG limit 4096.
    assert len(sent_text) < 4096
    assert "[truncated]" in sent_text


# ---------------------------------------------------------------------------
# assert_embeddings_configured_or_alert — startup-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_check_alerts_on_disabled_client(monkeypatch, caplog):
    """Embeddings не настроены → DisabledEmbeddingClient → alert + log critical."""
    monkeypatch.delenv("SREDA_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("SREDA_EMBEDDINGS_MODEL", raising=False)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    fake_alert = AsyncMock(return_value=True)
    with patch(
        "sreda.services.admin_alerts.alert_admin_async", fake_alert
    ):
        with caplog.at_level(logging.CRITICAL):
            await embeddings.assert_embeddings_configured_or_alert()

    fake_alert.assert_called_once()
    msg = fake_alert.call_args.args[0]
    assert "🔴" in msg or "embeddings" in msg.lower()
    assert any("embeddings" in r.message.lower()
               for r in caplog.records if r.levelno >= logging.CRITICAL)


@pytest.mark.asyncio
async def test_startup_check_alerts_on_endpoint_failure(monkeypatch, caplog):
    """Endpoint сконфигурирован но не отвечает → alert (warning, not block)."""
    monkeypatch.setenv("SREDA_EMBEDDINGS_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("SREDA_EMBEDDINGS_MODEL", "fake-model")
    monkeypatch.setenv("SREDA_EMBEDDINGS_API_KEY", "fake-key")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    fake_alert = AsyncMock(return_value=True)
    # Подменяем embed_query чтобы кидало ошибку
    with patch(
        "sreda.services.admin_alerts.alert_admin_async", fake_alert
    ):
        with patch.object(
            embeddings.OpenAICompatEmbeddingClient,
            "embed_query",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.CRITICAL):
                await embeddings.assert_embeddings_configured_or_alert()

    fake_alert.assert_called_once()
    msg = fake_alert.call_args.args[0]
    assert "endpoint" in msg.lower() or "ping" in msg.lower() or "🟡" in msg


@pytest.mark.asyncio
async def test_startup_check_silent_on_healthy_endpoint(monkeypatch, caplog):
    """Embeddings работает → log info, alert НЕ вызывается."""
    monkeypatch.setenv("SREDA_EMBEDDINGS_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("SREDA_EMBEDDINGS_MODEL", "real-model")
    monkeypatch.setenv("SREDA_EMBEDDINGS_API_KEY", "real-key")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    fake_alert = AsyncMock(return_value=True)
    fake_vec = [0.1] * 1024
    with patch(
        "sreda.services.admin_alerts.alert_admin_async", fake_alert
    ):
        with patch.object(
            embeddings.OpenAICompatEmbeddingClient,
            "embed_query",
            return_value=fake_vec,
        ):
            with caplog.at_level(logging.INFO):
                await embeddings.assert_embeddings_configured_or_alert()

    fake_alert.assert_not_called()
    assert any(
        "embeddings OK" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]
