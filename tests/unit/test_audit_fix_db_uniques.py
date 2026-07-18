"""Audit 2026-07-18 fix regression tests — slug ``db-uniques``.

Покрывает фиксы находок db-migrations #1/#2/#3/#4, cross-concurrency FC-2
(message_jobs +bot_key, users.max_account_id partial unique, AgentThread
unique, MenuPlanItem unique), cross-security N1 (шифрование
``message_jobs.message_payload``) и миграцию ``20260718_0085``
(дедуп-хелперы, префлайт users, шифрование существующих payload'ов).

Уровень моделей — через ``db_session`` (SQLite ``create_all``). Уровень
миграции — прямые вызовы bind-хелперов 0085 на scratch-схеме SQLite, по
репо-прецеденту ``test_merge_housewife_plans_200`` (``_apply(conn)`` без
alembic upgrade-head стека). PG-only ветки (LOCK TABLE, FK drop/recreate)
пропускаются по диалекту внутри самих хелперов.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db import models as sreda_models
from sreda.db.models import (
    AgentThread,
    FreeTierUsage,
    MenuPlan,
    MenuPlanItem,
    MessageJob,
    Tenant,
    User,
    Workspace,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_migration_0085():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations" / "versions" / "20260718_0085_dedup_unique_hardening.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0085", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scratch_engine():
    return create_engine("sqlite:///:memory:")


def _job(**overrides: object) -> MessageJob:
    base = dict(
        id="job_1",
        tenant_id="tenant_1",
        thread_id="thread_1",
        channel="telegram",
        external_update_id="42",
        bot_key="sreda",
        message_payload={"text": "привет"},
        status="pending",
        enqueued_at=_now(),
        attempt=0,
    )
    base.update(overrides)
    return MessageJob(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# db-migrations #1: UNIQUE (channel, external_update_id, bot_key)
# ---------------------------------------------------------------------------


def test_message_jobs_same_update_id_different_bots_allowed(db_session: Session) -> None:
    """Ключевая регрессия находки #1: update 42 бота sreda и update 42 бота
    sreda_home — РАЗНЫЕ события, обе строки обязаны сохраниться."""
    db_session.add(_job(id="job_a", bot_key="sreda"))
    db_session.add(_job(id="job_b", bot_key="sreda_home", thread_id="thread_2"))
    db_session.flush()  # no IntegrityError
    assert db_session.query(MessageJob).count() == 2


def test_message_jobs_same_update_id_same_bot_rejected(db_session: Session) -> None:
    """Ределивери того же update тем же ботом — по-прежнему дедуп."""
    db_session.add(_job(id="job_a"))
    db_session.flush()
    db_session.add(_job(id="job_b", thread_id="thread_2"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_message_jobs_bot_key_not_null_with_default(db_session: Session) -> None:
    """Safety-net default 'sreda' (контракт OutboxMessage.bot_key): insert без
    bot_key не ломается, но колонка NOT NULL."""
    # Имитируем старый код, который bot_key не передаёт вообще.
    job = MessageJob(
        id="job_default",
        tenant_id="tenant_1",
        thread_id="thread_1",
        channel="telegram",
        external_update_id="42",
        message_payload={"text": "x"},
        status="pending",
        enqueued_at=_now(),
        attempt=0,
    )
    db_session.add(job)
    db_session.flush()
    loaded = db_session.query(MessageJob).filter_by(id="job_default").one()
    assert loaded.bot_key == "sreda"


# ---------------------------------------------------------------------------
# cross-security N1: message_payload зашифрован at rest
# ---------------------------------------------------------------------------


def test_message_payload_encrypted_at_rest(db_session: Session) -> None:
    payload = {"kind": "telegram_inbound", "payload": {"text": "купи молоко"}}
    db_session.add(_job(id="job_enc", message_payload=payload))
    db_session.flush()

    raw = db_session.execute(
        text("SELECT message_payload FROM message_jobs WHERE id = 'job_enc'")
    ).scalar()
    assert isinstance(raw, str)
    assert raw.startswith(("v1:", "v2:")), f"payload не зашифрован: {raw[:40]!r}"
    assert "купи молоко" not in raw

    db_session.expire_all()
    loaded = db_session.query(MessageJob).filter_by(id="job_enc").one()
    assert loaded.message_payload == payload


# ---------------------------------------------------------------------------
# FC-2: users.max_account_id partial unique (svc-inbound #1)
# ---------------------------------------------------------------------------


def _tenant(tid: str) -> Tenant:
    return Tenant(id=tid, name=f"Tenant {tid}")


def test_users_max_account_id_partial_unique(db_session: Session) -> None:
    db_session.add(_tenant("t1"))
    db_session.flush()
    db_session.add(User(id="u1", tenant_id="t1", max_account_id="max_777"))
    db_session.flush()
    db_session.add(User(id="u2", tenant_id="t1", max_account_id="max_777"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_users_max_account_id_null_duplicates_allowed(db_session: Session) -> None:
    """Partial: TG-only юзеры с NULL не ограничены."""
    db_session.add(_tenant("t1"))
    db_session.flush()
    db_session.add(User(id="u1", tenant_id="t1", max_account_id=None))
    db_session.add(User(id="u2", tenant_id="t1", max_account_id=None))
    db_session.flush()  # no IntegrityError


# ---------------------------------------------------------------------------
# FC-2: AgentThread unique (runtime-core #5)
# ---------------------------------------------------------------------------


def _workspace(wid: str, tid: str) -> Workspace:
    return Workspace(id=wid, tenant_id=tid, name=f"WS {wid}")


def _thread(tid_: str, suffix: str = "") -> AgentThread:
    return AgentThread(
        id=f"thread_{tid_}{suffix}",
        tenant_id="t1",
        workspace_id="w1",
        channel_type="telegram",
        external_chat_id="chat_1",
        status="active",
    )


def test_agent_threads_unique_triple(db_session: Session) -> None:
    db_session.add_all([_tenant("t1"), _workspace("w1", "t1")])
    db_session.flush()
    db_session.add(_thread("a"))
    db_session.flush()
    db_session.add(_thread("b"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_agent_threads_same_chat_different_channels_allowed(db_session: Session) -> None:
    db_session.add_all([_tenant("t1"), _workspace("w1", "t1")])
    db_session.flush()
    db_session.add(_thread("a"))
    other = _thread("b")
    other.channel_type = "max_dm"
    db_session.add(other)
    db_session.flush()  # no IntegrityError


# ---------------------------------------------------------------------------
# FC-2: MenuPlanItem unique (svc-housewife #2)
# ---------------------------------------------------------------------------


def _menu_plan() -> MenuPlan:
    return MenuPlan(
        id="plan_1",
        tenant_id="t1",
        user_id="u1",
        week_start_date=date(2026, 7, 13),
        status="active",
    )


def _item(item_id: str) -> MenuPlanItem:
    return MenuPlanItem(
        id=item_id,
        menu_plan_id="plan_1",
        tenant_id="t1",
        day_of_week=0,
        meal_type="breakfast",
        free_text="каша",
    )


def test_menu_plan_items_unique_slot(db_session: Session) -> None:
    db_session.add_all([_tenant("t1")])
    db_session.flush()
    db_session.add(User(id="u1", tenant_id="t1"))
    db_session.flush()
    db_session.add(_menu_plan())
    db_session.flush()
    db_session.add(_item("mpi_1"))
    db_session.flush()
    db_session.add(_item("mpi_2"))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# db-migrations #2/#3: parity create_all ↔ миграции (free_tier, signup)
# ---------------------------------------------------------------------------


def test_free_tier_unique_matches_migration_0024(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    index_names = {ix["name"] for ix in inspector.get_indexes("free_tier_usage")}
    assert "ix_free_tier_usage_unique" in index_names

    db_session.add(
        FreeTierUsage(
            id="ft_1", tenant_id="t1", user_id="u1",
            day=date(2026, 7, 18), llm_calls=1, updated_at=_now(),
        )
    )
    db_session.flush()
    db_session.add(
        FreeTierUsage(
            id="ft_2", tenant_id="t1", user_id="u1",
            day=date(2026, 7, 18), llm_calls=1, updated_at=_now(),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_signup_attempts_lookup_index_matches_migration_0041(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    index_names = {ix["name"] for ix in inspector.get_indexes("signup_attempts")}
    assert "ix_signup_attempts_lookup" in index_names


# ---------------------------------------------------------------------------
# db-migrations #4: пакетный экспорт моделей (autogenerate-полнота)
# ---------------------------------------------------------------------------


def test_models_package_exports_previously_missing_models() -> None:
    for name in ("AuditLog", "MemoryCategory", "FreeTierUsage", "ReplyButtonCache"):
        assert name in sreda_models.__all__, f"{name} не в __all__"
        assert getattr(sreda_models, name) is not None


# ---------------------------------------------------------------------------
# Миграция 20260718_0085: линковка + дедуп/префлайт/шифрование хелперы
# ---------------------------------------------------------------------------


def test_0085_revision_chain() -> None:
    mig = _load_migration_0085()
    assert mig.revision == "20260718_0085"
    assert mig.down_revision == "20260711_0084"


def _create_message_jobs_scratch(conn) -> None:
    conn.execute(text(
        """
        CREATE TABLE message_jobs (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            bot_key TEXT NOT NULL,
            external_update_id TEXT NOT NULL,
            enqueued_at TEXT NOT NULL,
            message_payload TEXT
        )
        """
    ))


def test_0085_dedup_message_jobs_keeps_earliest() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        _create_message_jobs_scratch(conn)
        # Три дубля одного (channel, bot_key, update): выживает самая ранняя
        # enqueued_at (id случайный — порядок по id недетерминирован).
        conn.execute(text(
            "INSERT INTO message_jobs VALUES"
            " ('job_c', 'telegram', 'sreda', '42', '2026-07-18 10:02:00', '{}'),"
            " ('job_a', 'telegram', 'sreda', '42', '2026-07-18 10:00:00', '{}'),"
            " ('job_b', 'telegram', 'sreda', '42', '2026-07-18 10:01:00', '{}'),"
            " ('job_other_bot', 'telegram', 'sreda_home', '42', '2026-07-18 09:00:00', '{}')"
        ))
        doomed = mig._dedup_message_jobs(conn)
        assert doomed == 2
        survivors = {
            r[0] for r in conn.execute(text("SELECT id FROM message_jobs")).all()
        }
        assert survivors == {"job_a", "job_other_bot"}
        archived = {
            r[0] for r in conn.execute(
                text("SELECT id FROM message_jobs_dedup_archive_0085")
            ).all()
        }
        assert archived == {"job_b", "job_c"}


def test_0085_dedup_message_jobs_no_dupes_no_archive() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        _create_message_jobs_scratch(conn)
        conn.execute(text(
            "INSERT INTO message_jobs VALUES"
            " ('job_a', 'telegram', 'sreda', '42', '2026-07-18 10:00:00', '{}')"
        ))
        assert mig._dedup_message_jobs(conn) == 0
        # Здоровая БД не засоряется пустой архивной таблицей (образец 0048).
        names = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }
        assert "message_jobs_dedup_archive_0085" not in names


def test_0085_dedup_menu_plan_items() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE menu_plan_items (
                id TEXT PRIMARY KEY,
                menu_plan_id TEXT NOT NULL,
                day_of_week INTEGER NOT NULL,
                meal_type TEXT NOT NULL,
                free_text TEXT
            )
            """
        ))
        conn.execute(text(
            "INSERT INTO menu_plan_items VALUES"
            " ('mpi_a', 'plan_1', 0, 'breakfast', 'каша'),"
            " ('mpi_b', 'plan_1', 0, 'breakfast', 'омлет'),"
            " ('mpi_c', 'plan_1', 1, 'breakfast', 'блин')"
        ))
        assert mig._dedup_menu_plan_items(conn) == 1
        survivors = {
            r[0] for r in conn.execute(text("SELECT id FROM menu_plan_items")).all()
        }
        assert survivors == {"mpi_a", "mpi_c"}
        archived = {
            r[0]
            for r in conn.execute(
                text("SELECT id FROM menu_plan_items_dedup_archive_0085")
            ).all()
        }
        assert archived == {"mpi_b"}


def test_0085_dedup_agent_threads_repoints_references() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE agent_threads (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        ))
        conn.execute(text(
            "CREATE TABLE conversation_turns (id TEXT PRIMARY KEY, thread_id TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE agent_runs (id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT)"
        ))
        # thr_old (выжившая) и thr_new (дубль) — одна тройка.
        conn.execute(text(
            "INSERT INTO agent_threads VALUES"
            " ('thr_new', 't1', 'telegram', 'chat_1', '2026-07-10 00:00:00', NULL),"
            " ('thr_old', 't1', 'telegram', 'chat_1', '2026-07-01 00:00:00', NULL),"
            " ('thr_unrelated', 't1', 'max_dm', 'chat_1', '2026-07-05 00:00:00', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO conversation_turns VALUES"
            " ('turn_1', 'thr_new'),"
            " ('turn_2', 'thr_old'),"
            " ('turn_3', 'thr_unrelated')"
        ))
        conn.execute(text(
            "INSERT INTO agent_runs VALUES"
            " ('run_1', 'thr_new', 'turn_1'),"
            " ('run_2', 'thr_unrelated', 'turn_3')"
        ))
        assert mig._dedup_agent_threads(conn) == 1

        threads = {
            r[0] for r in conn.execute(text("SELECT id FROM agent_threads")).all()
        }
        assert threads == {"thr_old", "thr_unrelated"}
        turns = dict(
            conn.execute(text("SELECT id, thread_id FROM conversation_turns")).all()
        )
        assert turns == {
            "turn_1": "thr_old",       # repoint doomed → survivor
            "turn_2": "thr_old",       # уже на выжившей
            "turn_3": "thr_unrelated", # не тронут
        }
        runs = dict(
            conn.execute(text("SELECT id, thread_id FROM agent_runs")).all()
        )
        assert runs == {"run_1": "thr_old", "run_2": "thr_unrelated"}
        archived = {
            r[0]
            for r in conn.execute(
                text("SELECT id FROM agent_threads_dedup_archive_0085")
            ).all()
        }
        assert archived == {"thr_new"}


def test_0085_users_preflight_blocks_max_account_duplicates() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY, max_account_id TEXT)"))
        conn.execute(text(
            "INSERT INTO users VALUES ('u1', 'max_1'), ('u2', 'max_1'), ('u3', NULL), ('u4', NULL)"
        ))
        with pytest.raises(RuntimeError, match="preflight"):
            mig._preflight_users_max_account_id(conn)


def test_0085_users_preflight_passes_on_distinct() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY, max_account_id TEXT)"))
        conn.execute(text(
            "INSERT INTO users VALUES ('u1', 'max_1'), ('u2', 'max_2'), ('u3', NULL)"
        ))
        mig._preflight_users_max_account_id(conn)  # не падает


def test_0085_encrypt_existing_payloads() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        _create_message_jobs_scratch(conn)
        conn.execute(text(
            "INSERT INTO message_jobs VALUES"
            " ('job_plain', 'telegram', 'sreda', '42', '2026-07-18 10:00:00',"
            "  '{\"text\": \"секрет\"}')"
        ))
        assert mig._encrypt_existing_payloads(conn) == 1
        raw = conn.execute(
            text("SELECT message_payload FROM message_jobs WHERE id = 'job_plain'")
        ).scalar()
        assert str(raw).startswith(("v1:", "v2:"))
        assert "секрет" not in str(raw)
        # Идемпотентность: повторный прогон не трогает уже зашифрованное.
        assert mig._encrypt_existing_payloads(conn) == 0
        # Roundtrip через decrypt — обратно исходный JSON.
        from sreda.services.encryption import decrypt_value

        assert decrypt_value(str(raw)) == '{"text": "секрет"}'


def test_0085_encrypt_empty_table_needs_no_key(monkeypatch) -> None:
    """Пустая таблица = скип БЕЗ импорта encryption — миграция не требует
    SREDA_ENCRYPTION_KEY на пустом контуре (очередь за фичефлагом)."""
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        _create_message_jobs_scratch(conn)
        monkeypatch.delenv("SREDA_ENCRYPTION_KEY", raising=False)
        from sreda.config.settings import get_settings
        from sreda.services.encryption import get_encryption_service

        get_settings.cache_clear()
        get_encryption_service.cache_clear()
        assert mig._encrypt_existing_payloads(conn) == 0


def test_0085_decrypt_existing_payloads() -> None:
    mig = _load_migration_0085()
    engine = _scratch_engine()
    with engine.begin() as conn:
        _create_message_jobs_scratch(conn)
        from sreda.services.encryption import encrypt_value

        enc = encrypt_value('{"text": "секрет"}')
        conn.execute(
            text(
                "INSERT INTO message_jobs VALUES"
                " ('job_enc', 'telegram', 'sreda', '42', '2026-07-18 10:00:00', :enc),"
                " ('job_plain', 'telegram', 'sreda', '43', '2026-07-18 10:01:00', '{}')"
            ),
            {"enc": enc},
        )
        assert mig._decrypt_existing_payloads(conn) == 1
        rows = dict(
            conn.execute(text("SELECT id, message_payload FROM message_jobs")).all()
        )
        assert rows["job_enc"] == '{"text": "секрет"}'
        assert rows["job_plain"] == "{}"  # plaintext не тронут
