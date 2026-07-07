# -*- coding: utf-8 -*-
"""#138 Ф1 — RED-тесты ContextVar-шва изоляции семей (RLS).

Задают КОНТРАКТ шва ДО реализации (TDD). Юнит-уровень: ContextVar set/reset, выбор движка,
fail-closed реестр privileged. Реальный `SET LOCAL sreda.tenant_id` + энфорс RLS под ролью
`sreda_app` — integration-тест на настоящем Postgres (маркер `pg`, здесь skip без PG; g-061:
дефолтный unit-SQLite не энфорсит RLS/роли).

Контракт (что Ф1 обязана дать в `sreda.db.session`):
- `tenant_ctx: ContextVar[str|None]`, `access_ctx: ContextVar[str]` ('tenant'|'privileged').
- `tenant_session(tenant_id)` — ctx-manager: ставит tenant_ctx=tid + access_ctx='tenant', сессия на
  app-движке; сбрасывает ctx на выходе (в т.ч. на исключении).
- `privileged_session(reason)` — ctx-manager: reason ОБЯЗАН быть в реестре `PRIVILEGED_REASONS`
  иначе fail-closed (raise); ставит access_ctx='privileged'; сессия на maintenance/identity-движке.
- `get_app_engine()`/`get_maintenance_engine()`/`get_identity_engine()` (lru_cache).
- `assert_runtime_role(expected)` — рантайм-ассерт `current_user` (fail-closed).
"""
from __future__ import annotations

import pytest

from sreda.db import session as dbs


def test_ctx_defaults_are_safe():
    """До входа в обёртку контекста нет — шов не должен «протекать» дефолтным тенантом."""
    assert dbs.tenant_ctx.get() is None


def test_tenant_session_sets_and_resets_ctx():
    with dbs.tenant_session("tenant_max_A") as s:
        assert dbs.tenant_ctx.get() == "tenant_max_A"
        assert dbs.access_ctx.get() == "tenant"
        assert s.execute  # это рабочая сессия
    assert dbs.tenant_ctx.get() is None  # сброс на выходе


def test_tenant_session_resets_ctx_on_exception():
    with pytest.raises(RuntimeError):
        with dbs.tenant_session("tenant_max_B"):
            assert dbs.tenant_ctx.get() == "tenant_max_B"
            raise RuntimeError("boom")
    assert dbs.tenant_ctx.get() is None  # сброс даже на исключении


def test_privileged_session_registered_reason_ok():
    # 'inbound-provision' (identity) — легальный reason из реестра
    with dbs.privileged_session("inbound-provision") as s:
        assert dbs.access_ctx.get() == "privileged"
        assert s.execute
    assert dbs.access_ctx.get() == "tenant"  # дефолт после выхода


def test_privileged_session_unregistered_reason_fail_closed():
    """Reason не в реестре → отказ (fail-closed). Иначе privileged_session = дыра обхода."""
    with pytest.raises((ValueError, PermissionError)):
        with dbs.privileged_session("totally_made_up_reason"):
            pass


def test_privileged_session_empty_reason_fail_closed():
    with pytest.raises((ValueError, PermissionError)):
        with dbs.privileged_session(""):
            pass


def test_privileged_session_clears_ambient_tenant_ctx():
    """Ф1-R1 MAJOR: privileged ВНУТРИ tenant_session(A) НЕ должен унаследовать tenant_ctx=A
    (иначе begin-событие общего движка выставит чужой sreda.tenant_id в привилегированном блоке).
    privileged ходит по РОЛИ, не по GUC → внутри tenant_ctx обязан быть None; снаружи — восстановлен."""
    with dbs.tenant_session("tenant_max_A"):
        assert dbs.tenant_ctx.get() == "tenant_max_A"
        with dbs.privileged_session("gc"):
            assert dbs.tenant_ctx.get() is None  # погашен внутри privileged
            assert dbs.access_ctx.get() == "privileged"
        assert dbs.tenant_ctx.get() == "tenant_max_A"  # восстановлен после выхода
    assert dbs.tenant_ctx.get() is None


def test_privileged_reasons_registry_is_small_and_explicit():
    """Реестр reason'ов — маленький явный allow-list (не «что угодно»)."""
    assert isinstance(dbs.PRIVILEGED_REASONS, (set, frozenset))
    assert "inbound-provision" in dbs.PRIVILEGED_REASONS
    # маркер: реестр не пустой и разумно мал (аудитопригоден)
    assert 0 < len(dbs.PRIVILEGED_REASONS) <= 20


def test_app_engine_begin_listener_registered():
    """На app-движке зарегистрировано событие 'begin' (эмитит set_config sreda.tenant_id).
    На SQLite set_config — no-op, но слушатель обязан быть (на PG он ставит контекст)."""
    from sqlalchemy import event

    eng = dbs.get_app_engine()
    assert event.contains(eng, "begin", dbs._emit_tenant_setconfig)  # noqa: SLF001


def test_three_engines_distinct_accessors_exist():
    """Три отдельных аксессора движков (app/maintenance/identity), каждый lru_cache."""
    assert callable(dbs.get_app_engine)
    assert callable(dbs.get_maintenance_engine)
    assert callable(dbs.get_identity_engine)


def test_create_task_captures_tenant_ctx():
    """#138 Ф2 срез-2 (detached-пересказчик): asyncio.create_task, созданная ВНУТРИ tenant-контекста,
    НАСЛЕДУЕТ tenant_ctx (копия контекста на момент создания). Пересказчик спавнится внутри
    tenant-скоупа inbound-хода → detached-summary пишет checkpoint под ВЕРНЫМ тенантом, даже когда
    внешний ход уже сбросил ctx в finally. Это то, что делает срез-2 покрытым срезом-1."""
    import asyncio

    captured: dict[str, object] = {}

    async def _main() -> None:
        async def _detached() -> None:
            captured["tid"] = dbs.tenant_ctx.get()

        with dbs.tenant_session("tenant_max_detached"):
            task = asyncio.create_task(_detached())
        # вышли из tenant_session → внешний ctx сброшен, НО задача несёт свою копию
        assert dbs.tenant_ctx.get() is None
        await task

    asyncio.run(_main())
    assert captured["tid"] == "tenant_max_detached"  # detached-задача видела тенанта хода


def test_queue_dispatch_reason_is_registered_maintenance():
    """#138 Ф2 срез-4 (message_dispatcher): консьюмер очереди message_jobs —
    КРОСС-ТЕНАНТНЫЙ (claim/mark/heartbeat видят джобы всех семей) → ходит под
    privileged_session('queue-dispatch'). Reason зарегистрирован и НЕ identity
    (идёт на maintenance-движок, не на узкую identity-роль). Пер-тенантный
    dispatch хода — под tenant_session(job.tenant_id) (см. тесты диспетчера)."""
    assert "queue-dispatch" in dbs.PRIVILEGED_REASONS
    assert "queue-dispatch" not in dbs._IDENTITY_REASONS  # noqa: SLF001
    with dbs.privileged_session("queue-dispatch") as s:
        assert dbs.access_ctx.get() == "privileged"
        assert dbs.tenant_ctx.get() is None  # privileged не течёт тенантом
        assert s.execute


@pytest.mark.pg
def test_integration_rls_under_app_role_isolates_tenants():
    """Плейсхолдер integration-теста (реальный Postgres под ролью sreda_app): два тенанта,
    SELECT под А → 0 строк Б; INSERT чужого tenant_id → отказ. Реализуется в Ф3 red-suite.
    Здесь skip без PG-фикстуры (маркер pg)."""
    pytest.skip("integration PG red-suite — Ф3 (нужны роли+RLS на реальном Postgres)")
