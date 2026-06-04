"""Unit tests for migration 0048's pre-existing-duplicate dedup helper.

A past race (legacy query-before-insert dedup, no DB constraint) could persist
the same Telegram update more than once.  Migration 0048 must collapse those
duplicates before creating the partial unique index, otherwise
``CREATE UNIQUE INDEX`` fails with a UniqueViolation (observed on prod:
``(telegram, sreda, 499721179)`` duplicated 24×).

These tests load the migration module directly and exercise
``_dedup_inbound_duplicates`` against minimal SQLite tables holding only the
columns the dedup SQL touches.  The ``agent_runs`` stand-in carries a REAL FK
to ``inbound_messages`` with ``PRAGMA foreign_keys=ON`` enabled, so the test
proves actual referential integrity (a missed repoint would raise on delete),
not merely value changes (Codex medium R1).  The production path is PostgreSQL,
but the SQL is intentionally portable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa


_MIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "20260603_0048_inbound_dedup_composite_key.py"
)

_ARCHIVE_TABLE = "inbound_messages_dedup_archive_0048"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0048", _MIG_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine_with_fk():
    """In-memory SQLite engine with foreign-key enforcement turned on."""
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):  # pragma: no cover - trivial hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _make_schema(conn) -> None:
    # ``id`` is NOT NULL (matches the real PK) so the EXISTS-based dedup is not
    # exposed to NULL ids; SQLite TEXT PRIMARY KEY would otherwise allow NULL.
    conn.execute(sa.text(
        "CREATE TABLE inbound_messages ("
        " id TEXT NOT NULL PRIMARY KEY,"
        " channel_type TEXT NOT NULL,"
        " bot_key TEXT NOT NULL,"
        " external_update_id TEXT"
        ")"
    ))
    # Real FK mirrors prod (agent_runs is the only table referencing
    # inbound_messages); with foreign_keys=ON, deleting a still-referenced row
    # would raise — so the test fails loudly if the repoint is wrong.
    conn.execute(sa.text(
        "CREATE TABLE agent_runs ("
        " id TEXT NOT NULL PRIMARY KEY,"
        " inbound_message_id TEXT REFERENCES inbound_messages(id)"
        ")"
    ))


def _insert_inbound(conn, id_, channel, bot, upd) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO inbound_messages"
            " (id, channel_type, bot_key, external_update_id)"
            " VALUES (:i, :c, :b, :u)"
        ),
        {"i": id_, "c": channel, "b": bot, "u": upd},
    )


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        sa.text(
            "SELECT count(*) FROM sqlite_master"
            " WHERE type='table' AND name=:n"
        ),
        {"n": name},
    ).scalar() == 1


def test_dedup_collapses_duplicates_and_repoints_fk():
    mod = _load_migration()
    engine = _engine_with_fk()
    with engine.begin() as conn:
        _make_schema(conn)
        # Dup group telegram/sreda/100 → 3 rows (survivor = lexicographic min id).
        _insert_inbound(conn, "in_a", "telegram", "sreda", "100")
        _insert_inbound(conn, "in_b", "telegram", "sreda", "100")
        _insert_inbound(conn, "in_c", "telegram", "sreda", "100")
        # Distinct groups (same numeric update id) must survive untouched.
        _insert_inbound(conn, "in_d", "telegram", "sreda_home", "100")
        _insert_inbound(conn, "in_e", "max", "sreda", "100")
        # NULL external_update_id rows are never constrained — keep all.
        _insert_inbound(conn, "in_f", "telegram", "sreda", None)
        _insert_inbound(conn, "in_g", "telegram", "sreda", None)
        # Run pointing at a NON-survivor must be repointed to the survivor.
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, inbound_message_id)"
            " VALUES ('run_1', 'in_c')"
        ))
        # Run already pointing at the survivor stays put.
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, inbound_message_id)"
            " VALUES ('run_2', 'in_a')"
        ))
        # Run with NULL FK is irrelevant and untouched.
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, inbound_message_id)"
            " VALUES ('run_3', NULL)"
        ))

        mod._dedup_inbound_duplicates(conn)

        # Dup group collapsed to the single survivor.
        survivors = conn.execute(sa.text(
            "SELECT id FROM inbound_messages"
            " WHERE channel_type='telegram' AND bot_key='sreda'"
            " AND external_update_id='100' ORDER BY id"
        )).scalars().all()
        assert survivors == ["in_a"]

        # Other groups untouched.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM inbound_messages WHERE bot_key='sreda_home'"
        )).scalar() == 1
        assert conn.execute(sa.text(
            "SELECT count(*) FROM inbound_messages WHERE channel_type='max'"
        )).scalar() == 1
        # NULL update_id rows all preserved.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM inbound_messages"
            " WHERE external_update_id IS NULL"
        )).scalar() == 2

        # FK repointed to survivor; survivor-pointing run unchanged; NULL stays.
        assert conn.execute(sa.text(
            "SELECT inbound_message_id FROM agent_runs WHERE id='run_1'"
        )).scalar() == "in_a"
        assert conn.execute(sa.text(
            "SELECT inbound_message_id FROM agent_runs WHERE id='run_2'"
        )).scalar() == "in_a"
        assert conn.execute(sa.text(
            "SELECT inbound_message_id FROM agent_runs WHERE id='run_3'"
        )).scalar() is None

        # Doomed rows were archived (exactly the two non-survivors) before delete.
        assert _table_exists(conn, _ARCHIVE_TABLE)
        archived = conn.execute(sa.text(
            f"SELECT id FROM {_ARCHIVE_TABLE} ORDER BY id"
        )).scalars().all()
        assert archived == ["in_b", "in_c"]

        # The partial unique index is now creatable without a UniqueViolation.
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX ux_test ON inbound_messages"
            " (channel_type, bot_key, external_update_id)"
            " WHERE external_update_id IS NOT NULL"
        ))


def test_dedup_is_noop_without_duplicates():
    mod = _load_migration()
    engine = _engine_with_fk()
    with engine.begin() as conn:
        _make_schema(conn)
        _insert_inbound(conn, "in_a", "telegram", "sreda", "1")
        _insert_inbound(conn, "in_b", "telegram", "sreda", "2")
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, inbound_message_id)"
            " VALUES ('run_1', 'in_a')"
        ))

        mod._dedup_inbound_duplicates(conn)

        assert conn.execute(sa.text(
            "SELECT count(*) FROM inbound_messages"
        )).scalar() == 2
        assert conn.execute(sa.text(
            "SELECT inbound_message_id FROM agent_runs WHERE id='run_1'"
        )).scalar() == "in_a"
        # No duplicates → no archive table is created (fresh DBs stay clean).
        assert not _table_exists(conn, _ARCHIVE_TABLE)
