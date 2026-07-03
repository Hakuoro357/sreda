"""Unit tests for the admin overview snapshot (#292).

Covers the acceptance-checklist machine items:
- spend math goes through llm_pricing: priced pair → $ from confirmed
  price; unpriced pair → priced=False and NO invented dollars;
- error/slow aggregates count the right statuses/thresholds;
- snapshot roundtrip (store → load) and fail-soft behaviours.

No network: provider balances are monkeypatched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.admin import overview_snapshot as ov
from sreda.db.base import Base
from sreda.db.models.skill_platform import SkillAIExecution

# Оригинал до autouse-мока (см. test_balances_failsoft).
_REAL_BALANCES_BLOCK = ov._balances_block


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def session(session_factory):
    s = session_factory()
    yield s
    s.close()


_SEQ = iter(range(10_000))


def _exec_row(
    *,
    status: str = "succeeded",
    provider_key: str | None = "inception-mercury2",
    model: str | None = "mercury-2",
    prompt_tokens: int = 1000,
    completion_tokens: int = 100,
    latency_ms: int = 500,
    age_hours: float = 1.0,
    error_code: str | None = None,
    task_type: str = "chat",
) -> SkillAIExecution:
    n = next(_SEQ)
    return SkillAIExecution(
        id=f"ex_{n}",
        run_id=f"run_{n}",
        tenant_id="tenant_test",
        feature_key="housewife_assistant",
        task_type=task_type,
        provider_key=provider_key,
        model=model,
        status=status,
        error_code=error_code,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        latency_ms=latency_ms,
        created_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )


@pytest.fixture()
def fake_settings():
    return SimpleNamespace(chat_provider="mimo", chat_fallback_provider="")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Balances must never hit the network in unit tests."""
    monkeypatch.setattr(
        ov, "_balances_block",
        lambda settings: [{"key": "openrouter", "label": "OpenRouter",
                           "status": "ok", "headline": "$12.40", "details": ""}],
    )


def test_llm_24h_counts_errors(session, fake_settings):
    session.add_all([
        _exec_row(),                                   # ok
        _exec_row(status="failed", error_code="timeout"),
        _exec_row(status="validation_failed"),
        _exec_row(age_hours=30),                       # вне окна 24ч
    ])
    session.commit()
    payload = ov.compute_overview(session, fake_settings)
    blk = payload["llm_24h"]
    assert blk["calls"] == 3          # строка age=30h не в окне
    assert blk["errors"] == 2
    assert "slow" not in blk          # медленные теперь из трейсов (slow_turns)


def test_errors_list_shape(session, fake_settings):
    session.add(_exec_row(status="failed", error_code="timeout", model="mercury-2"))
    session.commit()
    payload = ov.compute_overview(session, fake_settings)
    err = payload["errors_recent"]
    assert len(err) == 1 and err[0]["error_code"] == "timeout"


def test_slow_turns_from_traces(session, fake_settings, monkeypatch):
    """«Медленные» = турны из trace.log (latency_ms в БД пуст на прод-пути)."""
    from types import SimpleNamespace as NS

    fake_traces = [
        NS(timestamp="2026-07-02 22:05:03.160", user_id="user_tg_755682022",
           channel="max", total_ms=30_282,
           stages=[NS(name="llm", duration_ms=28_000), NS(name="tools", duration_ms=1_500)]),
        NS(timestamp="2020-01-01 00:00:00.000", user_id="old", channel="tg",
           total_ms=99_000, stages=[]),  # старше 24ч — вне окна
    ]
    monkeypatch.setattr(
        "sreda.admin.trace_parser.parse_trace_log",
        lambda path, **kw: fake_traces,
    )
    fake_settings.trace_log_path = "/var/log/sreda/trace.log"
    from datetime import UTC, datetime
    blk = ov._slow_turns_block(fake_settings, datetime(2026, 7, 3, 6, 0, tzinfo=UTC))
    assert blk["count_24h"] == 1
    r = blk["recent"][0]
    assert r["total_ms"] == 30_282 and r["channel"] == "max"
    assert r["top_stage"] == "llm 28.0 с"


def test_slow_turns_failsoft_no_path(fake_settings):
    from datetime import UTC, datetime
    blk = ov._slow_turns_block(fake_settings, datetime.now(UTC))
    assert blk == {"count_24h": 0, "recent": []}


def test_users_block_counts(session, fake_settings):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text
    now = datetime.now(UTC)
    for tid, age_days in (("t_new", 0), ("t_week", 3), ("t_old", 40)):
        session.execute(text(
            "INSERT INTO tenants (id, name, created_at) VALUES (:i, :n, :c)"
        ), {"i": tid, "n": "x", "c": now - timedelta(days=age_days)})
    session.commit()
    blk = ov._users_block(session, now)
    assert blk["total"] == 3
    assert blk["new_7d"] == 2
    assert blk["new_today"] >= 1  # t_new (граница дня MSK)


def test_users_block_msk_day_boundary(session, fake_settings):
    """R1 (high+medium MAJOR): 21:30 UTC = 00:30 MSK СЛЕДУЮЩЕГО дня —
    тенант, созданный в 21:05 UTC, должен попасть в «сегодня» по Москве."""
    from datetime import UTC, datetime

    from sqlalchemy import text
    now = datetime(2026, 7, 2, 21, 30, tzinfo=UTC)  # 03.07 00:30 МСК
    session.execute(text(
        "INSERT INTO tenants (id, name, created_at) VALUES ('t_msk','x',:c)"
    ), {"c": datetime(2026, 7, 2, 21, 5, tzinfo=UTC)})  # 03.07 00:05 МСК
    session.commit()
    blk = ov._users_block(session, now)
    assert blk["new_today"] == 1  # по UTC-дате было бы 0 — день 02.07


def test_purchases_paid_without_paid_at_counted(session, fake_settings):
    """R1 (субагент MAJOR): paid-заказ с paid_at=NULL не должен выпадать
    (fallback на created_at)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from sreda.db.models.billing import PaymentOrder
    now = datetime.now(UTC)
    session.execute(text(
        "INSERT INTO tenants (id, name, created_at) VALUES ('t2','x',:c)"), {"c": now})
    session.add(PaymentOrder(
        id="onull", tenant_id="t2", provider_key="yookassa", order_type="sub",
        status="paid", amount_rub=700, description="d",
        paid_at=None, created_at=now - timedelta(days=1)))
    session.commit()
    blk = ov._purchases_block(session, now)
    assert blk["orders_7d"] == 1 and blk["sum_rub_7d"] == 700


def test_purchases_block_counts(session, fake_settings):
    from datetime import UTC, datetime, timedelta

    from sreda.db.models.billing import PaymentOrder
    now = datetime.now(UTC)
    session.execute(  # тенант для FK-читаемости (sqlite FK не форсит, но честно)
        __import__("sqlalchemy").text(
            "INSERT INTO tenants (id, name, created_at) VALUES ('t1','x',:c)"),
        {"c": now})
    session.add_all([
        PaymentOrder(id="o1", tenant_id="t1", provider_key="yookassa",
                     order_type="sub", status="paid", amount_rub=500,
                     description="d", paid_at=now - timedelta(days=2)),
        PaymentOrder(id="o2", tenant_id="t1", provider_key="yookassa",
                     order_type="sub", status="paid", amount_rub=300,
                     description="d", paid_at=now - timedelta(days=20)),
        PaymentOrder(id="o3", tenant_id="t1", provider_key="yookassa",
                     order_type="sub", status="created", amount_rub=999,
                     description="d"),  # НЕ оплачен — не считается
    ])
    session.commit()
    blk = ov._purchases_block(session, now)
    assert blk["paid_tenants"] == 1
    assert blk["orders_7d"] == 1 and blk["sum_rub_7d"] == 500
    assert blk["orders_30d"] == 2 and blk["sum_rub_30d"] == 800


def test_cost_priced_pair_uses_confirmed_price(session, fake_settings):
    # mercury-2: input $0.25/M (cached $0.025, hit 0.76), output $0.75/M —
    # подтверждённый прайс в llm_pricing. 1M prompt + 100k completion:
    # upper = 1.0*0.25 + 0.1*0.75 = $0.325. Блок cost — через
    # get_cost_volume_summary (#150), снапшот лишь JSON-ит его.
    session.add_all([
        _exec_row(prompt_tokens=500_000, completion_tokens=50_000),
        _exec_row(prompt_tokens=500_000, completion_tokens=50_000),
    ])
    session.commit()
    payload = ov.compute_overview(session, fake_settings)
    day = payload["cost"]["day"]
    assert day["calls"] == 2
    assert day["upper_subtotal_usd"] == pytest.approx(0.325, abs=1e-3)
    # est < upper (кеш-допущение Mercury удешевляет вход)
    assert day["priced_subtotal_usd"] < day["upper_subtotal_usd"]
    row = day["rows"][0]
    assert row["priced"] is True and row["provider_key"] == "inception-mercury2"


def test_cost_unpriced_pair_no_invented_dollars(session, fake_settings):
    session.add(_exec_row(provider_key="mimo-v2.5-pro", model="mimo-v2.5-pro"))
    session.commit()
    payload = ov.compute_overview(session, fake_settings)
    day = payload["cost"]["day"]
    assert day["priced_subtotal_usd"] == 0.0
    assert day["unpriced_calls"] == 1
    row = day["rows"][0]
    assert row["priced"] is False
    assert row["est_usd"] is None and row["upper_usd"] is None
    assert row["prompt_tokens"] == 1000  # токены показываем честно


def test_providers_block_groups_by_provider(session, fake_settings):
    """Фаза B: карточки провайдеров — траты из отчётов #150, роли из
    task_type, баланс по префиксу, беспрайсовое токенами."""
    session.add_all([
        # Mercury: priced, роль «диалог»
        _exec_row(task_type="react_turn", prompt_tokens=500_000, completion_tokens=50_000),
        _exec_row(task_type="react_turn", prompt_tokens=500_000, completion_tokens=50_000),
        # Yandex STT: беспрайсовый, роль «распознавание речи»
        _exec_row(provider_key="yandex", model="stt-general",
                  task_type="speech_recognition", prompt_tokens=1000, completion_tokens=0),
        # ошибка у Mercury за 24ч
        _exec_row(task_type="react_turn", status="failed"),
    ])
    session.commit()
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    from sreda.admin.queries import get_cost_volume_summary
    reports = get_cost_volume_summary(session)
    balances = [{"key": "inception-mercury2", "label": "Inception",
                 "status": "not_supported", "headline": "нет billing API", "details": ""}]
    provs = ov._providers_block(session, now, reports, balances)
    by_key = {p["key"]: p for p in provs}
    assert "inception" in by_key and "yandex" in by_key
    m = by_key["inception"]
    assert m["balance"]["headline"] == "нет billing API"     # префикс-мапа сработала
    assert m["errors_24h"]["errors"] == 1
    assert m["spend"]["month"]["est_usd"] > 0                # priced
    assert m["models"][0]["roles"].startswith("диалог")
    y = by_key["yandex"]
    assert y["spend"]["month"]["est_usd"] == 0.0             # беспрайсовый — не выдумываем $
    assert y["spend"]["month"]["unpriced_tokens"] == 1000
    assert y["models"][0]["roles"] == "распознавание речи"
    assert y["balance"] is None                               # в balances не передан


def test_snapshot_roundtrip(session, fake_settings):
    payload = ov.compute_overview(session, fake_settings)
    ov.store_snapshot(session, ov.KEY_OVERVIEW, payload)
    loaded, updated_at = ov.load_snapshot(session, ov.KEY_OVERVIEW)
    assert loaded["llm_24h"]["calls"] == 0
    assert updated_at is not None
    # повторный store НЕ плодит строк (upsert)
    ov.store_snapshot(session, ov.KEY_OVERVIEW, {"x": 1})
    loaded2, _ = ov.load_snapshot(session, ov.KEY_OVERVIEW)
    assert loaded2 == {"x": 1}


def test_load_missing_and_bad_json(session):
    loaded, updated_at = ov.load_snapshot(session, "nope")
    assert loaded == {} and updated_at is None
    ov.store_snapshot(session, "bad", {"a": 1})
    row = session.get(ov.AdminDashboardSnapshot, "bad")
    row.payload_json = "{broken"
    session.commit()
    loaded, updated_at = ov.load_snapshot(session, "bad")
    assert loaded == {} and updated_at is None


def test_refresh_overview_never_raises(session_factory, fake_settings, monkeypatch):
    # штатный проход
    assert ov.refresh_overview(session_factory, fake_settings) is True
    # компьют взорвался → False, не исключение (луп в job_runner переживёт)
    monkeypatch.setattr(ov, "compute_overview",
                        lambda s, st: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ov.refresh_overview(session_factory, fake_settings) is False


def test_balances_failsoft(session, fake_settings, monkeypatch):
    # сорвём балансы «по-настоящему»: вернуть реальную функцию (захвачена
    # до autouse-мока) и уронить fetch_balances
    monkeypatch.setattr(ov, "_balances_block", _REAL_BALANCES_BLOCK)
    monkeypatch.setattr(
        "sreda.services.provider_balances.fetch_balances",
        lambda settings: (_ for _ in ()).throw(RuntimeError("net down")),
    )
    payload = ov.compute_overview(session, fake_settings)
    assert payload["balances"] == []  # деградация, не 500
