"""Тесты non-retryable 4xx behaviour в TelegramClient (2026-04-28).

Spam-loop fix tg_1089832184: 361 callback retry на одну `pb:schedule`
кнопку. Корень — `_post_request` retry'ил 4xx (включая 400 expired
callback и 429 rate-limit), что только усугубляло loop. Теперь любой
4xx — fail-fast с TelegramDeliveryError(method, status_code).

Тесты:
- 400 → no retry, выбрасывается TelegramDeliveryError(status_code=400)
- 403 → no retry (bot blocked)
- 429 → no retry (rate-limit)
- 5xx → retry до 3 попыток
- timeout → retry до 3 попыток
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sreda.integrations.telegram.client import (
    TelegramClient,
    TelegramDeliveryError,
)


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Helper для конструирования HTTPStatusError с заданным status."""
    request = httpx.Request("POST", "https://api.telegram.org/test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


@pytest.mark.asyncio
async def test_400_non_retryable() -> None:
    """400 (callback expired / bad request) — no retry, raise immediately."""
    client = TelegramClient(token="test-token")

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = _make_http_status_error(400)
    post_call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal post_call_count
        post_call_count += 1
        return mock_response

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.post = mock_post
        mock_async_client.return_value.__aenter__.return_value = mock_instance

        with pytest.raises(TelegramDeliveryError) as exc_info:
            await client.send_message("123", "test")

    assert exc_info.value.status_code == 400
    assert exc_info.value.method == "sendMessage"
    assert post_call_count == 1, "must NOT retry on 4xx"


@pytest.mark.asyncio
async def test_403_non_retryable() -> None:
    """403 (bot blocked by user) — no retry."""
    client = TelegramClient(token="test-token")

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = _make_http_status_error(403)
    post_call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal post_call_count
        post_call_count += 1
        return mock_response

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.post = mock_post
        mock_async_client.return_value.__aenter__.return_value = mock_instance

        with pytest.raises(TelegramDeliveryError) as exc_info:
            await client.answer_callback_query("cb_id_123")

    assert exc_info.value.status_code == 403
    assert exc_info.value.method == "answerCallbackQuery"
    assert post_call_count == 1


@pytest.mark.asyncio
async def test_429_non_retryable() -> None:
    """429 (rate-limit) — no retry. Retry'и только усугубят rate-limit."""
    client = TelegramClient(token="test-token")

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = _make_http_status_error(429)
    post_call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal post_call_count
        post_call_count += 1
        return mock_response

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.post = mock_post
        mock_async_client.return_value.__aenter__.return_value = mock_instance

        with pytest.raises(TelegramDeliveryError) as exc_info:
            await client.send_message("123", "test")

    assert exc_info.value.status_code == 429
    assert post_call_count == 1


@pytest.mark.asyncio
async def test_500_retries_three_times() -> None:
    """5xx (server error) — retryable, до 3 попыток. После исчерпания
    raise TelegramDeliveryError."""
    client = TelegramClient(token="test-token")

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = _make_http_status_error(500)
    post_call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal post_call_count
        post_call_count += 1
        return mock_response

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.post = mock_post
        mock_async_client.return_value.__aenter__.return_value = mock_instance

        # asyncio.sleep делаем no-op чтобы тест не ждал 0.5+1.0 секунды
        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(TelegramDeliveryError) as exc_info:
                await client.send_message("123", "test")

    assert exc_info.value.status_code == 500
    assert post_call_count == 3, "5xx must retry 3 times"


@pytest.mark.asyncio
async def test_timeout_retries_three_times() -> None:
    """TimeoutException — retry до 3 попыток. status_code=None в финале."""
    client = TelegramClient(token="test-token")
    post_call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal post_call_count
        post_call_count += 1
        raise httpx.TimeoutException("timeout")

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.post = mock_post
        mock_async_client.return_value.__aenter__.return_value = mock_instance

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(TelegramDeliveryError) as exc_info:
                await client.send_message("123", "test")

    assert exc_info.value.status_code is None
    assert exc_info.value.method == "sendMessage"
    assert post_call_count == 3


@pytest.mark.asyncio
async def test_success_no_retry() -> None:
    """200 OK с первого раза — один POST, без retry."""
    client = TelegramClient(token="test-token")

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
    post_call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal post_call_count
        post_call_count += 1
        return mock_response

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_instance = AsyncMock()
        mock_instance.post = mock_post
        mock_async_client.return_value.__aenter__.return_value = mock_instance

        result = await client.send_message("123", "test")

    assert result == {"ok": True, "result": {"message_id": 1}}
    assert post_call_count == 1


def test_telegram_delivery_error_carries_method_and_status() -> None:
    """TelegramDeliveryError exposes .method и .status_code для caller-side
    routing (например `_handle_callback` различает 400 expired vs 5xx)."""
    err = TelegramDeliveryError(
        "test message", method="answerCallbackQuery", status_code=400,
    )
    assert err.method == "answerCallbackQuery"
    assert err.status_code == 400
    assert str(err) == "test message"


def test_telegram_delivery_error_defaults_to_none() -> None:
    """Backwards-compat: legacy raise без kwargs не падает."""
    err = TelegramDeliveryError("legacy")
    assert err.method is None
    assert err.status_code is None
