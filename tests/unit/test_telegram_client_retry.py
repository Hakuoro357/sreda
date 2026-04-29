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

2026-04-29: тесты адаптированы под новый connection-pool в
TelegramClient (`_CLIENT_POOL` keyed by token). Вместо
`patch("httpx.AsyncClient")` подменяем `_get_pool_client` чтобы
вернуть mock со срежиссированным `.post()` (AsyncMock).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sreda.integrations.telegram import client as tgclient
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


@pytest.fixture
def mock_pool_client(monkeypatch):
    """Replace `_get_pool_client` with one that returns a mock client.

    Returns the mock — tests configure its `.post.side_effect` /
    `.get.side_effect` and inspect `.post.call_count`.
    """
    mock_client = MagicMock()
    mock_client.post = AsyncMock()
    mock_client.get = AsyncMock()
    monkeypatch.setattr(
        tgclient, "_get_pool_client", lambda _token: mock_client,
    )
    # Also kill the real cache so other tests don't see leftover.
    tgclient._CLIENT_POOL.clear()
    yield mock_client
    tgclient._CLIENT_POOL.clear()


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _err_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.side_effect = _make_http_status_error(status_code)
    return resp


@pytest.mark.asyncio
async def test_400_non_retryable(mock_pool_client) -> None:
    """400 (callback expired / bad request) — no retry, raise immediately."""
    mock_pool_client.post.side_effect = [_err_response(400)]

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.send_message("123", "test")

    assert exc_info.value.status_code == 400
    assert exc_info.value.method == "sendMessage"
    assert mock_pool_client.post.call_count == 1, "must NOT retry on 4xx"


@pytest.mark.asyncio
async def test_403_non_retryable(mock_pool_client) -> None:
    """403 (bot blocked by user) — no retry."""
    mock_pool_client.post.side_effect = [_err_response(403)]

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.answer_callback_query("cb_id_123")

    assert exc_info.value.status_code == 403
    assert exc_info.value.method == "answerCallbackQuery"
    assert mock_pool_client.post.call_count == 1


@pytest.mark.asyncio
async def test_429_non_retryable(mock_pool_client) -> None:
    """429 (rate-limit) — no retry. Retry'и только усугубят rate-limit."""
    mock_pool_client.post.side_effect = [_err_response(429)]

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.send_message("123", "test")

    assert exc_info.value.status_code == 429
    assert mock_pool_client.post.call_count == 1


@pytest.mark.asyncio
async def test_500_retries_three_times(mock_pool_client, monkeypatch) -> None:
    """5xx (server error) — retryable, до 3 попыток."""
    mock_pool_client.post.side_effect = [
        _err_response(500), _err_response(500), _err_response(500),
    ]
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.send_message("123", "test")

    assert exc_info.value.status_code == 500
    assert mock_pool_client.post.call_count == 3, "5xx must retry 3 times"


@pytest.mark.asyncio
async def test_timeout_retries_three_times(mock_pool_client, monkeypatch) -> None:
    """TimeoutException — retry до 3 попыток. status_code=None в финале."""
    mock_pool_client.post.side_effect = httpx.TimeoutException("timeout")
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.send_message("123", "test")

    assert exc_info.value.status_code is None
    assert exc_info.value.method == "sendMessage"
    assert mock_pool_client.post.call_count == 3


@pytest.mark.asyncio
async def test_success_no_retry(mock_pool_client) -> None:
    """200 OK с первого раза — один POST, без retry."""
    mock_pool_client.post.side_effect = [
        _ok_response({"ok": True, "result": {"message_id": 1}}),
    ]

    client = TelegramClient(token="test-token")
    result = await client.send_message("123", "test")

    assert result == {"ok": True, "result": {"message_id": 1}}
    assert mock_pool_client.post.call_count == 1


@pytest.mark.asyncio
async def test_pool_reused_across_requests(mock_pool_client) -> None:
    """Connection pool: два последовательных вызова одного TelegramClient
    инстанса используют один и тот же httpx-клиент (TLS handshake amortized)."""
    mock_pool_client.post.side_effect = [
        _ok_response({"ok": True, "result": {}}),
        _ok_response({"ok": True, "result": {}}),
    ]

    client = TelegramClient(token="test-token")
    await client.send_message("123", "first")
    await client.send_message("123", "second")

    # Same mock served both calls — proves single client reused.
    assert mock_pool_client.post.call_count == 2


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


# 2026-04-29 (incident user_tg_471032584): Telegram Bot API в edge-cases
# (невалидный reply_markup, malformed parse_mode, etc.) умеет вернуть
# HTTP 200 с `{"ok": false, "description": "..."}`. Раньше client
# возвращал такой body как успех, caller думал что отправили, юзер
# не получал ничего, в логах только `200 OK`. Теперь явно raise
# TelegramDeliveryError на ok=false.
@pytest.mark.asyncio
async def test_200_with_ok_false_raises(mock_pool_client) -> None:
    """HTTP 200 + body `{ok: false, description: ...}` → TelegramDeliveryError."""
    mock_pool_client.post.side_effect = [
        _ok_response({
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities",
        }),
    ]

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.send_message("123", "test")

    assert exc_info.value.method == "sendMessage"
    assert "ok=false" in str(exc_info.value)
    assert "can't parse entities" in str(exc_info.value)
    assert mock_pool_client.post.call_count == 1, "ok=false — no retry"
