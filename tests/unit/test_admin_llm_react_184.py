"""#184 ч.2-3: страница /admin/llm показывает react-механизм (ENV) + расход по провайдерам.

Корень: /admin/llm = легаси chat-свитчер + балансы (только openrouter/mimo). react-модель
(planner_provider + Оса-fallback) управляется ENV и была НЕВИДИМА на странице; у Inception/Groq
нет API баланса → расход не показывался. Этот файл фиксирует контракт новых полей контекста.
"""
from __future__ import annotations

from sreda.admin.routes import _llm_context, _provider_spend
from sreda.config import settings as st_mod
from sreda.runtime import react_loop


def test_provider_spend_groups_by_provider(tmp_path):
    """ч.3: расход агрегируется по provider_key (вызовы + токены) из skill_ai_executions."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant

    engine = create_engine(f"sqlite:///{tmp_path / 'spend.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="tsp", name="T"))
    s.commit()

    # 2 вызова Фредди (Mercury) + 1 Осы (Groq) — разные провайдеры
    react_loop._record_react_usage(
        bind=engine, tenant_id="tsp", provider_key="inception-mercury2",
        model="mercury-2", prompt_tokens=100, completion_tokens=20, run_id="r1")
    react_loop._record_react_usage(
        bind=engine, tenant_id="tsp", provider_key="inception-mercury2",
        model="mercury-2", prompt_tokens=50, completion_tokens=10, run_id="r2")
    react_loop._record_react_usage(
        bind=engine, tenant_id="tsp", provider_key="groq-gpt-oss-120b",
        model="openai/gpt-oss-120b", prompt_tokens=200, completion_tokens=40, run_id="r3")

    by = {row["provider"]: row for row in _provider_spend(s)}
    assert by["inception-mercury2"]["calls"] == 2
    assert by["inception-mercury2"]["prompt_tokens"] == 150
    assert by["inception-mercury2"]["completion_tokens"] == 30
    assert by["groq-gpt-oss-120b"]["calls"] == 1
    assert by["groq-gpt-oss-120b"]["prompt_tokens"] == 200
    s.close()


def test_provider_spend_empty_db_no_crash(db_session):
    """ч.3: пустая БД → [] (валидирует имена колонок запроса, без падения)."""
    assert _provider_spend(db_session) == []


def test_llm_context_exposes_react_info(db_session, monkeypatch):
    """ч.2: контекст /admin/llm содержит react_info (primary/Оса-fallback/Оса-тенанты)."""
    monkeypatch.setenv("SREDA_REACT_OSA_FALLBACK", "1")
    st_mod.get_settings.cache_clear()
    try:
        ctx = _llm_context(db_session, "tok", with_balances=False)
        assert "react_info" in ctx and "react_spend" in ctx
        assert ctx["react_info"]["osa_fallback_on"] is True
        # R1-фикс: доступность запаса (флаг + ключ Groq) показывается ОТДЕЛЬНО от флага
        assert "osa_fallback_available" in ctx["react_info"]
        assert isinstance(ctx["react_info"]["osa_fallback_available"], bool)
        assert ctx["react_info"]["primary"]  # planner_provider не пуст
    finally:
        st_mod.get_settings.cache_clear()
