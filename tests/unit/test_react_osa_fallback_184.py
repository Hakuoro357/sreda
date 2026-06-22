"""#184: Оса (fallback_llm) как запас Фредди в ReAct — при сбое primary ход уходит на fallback.

RunnableLambda-стабы: chat-узел делает .with_fallbacks ПОСЛЕ bind_tools, а это метод Runnable
(плоский объект-стаб его не имеет) → bind_tools возвращает RunnableLambda.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from sreda.config import settings as st_mod
from sreda.runtime import react_loop
from tests.unit.conftest import seed_telegram_user


class _RaisingPrimary:
    def bind_tools(self, tools):  # noqa: ANN001
        def _raise(_messages):
            raise RuntimeError("primary 5xx")
        return RunnableLambda(_raise)


class _FallbackOK:
    def bind_tools(self, tools):  # noqa: ANN001
        return RunnableLambda(lambda _m: AIMessage(content="Ответ от Осы (fallback)."))


@pytest.mark.asyncio
async def test_react_fallback_used_when_primary_fails(db_session):
    """primary падает на invoke → LangGraph переходит на fallback_llm, ответ от него."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:fb:{uuid4().hex}", llm=_RaisingPrimary(),
        fallback_llm=_FallbackOK(), user_text="привет",
        inbound_message_id="m-fb", channel="telegram")
    assert "Осы" in str(r), r


@pytest.mark.asyncio
async def test_react_no_fallback_primary_raises_safe_reply(db_session):
    """Без fallback (None) primary-сбой → безопасный fallback-ответ handle_turn (не падает)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:nofb:{uuid4().hex}", llm=_RaisingPrimary(),
        fallback_llm=None, user_text="привет",
        inbound_message_id="m-nofb", channel="telegram")
    assert str(r)  # не пустой, ход не упал (handle_turn ловит исключение → safe reply)


def test_react_fallback_llm_gated_by_flag(monkeypatch):
    """Флаг SREDA_REACT_OSA_FALLBACK ВЫКЛ (дефолт) → react_fallback_llm() = None."""
    monkeypatch.delenv("SREDA_REACT_OSA_FALLBACK", raising=False)
    st_mod.get_settings.cache_clear()
    assert react_loop.react_fallback_llm() is None
    st_mod.get_settings.cache_clear()


def _u(p: int, c: int) -> dict:
    return {"input_tokens": p, "output_tokens": c, "total_tokens": p + c}


class _RaisingPrimaryUsage:
    def bind_tools(self, tools):  # noqa: ANN001
        def _raise(_m):
            raise RuntimeError("primary 5xx")
        return RunnableLambda(_raise)


class _FallbackUsage:
    def bind_tools(self, tools):  # noqa: ANN001
        return RunnableLambda(
            lambda _m: AIMessage(content="Оса.", usage_metadata=_u(80, 20)))


@pytest.mark.asyncio
async def test_react_fallback_records_usage_under_groq(db_session, monkeypatch):
    """R1-фикс (MAJOR): при срабатывании fallback расход пишется под groq-gpt-oss-120b,
    НЕ под primary (Mercury) — иначе таблица «расход по провайдерам» врёт."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    captured: list[dict] = []
    monkeypatch.setattr(react_loop, "_record_react_usage", lambda **kw: captured.append(kw))

    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:attr:{uuid4().hex}", llm=_RaisingPrimaryUsage(),
        fallback_llm=_FallbackUsage(), user_text="привет", inbound_message_id="m-attr",
        channel="telegram", provider_key="inception-mercury2")

    assert captured, "учёт не вызван"
    assert all(c["provider_key"] == "groq-gpt-oss-120b" for c in captured), \
        f"расход fallback атрибутирован неверно: {[c['provider_key'] for c in captured]}"
    assert captured[-1]["prompt_tokens"] == 80 and captured[-1]["completion_tokens"] == 20
    # R2 MINOR (Codex high): прайс зависит от (provider_key, model) — регресс модели тихо сломал бы
    # USD-оценку. Модель Осы резолвится через _GROQ_MODEL_BY_PROVIDER даже для opaque-стаба.
    assert captured[-1]["model"] == "openai/gpt-oss-120b", \
        f"модель fallback атрибутирована неверно: {captured[-1]['model']}"


@pytest.mark.parametrize("primary", ["groq-gpt-oss-120b", "groq-gpt-oss-120b-low"])
def test_react_fallback_skips_when_primary_already_osa(monkeypatch, primary):
    """R1/R2-фикс (MAJOR/MINOR): primary уже Groq/Оса (ЛЮБОЙ вариант, incl. -low) →
    react_fallback_llm = None (нет Groq+Groq)."""
    monkeypatch.setenv("SREDA_REACT_OSA_FALLBACK", "1")
    st_mod.get_settings.cache_clear()
    try:
        assert react_loop.react_fallback_llm(primary) is None
    finally:
        st_mod.get_settings.cache_clear()


def test_react_fallback_guarded_build(monkeypatch):
    """R1-фикс (MAJOR): флаг ВКЛ, но build LLM падает (мисконфиг Groq) → None, не валит вход."""
    monkeypatch.setenv("SREDA_REACT_OSA_FALLBACK", "1")
    st_mod.get_settings.cache_clear()
    from sreda.services import llm as _llm_mod

    def _boom(*a, **k):
        raise RuntimeError("groq misconfig")

    monkeypatch.setattr(_llm_mod, "get_chat_llm", _boom)
    try:
        assert react_loop.react_fallback_llm("inception-mercury2") is None
    finally:
        st_mod.get_settings.cache_clear()
