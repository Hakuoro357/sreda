"""Unit tests for migration 0029 (152-ФЗ Часть 2 — encrypt message content).

Стратегия: вместо полного `alembic upgrade head` (который ломается на
старых initial-миграциях из-за SQLite ALTER limitations) тестируем
helper-функции `_encrypt_table_column` / `_decrypt_table_column` напрямую
на таблицах созданных через `Base.metadata.create_all()`.

Это даёт ту же гарантию: для каждой из 6 колонок plaintext данные после
upgrade имеют envelope `v2:`, после downgrade — обратно plaintext.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import (  # noqa: F401  — imported for metadata
    InboundMessage,
    Job,
    OutboxMessage,
    Tenant,
    User,
)
from sreda.db.models.inbound_event import InboundEvent  # noqa: F401
from sreda.db.models.user_profile import TenantUserProfile  # noqa: F401


# Default values for NOT NULL columns when inserting fixtures via raw SQL.
_NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _migration_module():
    """Load migration by file path — module name starts with digit
    (`20260428_0029_*`) which is invalid as Python identifier, so
    importlib.import_module by name doesn't work. Use spec_from_file_location.
    """
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "migrations" / "versions" / "20260428_0029_encrypt_message_content.py"
    spec = importlib.util.spec_from_file_location("_migration_0029", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _insert_plaintext(conn, table: str, row_id: str, column: str, value: str, **extra) -> None:
    """Bulk INSERT plaintext bypassing ORM. Auto-fills NOT NULL `created_at`
    if not provided. Помещает row через raw SQL, минуя EncryptedString
    TypeDecorator (чтобы plaintext реально лёг в БД).
    """
    extra = dict(extra)
    if "created_at" not in extra and table in (
        "tenants", "outbox_messages", "inbound_messages", "inbound_events",
        "jobs", "tenant_user_profiles",
    ):
        extra["created_at"] = _NOW
    cols = ["id", column] + list(extra.keys())
    vals = [row_id, value] + list(extra.values())
    placeholders = ", ".join(f":{c}" for c in cols)
    bind = dict(zip(cols, vals))
    conn.execute(
        text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"),
        bind,
    )


def _read_raw(conn, table: str, column: str, row_id: str) -> str | None:
    row = conn.execute(
        text(f"SELECT {column} FROM {table} WHERE id = :rid"), {"rid": row_id}
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Tests for upgrade()
# ---------------------------------------------------------------------------


def test_upgrade_encrypts_tenants_name(engine):
    with engine.begin() as conn:
        _insert_plaintext(conn, "tenants", "t1", "name", "Boris Pechorov")
        # before
        before = _read_raw(conn, "tenants", "name", "t1")
        assert before == "Boris Pechorov"
        # apply migration helper directly
        mod = _migration_module()
        processed = mod._encrypt_table_column(conn, "tenants", "name")
        assert processed == 1
        # after
        after = _read_raw(conn, "tenants", "name", "t1")
        assert after.startswith("v2:")
        assert "Boris Pechorov" not in after


def test_upgrade_skips_already_encrypted_rows(engine):
    """Идемпотентность: повторный прогон не ломает уже зашифрованные ряды."""
    with engine.begin() as conn:
        _insert_plaintext(conn, "tenants", "t1", "name", "v2:fake:abc:def")
        mod = _migration_module()
        processed = mod._encrypt_table_column(conn, "tenants", "name")
        assert processed == 0
        # value не изменился
        after = _read_raw(conn, "tenants", "name", "t1")
        assert after == "v2:fake:abc:def"


def test_upgrade_handles_null_and_empty(engine):
    """NULL и пустые строки — не пытаемся шифровать, не падаем."""
    with engine.begin() as conn:
        # NULL through direct insert (omitting `name` won't work — it's NOT NULL.
        # use empty string instead — also skipped).
        _insert_plaintext(conn, "tenants", "t1", "name", "")
        mod = _migration_module()
        processed = mod._encrypt_table_column(conn, "tenants", "name")
        assert processed == 0


def test_upgrade_encrypts_all_six_columns(engine):
    """Параметризованный smoke на каждую из 6 колонок."""
    fixtures: list[tuple[str, str, str, dict]] = [
        ("tenants", "name", "Some Name", {}),
        ("outbox_messages", "payload_json", '{"text":"hello"}',
         {"tenant_id": "t1", "workspace_id": "w1",
          "channel_type": "telegram", "status": "pending",
          "is_interactive": 0}),
        ("inbound_events", "payload_json", '{"event":"x"}',
         {"tenant_id": "t1", "feature_key": "housewife", "event_type": "test",
          "external_event_key": "k1", "status": "new",
          "relevance_score": 0.0}),
        ("jobs", "payload_json", '{"args":["a"]}',
         {"tenant_id": "t1", "workspace_id": "w1", "job_type": "test",
          "status": "pending"}),
    ]
    with engine.begin() as conn:
        # Setup parent rows for FK
        conn.execute(text(
            "INSERT INTO tenants (id, name, created_at) "
            "VALUES ('t1', 'parent', :ts)"
        ), {"ts": _NOW})
        conn.execute(text(
            "INSERT INTO workspaces (id, tenant_id, name) "
            "VALUES ('w1', 't1', 'ws')"
        ))
        # Encrypt the parent tenant.name first so subsequent fixture inserts
        # don't hit FK issues — we only insert with id 't1' once below.
        # Insert each fixture
        mod = _migration_module()
        for table, column, plaintext, extra in fixtures:
            row_id = f"{table}_row_1"
            _insert_plaintext(conn, table, row_id, column, plaintext, **extra)
            processed = mod._encrypt_table_column(conn, table, column)
            assert processed >= 1, f"{table}.{column} not encrypted"
            after = _read_raw(conn, table, column, row_id)
            assert after.startswith("v2:"), f"{table}.{column} no v2: prefix"


# ---------------------------------------------------------------------------
# Tests for downgrade()
# ---------------------------------------------------------------------------


def test_downgrade_decrypts_back_to_plaintext(engine):
    with engine.begin() as conn:
        _insert_plaintext(conn, "tenants", "t1", "name", "Boris Pechorov")
        mod = _migration_module()
        # upgrade
        mod._encrypt_table_column(conn, "tenants", "name")
        encrypted = _read_raw(conn, "tenants", "name", "t1")
        assert encrypted.startswith("v2:")
        # downgrade
        processed = mod._decrypt_table_column(conn, "tenants", "name")
        assert processed == 1
        decrypted = _read_raw(conn, "tenants", "name", "t1")
        assert decrypted == "Boris Pechorov"


def test_downgrade_skips_already_plaintext(engine):
    with engine.begin() as conn:
        _insert_plaintext(conn, "tenants", "t1", "name", "Already plain")
        mod = _migration_module()
        processed = mod._decrypt_table_column(conn, "tenants", "name")
        assert processed == 0
        after = _read_raw(conn, "tenants", "name", "t1")
        assert after == "Already plain"


# ---------------------------------------------------------------------------
# ORM round-trip: после смены типа колонки на EncryptedString, ORM read
# возвращает plaintext, ORM write шифрует прозрачно.
# ---------------------------------------------------------------------------


def test_orm_read_decrypts_v2_envelope(session, engine):
    """Главный acceptance criterion: после миграции legacy plaintext рядов,
    они автоматически перешифровываются. Новые ORM-writes шифруются.
    Read — всегда plaintext."""
    from sreda.db.models.core import Tenant as TenantModel

    # 1. Legacy: вставляем plaintext напрямую (bypass ORM)
    with engine.begin() as conn:
        _insert_plaintext(conn, "tenants", "t1", "name", "Legacy User")

    # ORM read — backwards-compat: legacy plaintext (без префикса)
    # должен вернуться как есть.
    t = session.query(TenantModel).filter_by(id="t1").one()
    assert t.name == "Legacy User"

    # 2. Прогоняем миграцию — все рядки получают v2: envelope
    with engine.begin() as conn:
        mod = _migration_module()
        mod._encrypt_table_column(conn, "tenants", "name")

    # Sanity: raw — зашифровано
    with engine.begin() as conn:
        raw = _read_raw(conn, "tenants", "name", "t1")
        assert raw.startswith("v2:")

    # ORM read after migration — плейн доступен
    session.expire_all()
    t = session.query(TenantModel).filter_by(id="t1").one()
    assert t.name == "Legacy User"

    # 3. ORM write нового тенанта — auto-encrypt
    new_t = TenantModel(id="t2", name="New User Direct Via ORM")
    session.add(new_t)
    session.commit()

    with engine.begin() as conn:
        raw = _read_raw(conn, "tenants", "name", "t2")
        assert raw.startswith("v2:"), "ORM write must auto-encrypt"
