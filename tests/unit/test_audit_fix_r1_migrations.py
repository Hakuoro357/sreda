"""R1-фиксы аудита 2026-07-18, область W1 (миграция 20260718_0085).

Покрывает находки decision-log R1:

- C1  — архив message_jobs копирует payload ПОСЛЕ шифрования (не plaintext PII):
        порядок upgrade() = ALTER→encrypt→dedup/archive.
- C2  — 6-табличный EXCLUSIVE-лок берётся ПОСЛЕ шифрования (source-order guard;
        поведение локов no-op на SQLite).
- M1  — downgrade-префлайт коллизий (channel, external_update_id) между bot_key.
- M2  — перенумерация turn_seq doomed-турнов снимает коллизию
        UNIQUE(thread_id, turn_seq) при repoint на survivor.
- M22 — _preflight_users_max_account_id печатает sha256-префиксы, не сырые id.

Уровень миграции — прямые вызовы bind-хелперов на scratch-схеме SQLite
(репо-прецедент test_audit_fix_db_uniques). PG-only ветки (LOCK TABLE,
FK drop/recreate) диалектно пропускаются внутри хелперов.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def _load_migration_0085():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations" / "versions" / "20260718_0085_dedup_unique_hardening.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0085_r1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mj_scratch(conn) -> None:
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


_TWO_DUPES = (
    "INSERT INTO message_jobs VALUES"
    " ('job_survivor','telegram','sreda','42','2026-07-18 10:00:00',"
    "  '{\"text\": \"секрет-выжившего\"}'),"
    " ('job_doomed','telegram','sreda','42','2026-07-18 10:05:00',"
    "  '{\"text\": \"секрет-дубля\"}')"
)


# ---------------------------------------------------------------------------
# C1 — архив дублей содержит ШИФРОТЕКСТ (шифрование ДО дедуп/архива)
# ---------------------------------------------------------------------------


def test_c1_encrypt_before_dedup_archive_is_ciphertext() -> None:
    mig = _load_migration_0085()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _mj_scratch(conn)
        conn.execute(text(_TWO_DUPES))
        # Порядок upgrade() (R1 C1): сначала шифруем, потом дедуп/архив.
        mig._encrypt_existing_payloads(conn)
        assert mig._dedup_message_jobs(conn) == 1

        archived = conn.execute(text(
            "SELECT message_payload FROM message_jobs_dedup_archive_0085"
        )).scalars().all()
        assert archived, "архивная строка дубля должна существовать"
        for payload in archived:
            assert str(payload).startswith(("v1:", "v2:")), (
                f"архив содержит plaintext: {str(payload)[:40]!r}"
            )
            assert "секрет" not in str(payload)


def test_c1_wrong_order_would_leave_plaintext_in_archive() -> None:
    """Документирует баг, который чинит переупорядочивание: дедуп/архив ДО
    шифрования оставляет plaintext PII в архивной таблице навсегда."""
    mig = _load_migration_0085()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _mj_scratch(conn)
        conn.execute(text(_TWO_DUPES))
        # Неправильный порядок: архив создаётся из plaintext, шифрование
        # трогает только основную таблицу (не архив).
        mig._dedup_message_jobs(conn)
        mig._encrypt_existing_payloads(conn)

        archived = conn.execute(text(
            "SELECT message_payload FROM message_jobs_dedup_archive_0085"
        )).scalars().all()
        assert any("секрет" in str(p) for p in archived), (
            "ожидали plaintext в архиве при неправильном порядке"
        )


# ---------------------------------------------------------------------------
# C1+C2 — source-order инварианты upgrade() (лок-поведение no-op на SQLite)
# ---------------------------------------------------------------------------


def test_upgrade_orders_encrypt_before_dedup_and_lock_after() -> None:
    mig = _load_migration_0085()
    src = inspect.getsource(mig.upgrade)
    # Матчим формы ВЫЗОВОВ (…(bind) / op.add_column(), а не упоминания имён в
    # комментариях) — иначе комментарий-объяснение сдвинул бы индексы.
    i_timeout = src.index("_set_lock_timeout(bind)")
    i_addcol = src.index("op.add_column(")
    i_encrypt = src.index("_encrypt_existing_payloads(bind)")
    i_dedup = src.index("_dedup_message_jobs(bind)")
    i_lock = src.index("_lock_dedup_tables(bind)")

    # C1: шифрование payload'ов ДО дедуп/архива message_jobs.
    assert i_encrypt < i_dedup, "encrypt должен идти до dedup (C1)"
    # C2: 6-табличный EXCLUSIVE-лок берётся ПОСЛЕ шифрования (не держит
    # conversation_turns/agent_runs на время per-row перешифровки).
    assert i_encrypt < i_lock, "lock 6 таблиц должен идти после encrypt (C2)"
    # lock_timeout ставится в самом начале — ДО первого DDL (add_column),
    # чтобы ACCESS EXCLUSIVE от ALTER тоже был fail-fast.
    assert i_timeout < i_addcol, "lock_timeout — до первого DDL"


# ---------------------------------------------------------------------------
# M1 — downgrade-префлайт коллизий (channel, external_update_id) x bot_key
# ---------------------------------------------------------------------------


def test_m1_downgrade_preflight_blocks_multibot_collision() -> None:
    mig = _load_migration_0085()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _mj_scratch(conn)
        conn.execute(text(
            "INSERT INTO message_jobs VALUES"
            " ('j1','telegram','sreda','42','2026-07-18 10:00:00','{}'),"
            " ('j2','telegram','sreda_home','42','2026-07-18 10:01:00','{}')"
        ))
        with pytest.raises(RuntimeError, match="downgrade preflight"):
            mig._preflight_downgrade_message_jobs_unique(conn)


def test_m1_downgrade_preflight_passes_without_collision() -> None:
    mig = _load_migration_0085()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _mj_scratch(conn)
        conn.execute(text(
            "INSERT INTO message_jobs VALUES"
            " ('j1','telegram','sreda','42','2026-07-18 10:00:00','{}'),"
            " ('j2','telegram','sreda','43','2026-07-18 10:01:00','{}'),"
            " ('j3','max_dm','sreda','42','2026-07-18 10:02:00','{}')"
        ))
        # Разные (channel, update_id) пары — коллизии старого UNIQUE нет.
        mig._preflight_downgrade_message_jobs_unique(conn)  # не падает


# ---------------------------------------------------------------------------
# M2 — перенумерация turn_seq снимает коллизию UNIQUE(thread_id, turn_seq)
# ---------------------------------------------------------------------------


def _threads_scratch(conn) -> None:
    conn.execute(text(
        """
        CREATE TABLE agent_threads (
            id TEXT PRIMARY KEY, tenant_id TEXT, channel_type TEXT,
            external_chat_id TEXT, created_at TEXT, updated_at TEXT
        )
        """
    ))
    conn.execute(text(
        "CREATE TABLE conversation_turns ("
        " id TEXT PRIMARY KEY, thread_id TEXT, turn_seq INTEGER,"
        " UNIQUE (thread_id, turn_seq))"
    ))
    conn.execute(text(
        "CREATE TABLE agent_runs (id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT)"
    ))


def test_m2_renumber_avoids_turn_seq_collision_on_merge() -> None:
    mig = _load_migration_0085()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _threads_scratch(conn)
        conn.execute(text(
            "INSERT INTO agent_threads VALUES"
            " ('surv','t1','telegram','chat','2026-07-01 00:00:00',NULL),"
            " ('doom','t1','telegram','chat','2026-07-10 00:00:00',NULL)"
        ))
        # survivor: seq 1,2,3 (max 3); doomed: seq 1,2 — оба стартуют с 1.
        conn.execute(text(
            "INSERT INTO conversation_turns VALUES"
            " ('s1','surv',1),('s2','surv',2),('s3','surv',3),"
            " ('d1','doom',1),('d2','doom',2)"
        ))
        conn.execute(text(
            "INSERT INTO agent_runs VALUES ('r1','doom','d1')"
        ))

        # Без перенумерации repoint d1/d2→surv упал бы на UNIQUE — а тут проходит.
        assert mig._dedup_agent_threads(conn) == 1

        rows = dict(conn.execute(
            text("SELECT id, thread_id FROM conversation_turns")
        ).all())
        assert rows == {
            "s1": "surv", "s2": "surv", "s3": "surv",
            "d1": "surv", "d2": "surv",
        }
        seqs = dict(conn.execute(
            text("SELECT id, turn_seq FROM conversation_turns")
        ).all())
        # survivor не тронут; doomed сдвинут на survivor_max(3)+1 → 5,6.
        assert seqs["s1"] == 1 and seqs["s2"] == 2 and seqs["s3"] == 3
        assert seqs["d1"] == 5 and seqs["d2"] == 6
        # (thread_id, turn_seq) остался уникальным на survivor.
        surv_seqs = sorted(
            r[0] for r in conn.execute(text(
                "SELECT turn_seq FROM conversation_turns WHERE thread_id='surv'"
            )).all()
        )
        assert surv_seqs == [1, 2, 3, 5, 6]
        # agent_runs репойнтнут на survivor.
        assert conn.execute(
            text("SELECT thread_id FROM agent_runs WHERE id='r1'")
        ).scalar() == "surv"


# ---------------------------------------------------------------------------
# M22 — префлайт users печатает sha256-префиксы, не сырые max_account_id
# ---------------------------------------------------------------------------


def test_m22_users_preflight_hashes_ids_not_raw() -> None:
    mig = _load_migration_0085()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users (id TEXT PRIMARY KEY, max_account_id TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO users VALUES"
            " ('u1','max_secret_777'),('u2','max_secret_777')"
        ))
        with pytest.raises(RuntimeError) as ei:
            mig._preflight_users_max_account_id(conn)

    msg = str(ei.value)
    # Сырой id НЕ утёк в deploy-лог.
    assert "max_secret_777" not in msg
    # Печатается sha256-префикс (8 симв.) для сопоставления.
    assert "sha256" in msg
    expected_prefix = hashlib.sha256("max_secret_777".encode("utf-8")).hexdigest()[:8]
    assert expected_prefix in msg
