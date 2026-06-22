"""#187 Фаза 0 — SQLite Alembic round-trip миграции 0063 (`tenants.deleted_at` + partial-индекс).

Чеклист приёмки A9: миграция аддитивна; downgrade убирает колонку+индекс, upgrade возвращает;
существующие тенанты остаются `deleted_at=NULL` (БЕЗ backfill — в отличие от approved_at). Форма как
0051-round-trip (`test_alembic_operation_id_columns_sqlite.py`). RED до создания миграции 0063.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "test_migrations.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("SREDA_DATABASE_URL", url)
    monkeypatch.setenv("SREDA_LANGGRAPH_CHECKPOINTER", "memory")
    monkeypatch.delenv("SREDA_LANGGRAPH_PERSISTENCE_OPTED_IN", raising=False)

    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    yield cfg, db_path
    get_settings.cache_clear()


def _bootstrap_stamped_at_0063(engine, cfg) -> None:
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    Base.metadata.create_all(engine)
    command.stamp(cfg, "20260622_0063")


def test_migration_0063_downgrade_then_upgrade(alembic_cfg):
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_stamped_at_0063(engine, cfg)

    # Существующий тенант — без deleted_at (имитирует живого пользователя до миграции).
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO tenants (id, name, created_at) "
            "VALUES ('t_existing', 'enc', CURRENT_TIMESTAMP)"
        ))

    # Downgrade → колонка + partial-индекс уходят.
    command.downgrade(cfg, "20260622_0062")
    cols = {c["name"] for c in inspect(engine).get_columns("tenants")}
    assert "deleted_at" not in cols, f"downgrade не убрал deleted_at; got {sorted(cols)}"

    # Upgrade обратно → колонка + partial-индекс возвращаются.
    command.upgrade(cfg, "20260622_0063")
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("tenants")}
    assert "deleted_at" in cols, f"upgrade не вернул deleted_at; got {sorted(cols)}"
    idx = {ix["name"] for ix in insp.get_indexes("tenants")}
    assert "ix_tenants_deleted_at" in idx, f"нет partial-индекса ix_tenants_deleted_at; got {sorted(idx)}"

    # Пиним именно PARTIAL (не full index): SQLite-инспектор не отдаёт WHERE-предикат
    # в get_indexes(), поэтому читаем DDL из sqlite_master (ревью Ф0: все 3 гейта MINOR).
    with engine.connect() as conn:
        ddl = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_tenants_deleted_at'"
        )).scalar()
    assert ddl is not None and "deleted_at IS NOT NULL" in ddl, (
        f"индекс должен быть PARTIAL (WHERE deleted_at IS NOT NULL), не full; DDL={ddl!r}"
    )

    # Существующий тенант остался активным: deleted_at=NULL (аддитивно, без backfill).
    with engine.connect() as conn:
        val = conn.execute(
            text("SELECT deleted_at FROM tenants WHERE id='t_existing'")
        ).scalar()
    assert val is None, "существующий тенант должен иметь deleted_at=NULL (без backfill)"

    engine.dispose()
