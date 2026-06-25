"""#159 п.1 — устойчивость LLM-вызова в ReAct: wall-clock потолок на каждый вызов модели.

Дыра до фикса: узел chat звал модель сырым ``.invoke()`` — БЕЗ ограничения по времени.
Зависший/медленный primary (Mercury/deepseek) → ход висит (нет исключения → запас #184 не
срабатывает). Фикс: обернуть КАЖДЫЙ вызов модели (обе ветки — task и chat/fact, первичный
И запас) в ``invoke_with_per_call_timeout`` (та же обёртка, что у легаси-планировщика).
Срок берётся из настройки ``react_llm_timeout_sec`` (env ``SREDA_REACT_LLM_TIMEOUT_SEC``).

Стек надёжности: SDK-ретрай ChatOpenAI (блип) → wall-clock таймаут (зависание → запас) →
запас Оса/Фредди (жёсткий сбой) → safe-reply guard (всё упало).

Имена тестов СИНХРОНЫ с чеклистом приёмки #159 (ПРАВИЛО #7).

Зависание моделируем ``time.sleep`` в RunnableLambda: БЕЗ обёртки primary просто проспал бы и
вернул свой ответ (маркер «не должно дойти») → запас не сработал бы. С обёрткой (срок < сна)
→ LLMCallTimeout → запас. Поэтому присутствие/отсутствие маркера = фальсифицируемая проверка,
что обёртка реально в пути исполнения.
"""
from __future__ import annotations

import time
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from sreda.config import settings as st_mod
from sreda.runtime import react_loop
from tests.unit.conftest import seed_telegram_user

# Срок таймаута для sleep-тестов. Настройка react_llm_timeout_sec имеет ge=1.0 (прод-санити,
# как planner_timeout_sec) → минимально допустимое 1.0с; сон делаем заведомо больше.
_TIMEOUT_S = "1.0"
_SLEEP_S = 2.0


def _u(p: int, c: int) -> dict:
    return {"input_tokens": p, "output_tokens": c, "total_tokens": p + c}


class _HangingPrimary:
    """primary, чей invoke виснет дольше срока → должен сработать таймаут."""

    def __init__(self, *, hang: float = _SLEEP_S):
        self._hang = hang

    def bind_tools(self, tools):  # noqa: ANN001
        h = self._hang

        def _hang(_messages):
            time.sleep(h)
            return AIMessage(content="primary (не должно дойти)")
        return RunnableLambda(_hang)


class _FallbackOK:
    """Запас (Оса), отвечает мгновенно. usage_metadata для проверки атрибуции расхода."""

    def __init__(self, *, usage: dict | None = None):
        self._usage = usage

    def bind_tools(self, tools):  # noqa: ANN001
        usage = self._usage
        return RunnableLambda(
            lambda _m: AIMessage(content="Ответ от Осы (запас).", usage_metadata=usage))


class _FastPrimary:
    """primary, отвечает мгновенно (happy-path: обёртка не должна менять поведение)."""

    def bind_tools(self, tools):  # noqa: ANN001
        return RunnableLambda(lambda _m: AIMessage(content="primary-fast-ok"))


class _HangingFallback:
    """Запас, чей invoke виснет дольше срока → таймаут (проверка, что точка ЗАПАСА обёрнута)."""

    def __init__(self, *, hang: float = _SLEEP_S):
        self._hang = hang

    def bind_tools(self, tools):  # noqa: ANN001
        h = self._hang

        def _hang(_m):
            time.sleep(h)
            return AIMessage(content="fallback (не должно дойти)")
        return RunnableLambda(_hang)


class _CapTrace:
    """Фейк трейса (#192): включён, без сбора тулов, ловит llm_calls каждого persist_trace_finish."""

    def __init__(self):
        self.calls: list[dict] = []

    def trace_enabled(self):
        return True

    def collect_tool_calls(self, *a, **k):
        return []

    def persist_trace_finish(self, **kw):
        self.calls.append(kw)

    def __getattr__(self, _):  # прочие методы трейса (start и т.п.) — no-op
        return lambda *a, **k: None


# ───────────────────────── поведенческие (sleep) ─────────────────────────
@pytest.mark.asyncio
async def test_task_primary_timeout_falls_back_to_osa(db_session, monkeypatch):
    """task: primary завис дольше срока → таймаут → запас (Оса) ответил, ход завершён."""
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", _TIMEOUT_S)
    st_mod.get_settings.cache_clear()
    captured: list[dict] = []
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: captured.append(kw))
    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:hang:{uuid4().hex}", llm=_HangingPrimary(),
        fallback_llm=_FallbackOK(usage=_u(80, 20)), user_text="привет",
        inbound_message_id="m-hang", channel="telegram", provider_key="inception-mercury2")
    assert "Осы" in str(r), r
    assert "не должно дойти" not in str(r), r
    # запас сработал → расход атрибутирован Осе (groq), не Mercury
    assert captured and captured[-1]["provider_key"] == "groq-gpt-oss-120b", captured
    st_mod.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_no_fallback_timeout_yields_safe_reply(db_session, monkeypatch):
    """task без запаса: primary завис → таймаут → исключение во внешний guard → safe-reply.
    Ход НЕ висит вечно и НЕ возвращает контент зависшего primary."""
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", _TIMEOUT_S)
    st_mod.get_settings.cache_clear()
    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:nofb:{uuid4().hex}", llm=_HangingPrimary(),
        fallback_llm=None, user_text="привет",
        inbound_message_id="m-nofb", channel="telegram", provider_key="inception-mercury2")
    assert str(r), "пустой ответ — ход упал"
    assert "не должно дойти" not in str(r), r
    st_mod.get_settings.cache_clear()


# ───────────────────────── регрессия happy-path ─────────────────────────
@pytest.mark.asyncio
async def test_happy_path_primary_no_fallback(db_session, monkeypatch):
    """primary отвечает быстро → его ответ, запас НЕ срабатывает (обёртка прозрачна)."""
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", _TIMEOUT_S)
    st_mod.get_settings.cache_clear()
    captured: list[dict] = []
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: captured.append(kw))
    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:fast:{uuid4().hex}", llm=_FastPrimary(),
        fallback_llm=_FallbackOK(), user_text="привет",
        inbound_message_id="m-fast", channel="telegram", provider_key="inception-mercury2")
    assert "primary-fast-ok" in str(r), r
    assert "Осы" not in str(r), r
    # расход атрибутирован primary (Mercury), не Осе → запас не сработал
    assert captured and captured[-1]["provider_key"] == "inception-mercury2", captured
    st_mod.get_settings.cache_clear()


# ─────────── R2 (Codex): реальная обёртка на точках запаса + chat/fact + телеметрия ───────────
@pytest.mark.asyncio
async def test_task_fallback_also_hangs_yields_safe_reply(db_session, monkeypatch):
    """task: primary завис И запас завис → ОБА под таймаутом → второй LLMCallTimeout → guard safe-reply.
    Доказывает, что точка ЗАПАСА (Оса) тоже обёрнута: без обёртки запас проспал бы и вернул контент."""
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", _TIMEOUT_S)
    st_mod.get_settings.cache_clear()
    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:fbhang:{uuid4().hex}", llm=_HangingPrimary(),
        fallback_llm=_HangingFallback(), user_text="привет",
        inbound_message_id="m-fbhang", channel="telegram", provider_key="inception-mercury2")
    assert str(r), "пустой ответ — ход упал"
    assert "не должно дойти" not in str(r), r
    st_mod.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_chat_fact_primary_timeout_falls_back_to_freddie(db_session, monkeypatch):
    """chat/fact (preflight ON, intent=chat): deepseek завис (РЕАЛЬНАЯ обёртка) → таймаут →
    Фредди web-only ответил. Покрывает точку chat/fact-первичную и chat/fact-запас end-to-end."""
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_CHAT_PROVIDER", "openrouter-deepseek")
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", _TIMEOUT_S)
    st_mod.get_settings.cache_clear()
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: None)
    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: _HangingPrimary())  # deepseek виснет

    class _ClassifyChatThenFreddie:
        async def ainvoke(self, _m):
            return AIMessage(content="chat")

        def bind_tools(self, tools):  # noqa: ANN001 — chat/fact запас (Фредди web-only)
            return RunnableLambda(lambda _m: AIMessage(content="freddie-web-only-ответ"))

    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:cfto:{uuid4().hex}", llm=_ClassifyChatThenFreddie(),
        fallback_llm=None, user_text="кто такой Пушкин?",
        inbound_message_id="m-cfto", channel="telegram", provider_key="inception-mercury2")
    assert "freddie-web-only" in str(r), r
    st_mod.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_timeout_fallback_records_primary_attempt_in_trace(db_session, monkeypatch):
    """При срабатывании запаса по ТАЙМАУТУ наблюдательный трейс llm_calls фиксирует попытку primary
    (provider + ошибка LLMCallTimeout) — чтобы дашборд стоимости не выглядел так, будто primary
    не вызывался. Деньги (#175) при этом — на ответивший провайдер (токены primary неизвестны)."""
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", _TIMEOUT_S)
    st_mod.get_settings.cache_clear()
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: None)
    monkeypatch.setattr(react_loop, "_persist_debug_turn", lambda **kw: None)
    cap = _CapTrace()
    monkeypatch.setattr(react_loop, "_trace", cap)
    u = seed_telegram_user(db_session)
    db_session.commit()
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:tel:{uuid4().hex}", llm=_HangingPrimary(),
        fallback_llm=_FallbackOK(usage=_u(80, 20)), user_text="привет",
        inbound_message_id="m-tel", channel="telegram", provider_key="inception-mercury2")
    with_lcs = [c for c in cap.calls if c.get("llm_calls")]
    assert with_lcs, cap.calls
    fb = [e for e in with_lcs[-1]["llm_calls"] if e.get("fallback_fired")]
    assert fb, with_lcs[-1]["llm_calls"]
    assert fb[-1]["primary_provider_key"] == "inception-mercury2", fb
    assert "Timeout" in (fb[-1]["primary_error"] or ""), fb  # LLMCallTimeout
    st_mod.get_settings.cache_clear()


# ───────────────────────── проводка таймаута из настройки ─────────────────────────
@pytest.mark.asyncio
async def test_task_invoke_uses_settings_timeout(db_session, monkeypatch):
    """task-ветка зовёт invoke_with_per_call_timeout с timeout_seconds из react_llm_timeout_sec."""
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", "17")
    st_mod.get_settings.cache_clear()
    seen: list[float] = []

    def _spy(runnable, messages, *, timeout_seconds=60.0):
        seen.append(timeout_seconds)
        return AIMessage(content="spy-task-ok")  # без tool_calls → ход завершается

    monkeypatch.setattr(react_loop, "invoke_with_per_call_timeout", _spy)
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: None)
    u = seed_telegram_user(db_session)
    db_session.commit()
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:spy:{uuid4().hex}", llm=_FastPrimary(),
        fallback_llm=None, user_text="привет",
        inbound_message_id="m-spy", channel="telegram", provider_key="inception-mercury2")
    assert seen and 17.0 in seen, seen
    st_mod.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_chat_fact_invoke_uses_settings_timeout(db_session, monkeypatch):
    """chat/fact-ветка (preflight ON, intent=chat): первичный вызов deepseek тоже идёт через
    обёртку с тем же сроком из настройки."""
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_CHAT_PROVIDER", "openrouter-deepseek")
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", "23")
    st_mod.get_settings.cache_clear()
    seen: list[float] = []

    def _spy(runnable, messages, *, timeout_seconds=60.0):
        seen.append(timeout_seconds)
        return AIMessage(content="spy-chat-ok")  # без tool_calls → ход завершается

    monkeypatch.setattr(react_loop, "invoke_with_per_call_timeout", _spy)
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: None)

    class _ClassifyChat:
        """Фредди-классификатор: ainvoke → 'chat'; bind_tools → заглушка (не зовётся, spy замыкает)."""
        async def ainvoke(self, _m):
            return AIMessage(content="chat")

        def bind_tools(self, tools):  # noqa: ANN001
            return RunnableLambda(lambda _m: AIMessage(content="freddie-web-only"))

    class _DeepseekStub:
        def bind_tools(self, tools):  # noqa: ANN001
            return RunnableLambda(lambda _m: AIMessage(content="deepseek (не зовётся)"))

    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: _DeepseekStub())
    u = seed_telegram_user(db_session)
    db_session.commit()
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:cf:{uuid4().hex}", llm=_ClassifyChat(),
        fallback_llm=None, user_text="кто такой Пушкин?",
        inbound_message_id="m-cf", channel="telegram", provider_key="inception-mercury2")
    assert seen and 23.0 in seen, seen
    st_mod.get_settings.cache_clear()
