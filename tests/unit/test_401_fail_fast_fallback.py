"""#401: fail-fast на резерв при 5xx основного LLM + под-тайминги в трассе.

Инцидент 20.07: Mercury (Фредди) вернул 500 → openai-клиент primary ретраил (default
max_retries=2, эксп. бэкофф) ~40с ПОД wall-clock 60с → поднял APIError → фолбэк Оса ~4с
= ~44с. Оса сама отвечает 3-5с. Владелец: «500 → сразу в фолбэк».

Ч1: react_primary_llm строит primary с max_retries=0, КОГДА доступен запас (Оса) — 5xx primary
    НЕ ретраит сам себя, а сразу уходит в фолбэк. Без запаса (флаг OFF / primary уже Оса) —
    дефолтный retry клиента сохранён (последний рубеж).
Ч2: llm_calls-трейс несёт РАЗДЕЛЬНЫЕ primary_latency_ms / fallback_latency_ms (не один агрегат
    latency_ms), чтобы следующий такой инцидент раскладывался точно (наблюдаемость под #396).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from sreda.config import settings as st_mod
from sreda.runtime import react_loop
from sreda.services import llm as llm_mod
from tests.unit.conftest import seed_telegram_user


# --- Ч1: fail-fast конструкция primary ----------------------------------

def _capture_get_chat_llm(monkeypatch):
    """Перехват kwargs, с которыми react_primary_llm зовёт get_chat_llm."""
    calls: list[dict] = []

    def _cap(*a, **k):
        calls.append(k)
        return object()  # заглушка клиента — саму конструкцию тут не проверяем

    monkeypatch.setattr(llm_mod, "get_chat_llm", _cap)
    return calls


def test_react_primary_fail_fast_when_fallback_available(monkeypatch):
    """Запас (Оса) доступен → primary строится с max_retries=0 (5xx сразу в фолбэк, без ретрая)."""
    monkeypatch.setenv("SREDA_REACT_OSA_FALLBACK", "1")
    st_mod.get_settings.cache_clear()
    calls = _capture_get_chat_llm(monkeypatch)
    try:
        react_loop.react_primary_llm("inception-mercury2")
    finally:
        st_mod.get_settings.cache_clear()
    assert calls, "get_chat_llm не вызван"
    assert calls[-1].get("max_retries") == 0, \
        f"primary при доступном запасе должен строиться с max_retries=0: {calls[-1]}"


def test_react_primary_keeps_retry_when_fallback_off(monkeypatch):
    """Флаг OFF (запаса нет) → retry primary НЕ трогаем (дефолтный клиентский retry сохранён)."""
    monkeypatch.delenv("SREDA_REACT_OSA_FALLBACK", raising=False)
    st_mod.get_settings.cache_clear()
    calls = _capture_get_chat_llm(monkeypatch)
    try:
        react_loop.react_primary_llm("inception-mercury2")
    finally:
        st_mod.get_settings.cache_clear()
    assert calls, "get_chat_llm не вызван"
    assert "max_retries" not in calls[-1], \
        f"без запаса retry primary трогать нельзя (последний рубеж): {calls[-1]}"


@pytest.mark.parametrize("primary", ["groq-gpt-oss-120b", "groq-gpt-oss-120b-low"])
def test_react_primary_keeps_retry_when_primary_already_osa(monkeypatch, primary):
    """primary уже Groq/Оса → запаса нет (Groq+Groq) → retry сохранён (max_retries не форсим)."""
    monkeypatch.setenv("SREDA_REACT_OSA_FALLBACK", "1")
    st_mod.get_settings.cache_clear()
    calls = _capture_get_chat_llm(monkeypatch)
    try:
        react_loop.react_primary_llm(primary)
    finally:
        st_mod.get_settings.cache_clear()
    assert calls, "get_chat_llm не вызван"
    assert "max_retries" not in calls[-1], \
        f"primary уже Оса — retry не трогаем: {calls[-1]}"


def test_max_retries_reaches_openai_client():
    """Пламбинг-гейт: max_retries=0 через get_chat_llm доходит до openai-клиента (конструкция, без сети).

    Не-RED (плумбинг верен и сегодня) — страхует, что рефактор get_chat_llm/_build_chat_llm,
    уронивший проброс **kwargs, будет пойман (фикс #401 на этом пробросе стоит)."""
    try:
        s = st_mod.Settings(mimo_api_key="test-key-not-real")
    except Exception:  # noqa: BLE001 — construct требует env
        pytest.skip("Settings construct requires env")
    client = llm_mod.get_chat_llm(provider="mimo", settings=s, max_retries=0)
    if client is None:
        pytest.skip("mimo provider not buildable in this env")
    assert client.root_client.max_retries == 0, \
        f"max_retries не дошёл до openai-клиента: {client.root_client.max_retries}"


# --- поведение: primary зовётся РОВНО один раз на 5xx (без двойного захода в цикле) ---

class _CountingRaisingPrimary:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ANN001
        def _raise(_m):
            self.calls += 1
            raise RuntimeError("primary 5xx")
        return RunnableLambda(_raise)


class _FallbackOK:
    def bind_tools(self, tools):  # noqa: ANN001
        return RunnableLambda(lambda _m: AIMessage(content="Ответ от Осы (fallback)."))


class _OKPrimary:
    def bind_tools(self, tools):  # noqa: ANN001
        return RunnableLambda(lambda _m: AIMessage(content="Ответ Фредди."))


@pytest.mark.asyncio
async def test_primary_invoked_once_on_5xx(db_session):
    """На 5xx primary цикл зовёт primary РОВНО один раз и уходит в фолбэк (без ретрая в цикле)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    primary = _CountingRaisingPrimary()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:once:{uuid4().hex}", llm=primary,
        fallback_llm=_FallbackOK(), user_text="привет",
        inbound_message_id="m-once", channel="telegram",
        provider_key="inception-mercury2")
    assert "Осы" in str(r), r
    assert primary.calls == 1, f"primary должен быть вызван РОВНО один раз: {primary.calls}"


# --- Ч2: под-тайминги в трассе ------------------------------------------

def _capture_trace(monkeypatch):
    """Включаем трейс и перехватываем llm_calls через persist_trace_finish; DB-пишущие
    trace-функции (start/abandoned зовут глобальный session-factory → реальный Postgres,
    которого в юнит-тестах нет) глушим в no-op."""
    captured: dict = {}

    def _cap(**kw):
        captured["llm_calls"] = kw.get("llm_calls")

    monkeypatch.setattr(react_loop._trace, "trace_enabled", lambda: True)
    monkeypatch.setattr(react_loop._trace, "persist_trace_start", lambda **k: None)
    monkeypatch.setattr(react_loop._trace, "persist_trace_abandoned", lambda **k: None)
    monkeypatch.setattr(react_loop._trace, "persist_trace_finish", _cap)
    monkeypatch.setattr(react_loop, "_emit_react_timeline", lambda *a, **k: None)
    return captured


def _chat_call(captured: dict) -> dict:
    lcs = captured.get("llm_calls") or []
    chat = [c for c in lcs if c.get("phase") == "chat"]
    assert chat, f"нет chat-записи в llm_calls: {lcs}"
    return chat[-1]


@pytest.mark.asyncio
async def test_trace_sub_timings_on_fallback(db_session, monkeypatch):
    """Фолбэк сработал → трасса несёт РАЗДЕЛЬНЫЕ primary_latency_ms и fallback_latency_ms."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    captured = _capture_trace(monkeypatch)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:st1:{uuid4().hex}", llm=_CountingRaisingPrimary(),
        fallback_llm=_FallbackOK(), user_text="привет",
        inbound_message_id="m-st1", channel="telegram",
        provider_key="inception-mercury2")
    c = _chat_call(captured)
    assert c.get("fallback_fired") is True, c
    assert isinstance(c.get("primary_latency_ms"), int), f"нет primary_latency_ms: {c}"
    assert isinstance(c.get("fallback_latency_ms"), int), f"нет fallback_latency_ms: {c}"
    assert isinstance(c.get("latency_ms"), int), f"нет итогового latency_ms: {c}"


@pytest.mark.asyncio
async def test_trace_primary_latency_on_success(db_session, monkeypatch):
    """primary ответил сам → есть primary_latency_ms, fallback_latency_ms отсутствует."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    captured = _capture_trace(monkeypatch)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:st2:{uuid4().hex}", llm=_OKPrimary(),
        fallback_llm=_FallbackOK(), user_text="привет",
        inbound_message_id="m-st2", channel="telegram",
        provider_key="inception-mercury2")
    c = _chat_call(captured)
    assert c.get("fallback_fired") is False, c
    assert isinstance(c.get("primary_latency_ms"), int), f"нет primary_latency_ms: {c}"
    assert c.get("fallback_latency_ms") is None, \
        f"на успехе fallback_latency_ms не должно быть: {c}"
