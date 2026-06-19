"""#175 (хвост #150/#151): ReAct-цикл пишет usage планировщика в skill_ai_executions.

Корень: запись расхода (`record_llm_usage`) звали только legacy-пути (handlers, planner_chat,
STT). `react_loop.handle_turn` НЕ писал ничего → деньги/админка (#150) слепы к ReAct-расходу.
Этот файл фиксирует контракт #151 для НОВОГО пути:
- хелпер `_record_react_usage` пишет наблюдательную строку (provider+токены, credits=0);
- chat-узел зовёт его на КАЖДЫЙ вызов LLM (multi-pass → несколько строк);
- сбой записи НЕ валит ход (guarded).

КРАСНЫЙ ДО реализации (#175): нет `_record_react_usage`, нет параметра provider_key у handle_turn.
"""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessage

from sreda.runtime import react_loop
from tests.unit.conftest import seed_telegram_user
from tests.unit.test_react_lazy_families_165 import _RecordingStubLLM


def _u(p: int, c: int) -> dict:
    return {"input_tokens": p, "output_tokens": c, "total_tokens": p + c}


# --------------------------------------------------------------- A: хелпер пишет строку

def test_record_react_usage_writes_observational_row(tmp_path):
    """A: хелпер пишет ОДНУ строку skill_ai_executions с provider+токенами, credits=0
    (наблюдательная: для USD-страниц, без влияния на кредит-квоту — Mercury не MiMo)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant
    from sreda.db.models.skill_platform import SkillAIExecution

    # Файловый SQLite: хелпер пишет в ОТДЕЛЬНОЙ session на том же engine (новое
    # соединение); файл общий → строки видны тестовой session (как в тесте #151).
    engine = create_engine(f"sqlite:///{tmp_path / 'u175.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t175", name="T"))
    s.commit()

    react_loop._record_react_usage(
        bind=engine, tenant_id="t175", provider_key="inception-mercury2",
        model="mercury-2", prompt_tokens=120, completion_tokens=40, run_id="r1")

    rows = s.query(SkillAIExecution).filter(SkillAIExecution.tenant_id == "t175").all()
    assert len(rows) == 1
    r = rows[0]
    assert r.provider_key == "inception-mercury2"
    assert r.prompt_tokens == 120
    assert r.completion_tokens == 40
    assert r.total_tokens == 160
    assert r.credits_consumed == 0  # наблюдательная строка (credits_override=0)
    assert r.feature_key == "housewife_assistant"
    s.close()


def test_record_react_usage_skips_zero_tokens(tmp_path):
    """A2: нулевые токены (провайдер не отдал usage) → строку НЕ пишем (мусор не копим)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant
    from sreda.db.models.skill_platform import SkillAIExecution

    engine = create_engine(f"sqlite:///{tmp_path / 'u175z.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t175", name="T"))
    s.commit()

    react_loop._record_react_usage(
        bind=engine, tenant_id="t175", provider_key="inception-mercury2",
        model="mercury-2", prompt_tokens=0, completion_tokens=0, run_id="r1")

    assert s.query(SkillAIExecution).count() == 0
    s.close()


def test_record_react_usage_guarded_never_raises():
    """A3: битый bind → хелпер НЕ поднимает исключение (учёт не валит ход)."""
    react_loop._record_react_usage(
        bind=None, tenant_id="t", provider_key="p", model="m",
        prompt_tokens=5, completion_tokens=5, run_id="r")  # не должно бросить


# --------------------------------------------------- B: chat-узел зовёт хелпер на каждый вызов

@pytest.mark.asyncio
async def test_react_records_usage_per_llm_call(db_session, monkeypatch):
    """B: ход ReAct с 2 проходами LLM → хелпер вызван 2 раза с извлечёнными токенами и
    provider_key/tenant_id хода (по строке на каждый реальный вызов LLM)."""
    u = seed_telegram_user(db_session)
    db_session.commit()

    captured: list[dict] = []
    monkeypatch.setattr(react_loop, "_record_react_usage",
                        lambda **kw: captured.append(kw))

    scripted = [
        AIMessage(content="", tool_calls=[{"name": "list_tasks", "args": {}, "id": "lt"}],
                  usage_metadata=_u(100, 20)),
        AIMessage(content="Готово.", usage_metadata=_u(150, 30)),
    ]
    stub = _RecordingStubLLM(scripted)

    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="u175-b", llm=stub, user_text="покажи задачи",
        inbound_message_id="u175-b-msg", channel="react",
        provider_key="inception-mercury2")

    assert len(captured) == 2, f"ожидали 2 записи (2 прохода LLM), получили {len(captured)}"
    assert captured[0]["prompt_tokens"] == 100 and captured[0]["completion_tokens"] == 20
    assert captured[1]["prompt_tokens"] == 150 and captured[1]["completion_tokens"] == 30
    assert all(c["provider_key"] == "inception-mercury2" for c in captured)
    assert all(c["tenant_id"] == u.tenant_id for c in captured)
    # модель резолвится в каноничную «mercury-2» (карта inception-провайдера) → ключ
    # (provider_key, model) совпадёт с прайс-таблицей llm_pricing → USD на дашборде/бюджете.
    assert all(c["model"] == "mercury-2" for c in captured), \
        f"модель не резолвилась в mercury-2: {[c['model'] for c in captured]}"


@pytest.mark.asyncio
async def test_react_usage_provider_key_fallback_to_planner_provider(db_session, monkeypatch):
    """#175 подстраховка: call-site НЕ передал provider_key → handle_turn берёт planner_provider
    (иначе пропущенный call-site тихо терял бы учёт — реальный прецедент с telegram_inbound)."""
    from sreda.config.settings import get_settings

    u = seed_telegram_user(db_session)
    db_session.commit()
    captured: list[dict] = []
    monkeypatch.setattr(react_loop, "_record_react_usage",
                        lambda **kw: captured.append(kw))

    stub = _RecordingStubLLM([AIMessage(content="Привет!", usage_metadata=_u(10, 5))])
    await react_loop.handle_turn(  # provider_key НЕ передан
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="u175-fb", llm=stub, user_text="привет",
        inbound_message_id="u175-fb-msg", channel="react")

    assert captured, "запись не вызвана — подстраховка provider_key не сработала"
    expected = get_settings().planner_provider
    assert expected, "planner_provider пуст в тест-настройках — нечего проверять"
    assert all(c["provider_key"] == expected for c in captured)


@pytest.mark.asyncio
async def test_react_usage_failure_does_not_break_turn(db_session, monkeypatch):
    """C: сбой записи usage НЕ валит ход — пользователь получает нормальный ответ."""
    u = seed_telegram_user(db_session)
    db_session.commit()

    def _boom(**kw):
        raise RuntimeError("accounting db down")

    monkeypatch.setattr(react_loop, "_record_react_usage", _boom)

    stub = _RecordingStubLLM([AIMessage(content="Привет!", usage_metadata=_u(10, 5))])
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="u175-c", llm=stub, user_text="привет",
        inbound_message_id="u175-c-msg", channel="react",
        provider_key="inception-mercury2")

    # ход НЕ упал в fallback «потеряла контекст»
    assert "Привет" in str(reply)


# --------------------------------------------------- D: честное пустое состояние бюджета (Fix 2)

def test_has_active_subscriptions_true_false(db_session):
    """D: helper различает «есть активные подписки» vs «нет» — основа честного пустого
    состояния (`/admin/budget`: «Нет расхода за этот день» вместо ложного «Нет активных подписок»)."""
    from datetime import datetime, timedelta, timezone

    from sreda.admin.queries import has_active_subscriptions
    from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
    from sreda.db.models.core import Tenant

    assert has_active_subscriptions(db_session) is False  # пусто

    _now = datetime.now(timezone.utc)
    db_session.add(Tenant(id="td", name="TD"))
    db_session.add(SubscriptionPlan(
        id="plan_d", plan_key="hw_base", feature_key="housewife_assistant",
        title="Free", description="", price_rub=0, credits_monthly_quota=1000))
    db_session.add(TenantSubscription(
        id="sub_d", tenant_id="td", plan_id="plan_d", feature_key="housewife_assistant",
        status="active", starts_at=_now - timedelta(days=1),
        active_until=_now + timedelta(days=30)))
    db_session.commit()

    assert has_active_subscriptions(db_session) is True
