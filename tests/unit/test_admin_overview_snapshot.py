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
) -> SkillAIExecution:
    n = next(_SEQ)
    return SkillAIExecution(
        id=f"ex_{n}",
        run_id=f"run_{n}",
        tenant_id="tenant_test",
        feature_key="housewife_assistant",
        task_type="chat",
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


def test_llm_24h_counts_errors_and_slow(session, fake_settings):
    session.add_all([
        _exec_row(),                                   # ok
        _exec_row(status="failed", error_code="timeout"),
        _exec_row(status="validation_failed"),
        _exec_row(latency_ms=45_000),                  # slow
        _exec_row(age_hours=30),                       # вне окна 24ч
    ])
    session.commit()
    payload = ov.compute_overview(session, fake_settings)
    blk = payload["llm_24h"]
    assert blk["calls"] == 4          # строка age=30h не в окне
    assert blk["errors"] == 2
    assert blk["error_rate_pct"] == 50.0
    assert blk["slow"] == 1


def test_errors_and_slow_lists_shapes(session, fake_settings):
    session.add_all([
        _exec_row(status="failed", error_code="timeout", model="mercury-2"),
        _exec_row(latency_ms=61_000),
    ])
    session.commit()
    payload = ov.compute_overview(session, fake_settings)
    err = payload["errors_recent"]
    assert len(err) == 1 and err[0]["error_code"] == "timeout"
    slow = payload["slow_recent"]
    assert len(slow) == 1 and slow[0]["latency_ms"] == 61_000


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
