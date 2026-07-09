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
    # #238: стоимость считается ИЗ ТОКЕНОВ (priced Mercury/Groq → >0), не из мёртвого столбца
    assert by["inception-mercury2"]["cost_usd"] is not None
    assert by["inception-mercury2"]["cost_usd"] > 0
    assert by["groq-gpt-oss-120b"]["cost_usd"] > 0
    s.close()


def test_provider_spend_mercury_priced_unpriced_none_238(tmp_path):
    """#238: Mercury (основной провайдер) считается из токенов (не NULL); беспрайсовый
    провайдер → cost_usd=None (страница покажет «—», не выдуманные/нулевые деньги)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant

    engine = create_engine(f"sqlite:///{tmp_path / 'spend238.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t238", name="T"))
    s.commit()
    react_loop._record_react_usage(
        bind=engine, tenant_id="t238", provider_key="inception-mercury2",
        model="mercury-2", prompt_tokens=10000, completion_tokens=2000, run_id="m1")
    # беспрайсовый провайдер (нет в llm_pricing._PRICES)
    react_loop._record_react_usage(
        bind=engine, tenant_id="t238", provider_key="mimo",
        model="mimo-v2.5-pro", prompt_tokens=5000, completion_tokens=1000, run_id="x1")
    by = {row["provider"]: row for row in _provider_spend(s)}
    assert by["inception-mercury2"]["cost_usd"] is not None and by["inception-mercury2"]["cost_usd"] > 0
    assert by["mimo"]["cost_usd"] is None      # беспрайсовый → «—»
    s.close()


def test_provider_spend_excludes_malformed_tokens_238(tmp_path):
    """#238 R1 (Codex): malformed-строки (отрицательные токены; legacy 0/0 при total>0)
    НЕ попадают в токены/стоимость/вызовы провайдера (как get_spend_by_model, _VALID_TOKENS).
    Одна отрицательная строка НЕ должна отравить сумму группы (иначе cost → None)."""
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant
    from sreda.db.models.skill_platform import SkillAIExecution

    engine = create_engine(f"sqlite:///{tmp_path / 'mal238.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="tm238", name="T"))
    now = datetime.now(UTC)

    def _row(rid, pt, ct, tot=None):
        return SkillAIExecution(
            id=rid, run_id=rid, tenant_id="tm238", feature_key="f",
            provider_key="inception-mercury2", model="mercury-2", status="succeeded",
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tot, created_at=now)

    s.add_all([
        _row("ok", 1000, 200),          # валидная
        _row("neg", -500, -100),        # отрицательные → аномалия, исключить
        _row("legacy", 0, 0, 5000),     # 0/0 при total>0 → incomplete, исключить
    ])
    s.commit()
    m = {r["provider"]: r for r in _provider_spend(s)}["inception-mercury2"]
    assert m["calls"] == 1                                      # только валидная строка
    assert m["prompt_tokens"] == 1000 and m["completion_tokens"] == 200
    assert m["cost_usd"] is not None and m["cost_usd"] > 0      # не отравлено malformed
    s.close()


def test_provider_spend_empty_db_no_crash(db_session):
    """ч.3: пустая БД → [] (валидирует имена колонок запроса, без падения)."""
    assert _provider_spend(db_session) == []


def test_llm_context_exposes_react_info(db_session, monkeypatch):
    """ч.2: контекст /admin/llm содержит react_info (primary/Оса-fallback/Оса-тенанты)."""
    from types import SimpleNamespace

    monkeypatch.setenv("SREDA_REACT_OSA_FALLBACK", "1")
    st_mod.get_settings.cache_clear()
    try:
        # #305: _llm_context now takes a Request (for csrf_token) instead of a
        # raw token; csrf_token only reads request.cookies.
        req = SimpleNamespace(cookies={})
        ctx = _llm_context(db_session, req, with_balances=False)
        assert "react_info" in ctx and "react_spend" in ctx
        assert ctx["react_info"]["osa_fallback_on"] is True
        # R1-фикс: доступность запаса (флаг + ключ Groq) показывается ОТДЕЛЬНО от флага
        assert "osa_fallback_available" in ctx["react_info"]
        assert isinstance(ctx["react_info"]["osa_fallback_available"], bool)
        assert ctx["react_info"]["primary"]  # planner_provider не пуст
    finally:
        st_mod.get_settings.cache_clear()
