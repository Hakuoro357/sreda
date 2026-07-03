# -*- coding: utf-8 -*-
"""STT Yandex: клиент строится с trust_env=False (RU-сервис идёт напрямую, не через NL-туннель).

Замер 2026-07-03: Yandex STT (российский, VDS в Москве) уходил в глобальный HTTPS_PROXY=:1080 (крюк
RU→NL→RU, 10× латентность, спайки/30с-отказы), т.к. httpx.AsyncClient без trust_env=False читает env.
Тест пинит паттерн RU-direct (как MAX-клиент/admin_alerts)."""
from __future__ import annotations

import asyncio

import httpx

from sreda.services.speech.yandex import YandexSpeechKitRecognizer


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"result": "привет мир"}


class _FakeClient:
    captured: dict = {}

    def __init__(self, **kwargs):
        _FakeClient.captured = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _Resp()


def test_yandex_stt_client_trust_env_false(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    rec = YandexSpeechKitRecognizer(api_key="k")
    out = asyncio.run(rec.recognize(b"audio-bytes"))
    assert out == "привет мир"
    # RU-сервис НЕ через env-прокси (иначе крюк через зарубежный туннель → спайки STT)
    assert _FakeClient.captured.get("trust_env") is False
