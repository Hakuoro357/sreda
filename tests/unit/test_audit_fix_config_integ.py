"""Регрессионные тесты фиксов аудита 2026-07-18 (slug: config-integ).

Покрытие:
- features/loader.py — сломанный feature-модуль: error-лог + admin-alert +
  skip, старт НЕ падает (deploy-ops MAJOR #3).
- features/registry.py:54 — register() бросает ValueError на дубликате
  feature_key (config-integ MINOR #6).
- config/bot_registry.py:186 — telegram_client_for fail-closed при пустом
  токене (config-integ MINOR #5).
- integrations/telegram/client.py:282 — HTTP 200 с не-JSON телом →
  TelegramDeliveryError с method/status_code (config-integ MINOR #4);
  keepalive-pinger — no-op shim (config-integ MINOR #1).
- integrations/max/client.py:473 — signed URL не попадает в цепочку
  __cause__/__context__ (config-integ MINOR #3).
- integrations/llm/openai_compatible.py — мёртвый модуль удалён
  (config-integ MINOR #2).

Без сети и без Postgres: весь HTTP замокан, БД не нужна.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sreda.config.bot_registry import (
    BotConfig,
    TelegramBotRegistry,
    telegram_client_for,
)
from sreda.features.loader import load_feature_modules
from sreda.features.registry import FeatureRegistry
from sreda.integrations.max.client import MaxClient, MaxDeliveryError
from sreda.integrations.telegram import client as tgclient
from sreda.integrations.telegram.client import (
    TelegramClient,
    TelegramDeliveryError,
    run_keepalive_pinger,
)


# ---------------------------------------------------------------------------
# loader: сломанный модуль → error-лог + admin-alert + skip (deploy-ops MAJOR)
# ---------------------------------------------------------------------------


@pytest.fixture()
def captured_alerts(monkeypatch):
    """Перехват send_admin_alert: best-effort алертинг не должен ходить
    в реальные HTTP/DB из unit-тестов."""
    calls: list[dict] = []

    def _fake_send(
        severity,
        title,
        body,
        *,
        dedupe_key=None,
        extra_context=None,
        both_channels=False,
    ):
        calls.append(
            {
                "severity": severity,
                "title": title,
                "body": body,
                "dedupe_key": dedupe_key,
            }
        )

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", _fake_send,
    )
    return calls


def test_loader_skips_unimportable_module_and_alerts(captured_alerts, caplog):
    registry = FeatureRegistry()
    with caplog.at_level(logging.ERROR, logger="sreda.features.loader"):
        load_feature_modules(["no_such_feature_module_xyz"], registry)

    assert registry.modules == {}, "сломанный модуль не должен регистрироваться"
    assert any("no_such_feature_module_xyz" in r.getMessage() for r in caplog.records)
    assert len(captured_alerts) == 1
    assert captured_alerts[0]["severity"] == "P1"
    assert captured_alerts[0]["dedupe_key"] == "feature_module_load:no_such_feature_module_xyz"


def test_loader_skips_module_whose_register_raises(monkeypatch, captured_alerts):
    broken = types.ModuleType("broken_feature_mod")

    def _register(_registry):
        raise RuntimeError("boom on import-time registration")

    broken.register = _register
    monkeypatch.setitem(sys.modules, "broken_feature_mod", broken)

    registry = FeatureRegistry()
    load_feature_modules(["broken_feature_mod"], registry)  # НЕ бросает

    assert registry.modules == {}
    assert [a["dedupe_key"] for a in captured_alerts] == [
        "feature_module_load:broken_feature_mod"
    ]


def test_loader_continues_after_broken_module(monkeypatch, captured_alerts):
    """Сломанный модуль не мешает загрузке следующих (skip, не падение рестарта)."""
    ok_mod = types.ModuleType("ok_feature_mod")
    ok_mod.feature_module = SimpleNamespace(feature_key="audit_test_feature_ok")
    monkeypatch.setitem(sys.modules, "ok_feature_mod", ok_mod)

    registry = FeatureRegistry()
    load_feature_modules(
        ["no_such_feature_module_xyz", "ok_feature_mod"], registry,
    )

    assert list(registry.modules) == ["audit_test_feature_ok"]
    assert len(captured_alerts) == 1


def test_loader_retired_feature_skipped_without_alert(monkeypatch, captured_alerts):
    """#181-гард живой: ретайренная фича тихо скипается ДО регистрации,
    без алерта (это штатный путь, не поломка)."""
    retired = types.ModuleType("retired_feature_mod")
    retired.feature_module = SimpleNamespace(feature_key="eds_monitor")
    monkeypatch.setitem(sys.modules, "retired_feature_mod", retired)

    registry = FeatureRegistry()
    load_feature_modules(["retired_feature_mod"], registry)

    assert registry.modules == {}
    assert captured_alerts == []


# ---------------------------------------------------------------------------
# registry: дубликат feature_key → ValueError (config-integ MINOR #6)
# ---------------------------------------------------------------------------


def test_registry_register_duplicate_feature_key_raises():
    registry = FeatureRegistry()
    registry.register(SimpleNamespace(feature_key="dup_feature"))
    with pytest.raises(ValueError, match="dup_feature"):
        registry.register(SimpleNamespace(feature_key="dup_feature"))


def test_registry_register_distinct_keys_ok():
    registry = FeatureRegistry()
    registry.register(SimpleNamespace(feature_key="feature_a"))
    registry.register(SimpleNamespace(feature_key="feature_b"))
    assert sorted(registry.modules) == ["feature_a", "feature_b"]


# ---------------------------------------------------------------------------
# bot_registry: fail-closed при пустом токене (config-integ MINOR #5)
# ---------------------------------------------------------------------------


def test_telegram_client_for_empty_token_raises():
    registry = TelegramBotRegistry([BotConfig(key="sreda", token="")])
    with pytest.raises(ValueError, match="empty token"):
        telegram_client_for("sreda", registry)


def test_from_settings_empty_token_keeps_sreda_resolvable(caplog):
    """MAX-only/dev инвариант: registry всё ещё «знает» sreda (routing-key
    validation, admin_alerts channel-probe), но пустой токен логируется."""
    settings = SimpleNamespace(
        telegram_bot_token=None,
        telegram_bot_username=None,
        telegram_miniapp_shortname=None,
        home_bot_token=None,
        home_bot_username=None,
        home_miniapp_shortname=None,
        home_bot_signup_open=True,
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )
    with caplog.at_level(logging.WARNING, logger="sreda.config.bot_registry"):
        registry = TelegramBotRegistry.from_settings(settings)
    assert registry.resolve("sreda").token == ""
    assert any("empty" in r.getMessage() for r in caplog.records)


def test_telegram_client_for_real_token_still_works():
    registry = TelegramBotRegistry([BotConfig(key="sreda", token="111:AAA")])
    client = telegram_client_for("sreda", registry)
    assert client.token == "111:AAA"


# ---------------------------------------------------------------------------
# telegram client: HTTP 200 с не-JSON телом (config-integ MINOR #4)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_pool_client(monkeypatch):
    """Тот же паттерн, что в test_telegram_client_retry.py: подменяем
    ``_get_pool_client`` мок-клиентом со срежиссированным ``.post()``."""
    mock_client = MagicMock()
    mock_client.post = AsyncMock()
    monkeypatch.setattr(tgclient, "_get_pool_client", lambda _token: mock_client)
    tgclient._CLIENT_POOL.clear()
    yield mock_client
    tgclient._CLIENT_POOL.clear()


@pytest.mark.asyncio
async def test_post_request_non_json_200_raises_delivery_error(mock_pool_client):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
    mock_pool_client.post.side_effect = [resp]

    client = TelegramClient(token="test-token")
    with pytest.raises(TelegramDeliveryError) as exc_info:
        await client.send_message("123", "hi")

    assert exc_info.value.method == "sendMessage"
    assert exc_info.value.status_code == 200
    assert mock_pool_client.post.call_count == 1, "не-JSON 200 не должен ретраиться"


@pytest.mark.asyncio
async def test_keepalive_pinger_is_retired_noop(caplog):
    """Пингер удалён по сути: no-op shim завершается сразу, без сети."""
    with caplog.at_level(logging.INFO, logger="sreda.integrations.telegram.client"):
        await asyncio.wait_for(
            run_keepalive_pinger("test-token", bot_key="sreda"), timeout=1.0,
        )
    assert any("retired" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# max client: signed URL не в цепочке исключения (config-integ MINOR #3)
# ---------------------------------------------------------------------------

_SIGNED_URL = "https://a.oneme.ru/audio?cid=1&signatureToken=zzz&expires=1"


class _FailingStreamCtx:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False


class _FakeHttpxClient:
    """Подмена httpx.AsyncClient для download_audio: ``stream()`` падает
    заданным транспортным исключением до любого сетевого I/O."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url):
        return _FailingStreamCtx(self._exc)


@pytest.mark.asyncio
async def test_download_audio_timeout_does_not_chain_signed_url(monkeypatch):
    fake = _FakeHttpxClient(httpx.TimeoutException("boom"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake)

    client = MaxClient(token="test-token")
    with pytest.raises(MaxDeliveryError) as exc_info:
        await client.download_audio(_SIGNED_URL)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_download_audio_network_error_does_not_chain_signed_url(monkeypatch):
    fake = _FakeHttpxClient(httpx.ConnectError("boom"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake)

    client = MaxClient(token="test-token")
    with pytest.raises(MaxDeliveryError) as exc_info:
        await client.download_audio(_SIGNED_URL)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# integrations/llm/openai_compatible.py удалён (config-integ MINOR #2)
# ---------------------------------------------------------------------------


def test_openai_compatible_module_removed():
    assert importlib.util.find_spec("sreda.integrations.llm.openai_compatible") is None
