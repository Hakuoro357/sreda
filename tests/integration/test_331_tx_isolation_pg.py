"""#331 integration: конфликт begin-события #138 (set_config GUC) с изоляцией quota-путей — на РЕАЛЬНОМ
Postgres (unit-свит на SQLite баг не ловил: `SET TRANSACTION` там не падает).

Ловит класс инцидента #331 ДО мержа: под tenant_ctx begin-событие эмитит `set_config` первым запросом
транзакции; при СТАРОМ коде (ручной `SET TRANSACTION ISOLATION LEVEL` внутри tx) Postgres роняет
ActiveSqlTransaction. Фикс (execution_options) → проходит. Мутация фикса → этот тест краснеет на реальном PG.

Не нужны миграции/RLS-роли: конфликт возникает от самого begin-хука, не от политик. DSN через env
(нет → skip, unit-прогоны не трогаются).
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.pg

_DSN = os.environ.get("SREDA_PG_TEST_DSN")
if not _DSN:
    pytest.skip("SREDA_PG_TEST_DSN не задан — #331 red-suite бежит только против реального Postgres",
                allow_module_level=True)

_TT = "tenant_ci_331"


@pytest.fixture()
def pg_engine(monkeypatch):
    monkeypatch.setenv("SREDA_DATABASE_URL", _DSN)
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401 — регистрирует таблицы в metadata
    from sreda.db.session import get_engine
    get_engine.cache_clear()
    eng = get_engine()  # на нём висит begin-событие #138 (_emit_tenant_setconfig)
    Base.metadata.create_all(eng)
    from sqlalchemy import text
    # FK: usage_ledger.tenant_id → tenants; заводим тенант
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name, created_at) VALUES (:i,:n, now()) "
                       "ON CONFLICT (id) DO NOTHING"), {"i": _TT, "n": "ci-331"})
    yield eng
    with eng.begin() as c:
        for tbl in ("usage_ledger", "web_search_usage", "fetch_url_usage", "tenants"):
            try:
                c.execute(text(f'DELETE FROM "{tbl}" WHERE tenant_id=:t' if tbl != "tenants"
                               else 'DELETE FROM tenants WHERE id=:t'), {"t": _TT})
            except Exception:
                pass
    get_engine.cache_clear()


def test_331_quota_paths_under_begin_hook_no_active_sql_transaction(pg_engine):
    """Все 3 quota-пути под tenant_ctx (begin-хук активен) НЕ падают ActiveSqlTransaction на реальном PG."""
    from sreda.db.session import tenant_ctx
    from sreda.services.usage_ledger import UsageLedgerService, msk_period_keys
    from sreda.services.web_search_usage import try_consume_tavily, QuotaDecision
    from sreda.services.fetch_url_usage import try_consume_fetch_url

    daily, monthly = msk_period_keys()
    tok = tenant_ctx.set(_TT)  # begin-событие #138 эмитит set_config первым запросом каждой tx
    try:
        assert UsageLedgerService(pg_engine).try_consume(
            _TT, "llm_turns", 1, [("daily", daily, 10), ("monthly", monthly, 200)]) is True
        assert try_consume_tavily(pg_engine, _TT, "u1", per_user_cap=5, global_cap=100) == QuotaDecision.ALLOW
        assert try_consume_fetch_url(pg_engine, _TT, "u1", 10) is True
    finally:
        tenant_ctx.reset(tok)
