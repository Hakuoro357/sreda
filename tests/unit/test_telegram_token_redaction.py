"""Regression: Telegram bot tokens must never reach logs or tracebacks.

httpx's ``raise_for_status()`` embeds the request URL in its exception message,
and Telegram URLs carry the bot token (``bot<id>:<token>/...``). The client
wraps every failure in ``TelegramDeliveryError`` raised ``from None`` so the
token-bearing httpx exception never rides the ``__cause__`` / ``__context__``
chain into an upstream ``logger.exception()`` / ``exc_info=True``; the client's
own per-attempt warnings log only the exception class; and the admin-alert sync
path (raw httpx) logs only the class, never the traceback.

These tests check BOTH the raised exception (formatted traceback) AND the
captured log records, so reverting either ``from None``→``from exc`` or
``type(exc).__name__``→``exc`` fails them.
"""

from __future__ import annotations

import logging
import traceback

import httpx
import pytest

from sreda.integrations.telegram import client as tg_client
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError

_TOKEN = "8888888:AA_super_secret_token_DO_NOT_LOG"
_LEAKY_URL = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"


def _fake_pool_client_returning(status: int):
    """Return a `_get_pool_client` replacement whose POST/GET yields *status*.

    The Response carries a real Request with the token-bearing URL, so the
    client's ``response.raise_for_status()`` produces httpx's real
    URL-in-message ``HTTPStatusError`` — the actual leak source.
    """

    class _FakeClient:
        async def _resp(self, verb: str, url: str) -> httpx.Response:
            return httpx.Response(status, request=httpx.Request(verb, url), text="err")

        async def post(self, url, **kwargs):  # noqa: ANN001, ARG002
            return await self._resp("POST", _LEAKY_URL)

        async def get(self, url, **kwargs):  # noqa: ANN001, ARG002
            # download_file builds a token-bearing file URL; echo it back.
            return await self._resp("GET", url)

    return lambda token: _FakeClient()


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _caplog_blob(caplog) -> str:
    blob = ""
    for r in caplog.records:
        blob += r.getMessage() + "\n"
        if r.exc_info:  # logger.exception / exc_info=True would set this
            blob += "".join(traceback.format_exception(*r.exc_info))
    return blob


def _assert_no_token(blob: str) -> None:
    assert _TOKEN not in blob, "bot token leaked"
    assert "api.telegram.org/bot" not in blob, "token-bearing URL leaked"


@pytest.mark.asyncio
async def test_client_4xx_error_does_not_leak_token(monkeypatch, caplog):
    monkeypatch.setattr(
        tg_client, "_get_pool_client", _fake_pool_client_returning(400)
    )
    c = TelegramClient(_TOKEN)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TelegramDeliveryError) as ei:
            await c._post_request(
                "sendMessage", timeout=5.0, json={"chat_id": "1", "text": "x"}
            )
    _assert_no_token(_formatted(ei.value) + "\n" + str(ei.value) + "\n" + _caplog_blob(caplog))


@pytest.mark.asyncio
async def test_client_5xx_exhausted_does_not_leak_token(monkeypatch, caplog):
    monkeypatch.setattr(
        tg_client, "_get_pool_client", _fake_pool_client_returning(503)
    )
    c = TelegramClient(_TOKEN)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TelegramDeliveryError) as ei:
            await c._post_request(
                "sendMessage", timeout=5.0, json={"chat_id": "1", "text": "x"}
            )
    _assert_no_token(_formatted(ei.value) + "\n" + str(ei.value) + "\n" + _caplog_blob(caplog))


@pytest.mark.asyncio
async def test_download_file_does_not_leak_token(monkeypatch, caplog):
    monkeypatch.setattr(
        tg_client, "_get_pool_client", _fake_pool_client_returning(404)
    )
    c = TelegramClient(_TOKEN)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TelegramDeliveryError) as ei:
            await c.download_file("voice/file_123.oga")
    _assert_no_token(_formatted(ei.value) + "\n" + str(ei.value) + "\n" + _caplog_blob(caplog))


def test_admin_alerts_telegram_failure_logs_class_only(monkeypatch, caplog):
    from sreda.services import admin_alerts

    def _raise_with_url(*_a, **_k):
        raise httpx.ConnectError(
            "connect failed", request=httpx.Request("POST", _LEAKY_URL)
        )

    monkeypatch.setattr(admin_alerts.httpx, "post", _raise_with_url)
    with caplog.at_level(logging.WARNING):
        ok = admin_alerts._post_telegram_sync(_TOKEN, "123", "hi")
    assert ok is False
    blob = _caplog_blob(caplog)
    _assert_no_token(blob)
    assert "ConnectError" in blob  # sanitized class-name signal is logged
