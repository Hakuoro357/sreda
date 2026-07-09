"""#138 Ф3-c — мета-тест дрейфа RLS-классификации (fail-closed гейт).

Каждая таблица Base.metadata обязана быть классифицирована в миграции 0082 РОВНО
в одном из множеств: TENANT_TABLES (RLS-изоляция по tenant_id) / ROOT_TABLES
(tenants) / NO_RLS_TABLES (осознанное исключение). Новая таблица без классификации
= красный тест → «классифицируй в 0082» (или добавь NO_RLS-исключение осознанно).
Плюс санити: у каждой TENANT-таблицы реально есть колонка tenant_id; опечатка в
имени (несуществующая таблица в списке) тоже ловится.
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


def test_rls_registry_covers_all_tables_exactly_once():
    """(а)+(б): каждая таблица метадаты ∈ ровно одному множеству; списки без мёртвых имён."""
    mig = _load_migration_0082()
    tenant = set(mig.TENANT_TABLES)
    root = set(mig.ROOT_TABLES)
    no_rls = set(mig.NO_RLS_TABLES)

    # Дубликаты внутри списков (кортеж мог задвоить имя).
    assert len(mig.TENANT_TABLES) == len(tenant), "дубликаты в TENANT_TABLES (0082)"
    assert len(mig.NO_RLS_TABLES) == len(no_rls), "дубликаты в NO_RLS_TABLES (0082)"

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
    mig = _load_migration_0082()
    tables = _metadata_tables()
    missing = sorted(
        t for t in mig.TENANT_TABLES
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
