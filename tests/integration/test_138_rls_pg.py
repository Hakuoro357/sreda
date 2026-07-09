"""#138 Ф3-d: integration red-suite изоляции тенантов на РЕАЛЬНОМ Postgres.

Тестируем МЕХАНИЗМ БД (RLS-политики миграции 0082 + роли/гранты 0078), а не
app-шов: соединения напрямую через SQLAlchemy/psycopg под ролями
sreda_app / sreda_maintenance, контекст тенанта — сырой
``SELECT set_config('sreda.tenant_id', <id>, true)``.

Ассерты — на ИСХОД (видимость строк / rowcount / отказ), не на текст политики
(g-057).

Запуск требует реального PG с прогнанной цепочкой миграций и тестовыми
LOGIN-ролями (на проде роли NOLOGIN; для тестов создаются с LOGIN):

    CREATE ROLE sreda_app LOGIN PASSWORD 'app'
        NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    -- аналогично sreda_identity / sreda_maintenance

DSN-ы через env (нет env → skip, unit-прогоны не трогаются):

    SREDA_PG_TEST_DSN_OWNER=postgresql+psycopg://postgres:...@localhost:15432/sreda_test
    SREDA_PG_TEST_DSN_APP=postgresql+psycopg://sreda_app:app@localhost:15432/sreda_test
    SREDA_PG_TEST_DSN_MAINT=postgresql+psycopg://sreda_maintenance:maintenance@...
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.pg

_DSN_OWNER = os.environ.get("SREDA_PG_TEST_DSN_OWNER")
_DSN_APP = os.environ.get("SREDA_PG_TEST_DSN_APP")
_DSN_MAINT = os.environ.get("SREDA_PG_TEST_DSN_MAINT")

if not (_DSN_OWNER and _DSN_APP and _DSN_MAINT):
    pytest.skip(
        "SREDA_PG_TEST_DSN_OWNER/_APP/_MAINT не заданы — red-suite RLS "
        "бежит только против реального Postgres (#138 Ф3-d)",
        allow_module_level=True,
    )

# ── Сид: детерминированные id (идемпотентный пересев владельцем) ──────────

TEN_A = "rls-red-ten-a"
TEN_B = "rls-red-ten-b"

USER_A = "rls-red-user-a"
USER_B = "rls-red-user-b"

REM_A = "rls-red-rem-a"
REM_B = "rls-red-rem-b"

CL_A = "rls-red-cl-a"
CL_B = "rls-red-cl-b"

CLI_A = "rls-red-cli-a"
CLI_B = "rls-red-cli-b"

THREAD_A = "rls-red-thread-a"
THREAD_B = "rls-red-thread-b"

#: (таблица, колонка tenant-скоупа) для счётчиков видимости.
VISIBILITY_TABLES = (
    "family_reminders",
    "checklists",
    "checklist_items",
    "react_checkpoint",
)


def _seed(owner_engine) -> None:
    """Пересев владельцем (bypass RLS): 2 тенанта + users + по строке данных."""
    now = datetime.now(timezone.utc)
    with owner_engine.begin() as conn:
        # Чистим в обратном FK-порядке (идемпотентность повторных прогонов).
        conn.execute(
            text("DELETE FROM react_checkpoint WHERE thread_id IN (:a, :b)"),
            {"a": THREAD_A, "b": THREAD_B},
        )
        conn.execute(
            text("DELETE FROM checklist_items WHERE id IN (:a, :b)"),
            {"a": CLI_A, "b": CLI_B},
        )
        conn.execute(
            text("DELETE FROM checklists WHERE id IN (:a, :b)"),
            {"a": CL_A, "b": CL_B},
        )
        conn.execute(
            text("DELETE FROM family_reminders WHERE id IN (:a, :b)"),
            {"a": REM_A, "b": REM_B},
        )
        conn.execute(
            text("DELETE FROM users WHERE id IN (:a, :b)"),
            {"a": USER_A, "b": USER_B},
        )
        conn.execute(
            text("DELETE FROM tenants WHERE id IN (:a, :b)"),
            {"a": TEN_A, "b": TEN_B},
        )

        for ten, user, rem, cl, cli, thread in (
            (TEN_A, USER_A, REM_A, CL_A, CLI_A, THREAD_A),
            (TEN_B, USER_B, REM_B, CL_B, CLI_B, THREAD_B),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, name, created_at) "
                    "VALUES (:id, :name, :now)"
                ),
                {"id": ten, "name": f"red-suite {ten}", "now": now},
            )
            conn.execute(
                text("INSERT INTO users (id, tenant_id) VALUES (:id, :ten)"),
                {"id": user, "ten": ten},
            )
            conn.execute(
                text(
                    "INSERT INTO family_reminders "
                    "(id, tenant_id, user_id, title, trigger_at, status, "
                    " created_at, updated_at) "
                    "VALUES (:id, :ten, :user, :title, :now, 'scheduled', "
                    "        :now, :now)"
                ),
                {
                    "id": rem,
                    "ten": ten,
                    "user": user,
                    "title": f"reminder {ten}",
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO checklists (id, tenant_id, user_id, title) "
                    "VALUES (:id, :ten, :user, :title)"
                ),
                {"id": cl, "ten": ten, "user": user, "title": f"cl {ten}"},
            )
            conn.execute(
                text(
                    "INSERT INTO checklist_items "
                    "(id, checklist_id, tenant_id, title) "
                    "VALUES (:id, :cl, :ten, :title)"
                ),
                {"id": cli, "cl": cl, "ten": ten, "title": f"item {ten}"},
            )
            conn.execute(
                text(
                    "INSERT INTO react_checkpoint "
                    "(thread_id, checkpoint_ns, checkpoint_id, "
                    " checkpoint_type, blob, metadata_type, metadata, "
                    " created_at, tenant_id) "
                    "VALUES (:thread, '', :cp_id, 'msgpack', :blob, "
                    "        'msgpack', :meta, :now, :ten)"
                ),
                {
                    "thread": thread,
                    "cp_id": f"cp-{ten}",
                    "blob": b"x",
                    "meta": b"x",
                    "now": now,
                    "ten": ten,
                },
            )


@pytest.fixture(scope="module")
def engines():
    owner = create_engine(_DSN_OWNER, poolclass=NullPool)
    app = create_engine(_DSN_APP, poolclass=NullPool)
    maint = create_engine(_DSN_MAINT, poolclass=NullPool)
    _seed(owner)
    yield {"owner": owner, "app": app, "maint": maint}
    owner.dispose()
    app.dispose()
    maint.dispose()


def _set_tenant(conn, tenant_id: str) -> None:
    """Сырой set_config, is_local=true — контекст живёт до конца транзакции."""
    conn.execute(
        text("SELECT set_config('sreda.tenant_id', :ten, true)"),
        {"ten": tenant_id},
    )


def _count(conn, table: str, tenant_id: str) -> int:
    # Имена таблиц — из статического кортежа модуля, не пользовательский ввод.
    return conn.execute(
        text(f"SELECT count(*) FROM {table} WHERE tenant_id = :ten"),  # noqa: S608
        {"ten": tenant_id},
    ).scalar_one()


# ── 1. app + set_config → виден ТОЛЬКО свой тенант ─────────────────────────


def test_app_with_tenant_context_sees_only_own_rows(engines):
    with engines["app"].connect() as conn:
        with conn.begin():
            _set_tenant(conn, TEN_A)
            for table in VISIBILITY_TABLES:
                assert _count(conn, table, TEN_A) == 1, (
                    f"{table}: строка тенанта A обязана быть видна под A"
                )
                assert _count(conn, table, TEN_B) == 0, (
                    f"{table}: строка тенанта B ПРОТЕКЛА под контекст A"
                )


# ── 2. app БЕЗ set_config → fail-closed, 0 строк ───────────────────────────


def test_app_without_tenant_context_sees_nothing(engines):
    with engines["app"].connect() as conn:
        with conn.begin():
            for table in VISIBILITY_TABLES:
                total = conn.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                ).scalar_one()
                assert total == 0, (
                    f"{table}: без sreda.tenant_id app видит {total} строк — "
                    f"fail-closed нарушен"
                )


# ── 3. INSERT с чужим tenant_id → отказ (WITH CHECK); со своим → ок ────────


def test_app_insert_foreign_tenant_rejected_own_ok(engines):
    now = datetime.now(timezone.utc)

    def _insert(conn, tenant_id: str) -> None:
        conn.execute(
            text(
                "INSERT INTO family_reminders "
                "(id, tenant_id, title, trigger_at, status, "
                " created_at, updated_at) "
                "VALUES (:id, :ten, 'smuggled', :now, 'scheduled', :now, :now)"
            ),
            {"id": f"rls-red-ins-{uuid.uuid4().hex[:8]}", "ten": tenant_id, "now": now},
        )

    with engines["app"].connect() as conn:
        # Чужой tenant_id — политика WITH CHECK обязана отбить.
        with pytest.raises(DBAPIError, match="row-level security"):
            with conn.begin():
                _set_tenant(conn, TEN_A)
                _insert(conn, TEN_B)

        # Свой tenant_id — проходит (и откатывается, сид не портим).
        with conn.begin() as txn:
            _set_tenant(conn, TEN_A)
            _insert(conn, TEN_A)
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM family_reminders "
                        "WHERE title = 'smuggled' AND tenant_id = :ten"
                    ),
                    {"ten": TEN_A},
                ).scalar_one()
                == 1
            )
            txn.rollback()


# ── 4. UPDATE/DELETE чужих строк → 0 rowcount (невидимы) ───────────────────


def test_app_update_delete_foreign_rows_noop(engines):
    with engines["app"].connect() as conn:
        with conn.begin() as txn:
            _set_tenant(conn, TEN_A)

            upd = conn.execute(
                text(
                    "UPDATE family_reminders SET title = 'hacked' "
                    "WHERE id = :id"
                ),
                {"id": REM_B},
            )
            assert upd.rowcount == 0, "UPDATE чужой строки обязан задеть 0 строк"

            dele = conn.execute(
                text("DELETE FROM checklist_items WHERE id = :id"),
                {"id": CLI_B},
            )
            assert dele.rowcount == 0, "DELETE чужой строки обязан задеть 0 строк"
            txn.rollback()

    # Строка B физически цела (проверка владельцем, мимо RLS).
    with engines["owner"].connect() as conn:
        title = conn.execute(
            text("SELECT title FROM family_reminders WHERE id = :id"),
            {"id": REM_B},
        ).scalar_one()
        assert title == f"reminder {TEN_B}"


# ── 5. tenants: self-select — ровно СВОЯ строка ────────────────────────────


def test_app_tenants_self_select(engines):
    with engines["app"].connect() as conn:
        with conn.begin():
            _set_tenant(conn, TEN_A)
            rows = conn.execute(
                text("SELECT id FROM tenants WHERE id IN (:a, :b)"),
                {"a": TEN_A, "b": TEN_B},
            ).scalars().all()
            assert rows == [TEN_A], (
                f"app под A обязан видеть ровно свою строку tenants, видит: {rows}"
            )


# ── 6. checklist_items: свой tenant_id + чужой checklist → composite-FK ────


def test_app_checklist_item_cross_tenant_parent_rejected(engines):
    with engines["app"].connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            with conn.begin():
                _set_tenant(conn, TEN_A)
                conn.execute(
                    text(
                        "INSERT INTO checklist_items "
                        "(id, checklist_id, tenant_id, title) "
                        "VALUES (:id, :cl, :ten, 'cross-tenant')"
                    ),
                    {
                        "id": f"rls-red-x-{uuid.uuid4().hex[:8]}",
                        "cl": CL_B,  # чеклист тенанта B
                        "ten": TEN_A,  # свой tenant_id — WITH CHECK пройдёт
                    },
                )
        # Отбивает composite-FK (checklist_id, tenant_id) → checklists.
        assert "fk_checklist_items_parent_tenant" in str(exc_info.value)


# ── 7. maintenance: видит оба тенанта БЕЗ set_config ───────────────────────


def test_maintenance_sees_all_tenants_without_context(engines):
    with engines["maint"].connect() as conn:
        with conn.begin():
            for table in VISIBILITY_TABLES:
                assert _count(conn, table, TEN_A) == 1, f"{table}: A под maintenance"
                assert _count(conn, table, TEN_B) == 1, f"{table}: B под maintenance"
            tenants = conn.execute(
                text("SELECT count(*) FROM tenants WHERE id IN (:a, :b)"),
                {"a": TEN_A, "b": TEN_B},
            ).scalar_one()
            assert tenants == 2


# ── 8. pool-reuse: is_local=true сбрасывается на границе транзакции ────────


def test_tenant_context_does_not_leak_across_transactions(engines):
    with engines["app"].connect() as conn:
        # txn1: контекст A установлен → строки A видны.
        with conn.begin():
            _set_tenant(conn, TEN_A)
            assert _count(conn, "family_reminders", TEN_A) == 1

        # txn2 на ТОЙ ЖЕ connection, БЕЗ set_config: is_local=true обязан
        # был сброситься на commit → fail-closed.
        with conn.begin():
            for table in VISIBILITY_TABLES:
                total = conn.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                ).scalar_one()
                assert total == 0, (
                    f"{table}: tenant-контекст ПРОТЁК в следующую транзакцию "
                    f"той же connection ({total} строк видно)"
                )


# ── 9. react_checkpoint: чужой диалог закрыт ───────────────────────────────


def test_app_react_checkpoint_foreign_thread_invisible(engines):
    with engines["app"].connect() as conn:
        with conn.begin():
            _set_tenant(conn, TEN_A)
            own = conn.execute(
                text("SELECT count(*) FROM react_checkpoint WHERE thread_id = :t"),
                {"t": THREAD_A},
            ).scalar_one()
            foreign = conn.execute(
                text("SELECT count(*) FROM react_checkpoint WHERE thread_id = :t"),
                {"t": THREAD_B},
            ).scalar_one()
            assert own == 1, "свой чекпоинт обязан быть виден"
            assert foreign == 0, "чекпоинт чужого тенанта ПРОТЁК (диалог не закрыт)"


def test_pg_policies_parity_with_app_registry(engines):
    """#138 R1 M8: parity реестр ⇔ факт БД. Каждая TENANT-таблица app-реестра ОБЯЗАНА
    иметь на реальном PG политику p_{t}_tenant И relrowsecurity=true; NO_RLS-таблица —
    НЕ иметь RLS. Ловит «добавил в rls_registry, забыл миграцию ENABLE RLS» (и наоборот)."""
    from sreda.db import rls_registry as reg

    owner = engines["owner"]
    with owner.connect() as c:
        rls_on = {
            r[0] for r in c.execute(text(
                "SELECT relname FROM pg_class WHERE relrowsecurity = true"
            ))
        }
        policied = {
            r[0] for r in c.execute(text(
                "SELECT tablename FROM pg_policies WHERE policyname LIKE 'p\_%\_tenant'"
            ))
        }

    tenant = set(reg.TENANT_TABLES)
    # (1) каждая tenant-таблица: RLS включён + есть tenant-политика.
    assert tenant - rls_on == set(), f"TENANT-таблицы БЕЗ ENABLE RLS на PG: {sorted(tenant - rls_on)}"
    assert tenant - policied == set(), f"TENANT-таблицы БЕЗ политики p_*_tenant: {sorted(tenant - policied)}"
    # (2) NO_RLS-таблицы реально без RLS (осознанное исключение не должно молча включиться).
    assert reg.NO_RLS_TABLES & rls_on == set(), (
        f"NO_RLS-таблицы с внезапным ENABLE RLS: {sorted(reg.NO_RLS_TABLES & rls_on)}"
    )
