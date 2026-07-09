"""#138 Ф3-c — мета-тест дрейфа RLS-классификации (fail-closed гейт).

R1 M8-фикс: реестр — источник истины в APP-модуле ``sreda.db.rls_registry`` (НЕ в
применённой миграции 0082, иначе добавление имени туда не создавало политику →
несамоисполнимо). Каждая таблица Base.metadata обязана быть классифицирована РОВНО
в одном множестве реестра. Новая таблица без классификации = красный → «классифицируй
в rls_registry + напиши миграцию ENABLE RLS». Санити: TENANT-таблица имеет tenant_id;
опечатка (мёртвое имя) ловится. Parity реестр ⇔ pg_policies на реальном PG — в red-suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_0082():
    root = Path(__file__).resolve().parents[2]
    path = root / "migrations" / "versions" / "20260709_0082_enable_rls_policies.py"
    spec = importlib.util.spec_from_file_location("migration_0082", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _metadata_tables():
    """Все таблицы Base.metadata (НЕ __all__ — audit_log не в __all__, owner-решение №1)."""
    from sreda.db.base import Base

    import sreda.db.models  # noqa: F401 — регистрирует ядро таблиц
    # Подмодули, которые __init__ не импортирует (как в conftest _test_engine):
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    return Base.metadata.tables


def test_migration_0082_snapshot_matches_registry():
    """0082 держит ИСТОРИЧЕСКИЙ снапшот; на МОМЕНТ 0082 он обязан совпадать с
    app-реестром (иначе реальные политики разошлись бы с источником истины).
    Будущие таблицы добавляются в реестр + НОВОЙ миграцией — тогда снапшот 0082
    станет подмножеством реестра; здесь (пока новых RLS-миграций нет) — равенство."""
    from sreda.db import rls_registry as reg

    mig = _load_migration_0082()
    assert set(mig.TENANT_TABLES) == reg.TENANT_TABLES, "0082 TENANT-снапшот разошёлся с rls_registry"
    assert set(mig.ROOT_TABLES) == reg.ROOT_TABLES
    assert set(mig.NO_RLS_TABLES) == reg.NO_RLS_TABLES
    assert set(mig.IDENTITY_INSERT_TABLES) == reg.IDENTITY_INSERT_TABLES


def test_rls_registry_covers_all_tables_exactly_once():
    """(а)+(б): каждая таблица метадаты ∈ ровно одному множеству app-реестра; без мёртвых имён."""
    from sreda.db import rls_registry as reg

    tenant = set(reg.TENANT_TABLES)
    root = set(reg.ROOT_TABLES)
    no_rls = set(reg.NO_RLS_TABLES)

    # Ровно одно множество на таблицу — пересечения пусты.
    assert not tenant & root, f"таблицы и в TENANT, и в ROOT: {sorted(tenant & root)}"
    assert not tenant & no_rls, f"таблицы и в TENANT, и в NO_RLS: {sorted(tenant & no_rls)}"
    assert not root & no_rls, f"таблицы и в ROOT, и в NO_RLS: {sorted(root & no_rls)}"

    classified = tenant | root | no_rls
    actual = set(_metadata_tables()) - {"alembic_version"}

    unclassified = actual - classified
    assert not unclassified, (
        "Новые таблицы БЕЗ RLS-классификации — классифицируй в миграции 0082 "
        "(TENANT_TABLES / ROOT_TABLES / NO_RLS_TABLES, NO_RLS только осознанно "
        f"с причиной): {sorted(unclassified)}"
    )

    ghosts = classified - actual
    assert not ghosts, (
        "В списках 0082 есть несуществующие таблицы (опечатка = молчаливо "
        f"незащищённая таблица; поправь имя): {sorted(ghosts)}"
    )


def test_tenant_tables_have_tenant_id_column():
    """(в): изоляционная политика бессмысленна без колонки tenant_id."""
    from sreda.db import rls_registry as reg
    tables = _metadata_tables()
    missing = sorted(
        t for t in reg.TENANT_TABLES
        if t in tables and "tenant_id" not in tables[t].columns
    )
    assert not missing, (
        f"TENANT_TABLES без колонки tenant_id (политика 0082 не сработает): {missing}"
    )


def test_root_tenants_has_id_column():
    """(г): политика p_tenants_self фильтрует по id."""
    tables = _metadata_tables()
    assert "tenants" in tables
    assert "id" in tables["tenants"].columns
