"""dedup-unique hardening: message_jobs +bot_key + payload-шифрование, users/agent_threads/menu_plan_items uniques

Аудит 2026-07-18 (db-migrations #1/#2, cross-concurrency FC-2, cross-security N1,
svc-inbound #1, runtime-core #5, svc-housewife #2). Класс дефекта: app-level
дедуп-ключ без БД-уровневой уникальности → check-then-insert гонки и дубли,
необратимые для `one_or_none()`-читателей. Эталон оформления — миграция
20260603_0048 (inbound_dedup_composite_key): EXCLUSIVE-лок, архив дублей в
БД, NULL-safe EXISTS-предикат, transactional DDL (атомарно с revision-стемпом).

Что делает миграция:

1. ``message_jobs`` — расширение дедуп-ключа до ``(channel, external_update_id,
   bot_key)`` (db-migrations #1): счётчики Telegram ``update_id`` независимы
   per-bot, поэтому update 42 бота ``sreda`` и update 42 бота ``sreda_home`` —
   РАЗНЫЕ события; старый ключ ``(channel, external_update_id)`` принимал второе
   за дубль и молча терял сообщение (тот же класс, что закрыт для
   ``inbound_messages`` миграцией 0048). Шаги: ``+bot_key NOT NULL DEFAULT
   'sreda'`` (legacy single-bot backfill бесплатен), дедуп по новому ключу
   (на здоровой БД вакуусен — старый UNIQUE ещё активен и дубли невозможны;
   шаг оборонительный), замена констрейнта. Старый продюсер без bot_key
   продолжает писать (server_default) — откат кода безопасен.
2. ``message_jobs.message_payload`` — JSONB → Text с ``USING ::text`` и
   пошаговым шифрованием существующих plaintext-строк через ``encrypt_value()``
   (cross-security N1: колонка хранит ПОЛНЫЙ raw webhook update — PII; паттерн
   0028/0029, идемпотентно по envelope-префиксу). Пустая таблица = шаг скипается,
   ``SREDA_ENCRYPTION_KEY`` не требуется.
3. ``users.max_account_id`` — partial unique index (IS NOT NULL). Дубли юзеров
   авто-слиянию НЕ подлежат (у пользователя memories/профили/напоминания —
   merge только руками), поэтому префлайт: коллизии → RuntimeError со списком
   (deploy останавливается громко, данных не портит).
4. ``agent_threads`` — дедуп (архив + repoint ``conversation_turns.thread_id``
   и ``agent_runs.thread_id`` на выжившую строку) → UNIQUE
   ``(tenant_id, channel_type, external_chat_id)`` (runtime-core #5).
5. ``menu_plan_items`` — дедуп (архив + delete; FK-ссылок на id нет) → UNIQUE
   ``(menu_plan_id, day_of_week, meal_type)`` (svc-housewife #2).

``free_tier_usage`` DDL НЕ требуется: unique ``ix_free_tier_usage_unique``
существует с миграции 0024 — там синхронизирована только ORM-модель
(db-migrations #2).

Лок-профиль: таблицы малы (queue — за фичефлагом, threads/plans — сотни строк),
обычный transactional CREATE INDEX/CONSTRAINT (НЕ CONCURRENTLY) атомарен с
revision-стемпом (обоснование — docstring 0084). EXCLUSIVE-лок на время
дедуп-окна по образцу 0048, ``lock_timeout 5s`` — fail-fast вместо подвисания.

Downgrade обращает DDL (индексы/констрейнты/колонка/payload-тип с расшифровкой),
но НЕ восстанавливает удалённые дубли — они остаются в архивных таблицах
``*_dedup_archive_0085`` (создаются только при наличии дублей) — ручной путь
восстановления, как в 0048.

Revision ID: 20260718_0085
Revises: 20260711_0084
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260718_0085"
down_revision = "20260711_0084"
branch_labels = None
depends_on = None

_ENCRYPTED_PREFIXES = ("v1:", "v2:")

# Кросс-диалектный JSON-тип исходной колонки (как в модели до 0085): PG —
# JSONB, SQLite — TEXT-backed JSON. Нужен downgrade'у: голый postgresql.JSONB
# не рендерится SQLite-компилятором при batch-recreate.
_JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

# Выжившая строка группы дублей: детерминистическая, по смыслу «самая ранняя».
# message_jobs/agent_threads: ids — случайный hex (job_<hex> / thread_<hex>),
# поэтому по времени создания с tiebreak по id (у menu_plan_items timestamps
# нет — только id; выбор произволен, но детерминистирован, проигравшие строки
# архивируются до удаления).
_DOOMED_MESSAGE_JOBS = (
    "EXISTS ("
    "  SELECT 1 FROM message_jobs o"
    "  WHERE o.channel = d.channel"
    "    AND o.bot_key = d.bot_key"
    "    AND o.external_update_id = d.external_update_id"
    "    AND (o.enqueued_at < d.enqueued_at"
    "         OR (o.enqueued_at = d.enqueued_at AND o.id < d.id))"
    ")"
)
_DOOMED_AGENT_THREADS = (
    "EXISTS ("
    "  SELECT 1 FROM agent_threads o"
    "  WHERE o.tenant_id = d.tenant_id"
    "    AND o.channel_type = d.channel_type"
    "    AND o.external_chat_id = d.external_chat_id"
    "    AND (o.created_at < d.created_at"
    "         OR (o.created_at = d.created_at AND o.id < d.id))"
    ")"
)
_DOOMED_MENU_PLAN_ITEMS = (
    "EXISTS ("
    "  SELECT 1 FROM menu_plan_items o"
    "  WHERE o.menu_plan_id = d.menu_plan_id"
    "    AND o.day_of_week = d.day_of_week"
    "    AND o.meal_type = d.meal_type"
    "    AND o.id < d.id"
    ")"
)


def _lock_tables(bind) -> None:
    """EXCLUSIVE-лок на время дедуп→CREATE UNIQUE окна (образец 0048):
    ни конкурентный writer не добавит дубль, ни FK-insert не укажет на
    удаляемую строку. lock_timeout — fail-fast с откатом вместо подвисания.
    PostgreSQL only — SQLite сериализует writer'ов сам."""
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        bind.execute(sa.text(
            "LOCK TABLE message_jobs, users, agent_threads, menu_plan_items,"
            " conversation_turns, agent_runs IN EXCLUSIVE MODE"
        ))


def _archive_and_delete_doomed(bind, table: str, doomed_where: str, archive: str) -> int:
    """Архив + удаление дублей (0048): архивная таблица создаётся ТОЛЬКО при
    наличии дублей (здоровая БД не засоряется пустой), предикат delete —
    дословно тот же, что у архива (расхождение невозможно)."""
    has_dupes = bind.execute(sa.text(
        f"SELECT 1 FROM {table} d WHERE {doomed_where} LIMIT 1"
    )).first()
    if has_dupes is None:
        return 0
    doomed_count = bind.execute(sa.text(
        f"SELECT count(*) FROM {table} d WHERE {doomed_where}"
    )).scalar()
    bind.execute(sa.text(
        f"CREATE TABLE {archive} AS SELECT d.* FROM {table} d WHERE {doomed_where}"
    ))
    bind.execute(sa.text(
        f"DELETE FROM {table} WHERE id IN ("
        f"SELECT d.id FROM {table} d WHERE {doomed_where})"
    ))
    return int(doomed_count)


def _dedup_message_jobs(bind) -> int:
    """Дедуп по новому ключу (channel, bot_key, external_update_id).

    На здоровой БД вакуусен: старый UNIQUE (channel, external_update_id) к
    этому моменту ещё активен и bot_key только что бэкфиллен единым 'sreda'
    → дубли невозможны. Шаг оборонительный (env с вручную снятым
    констрейнтом / строками, вставленными в обход). FK-ссылок на
    message_jobs.id в схеме нет (проверено свипом моделей) — repoint не нужен.
    """
    return _archive_and_delete_doomed(
        bind, "message_jobs", _DOOMED_MESSAGE_JOBS, "message_jobs_dedup_archive_0085"
    )


def _dedup_menu_plan_items(bind) -> int:
    """Дедуп слотов (menu_plan_id, day_of_week, meal_type) — LLM-дубли одной
    ячейки недельной сетки. FK-ссылок на menu_plan_items.id в схеме нет
    (проверено свипом моделей) — repoint не нужен."""
    return _archive_and_delete_doomed(
        bind, "menu_plan_items", _DOOMED_MENU_PLAN_ITEMS,
        "menu_plan_items_dedup_archive_0085",
    )


def _dedup_agent_threads(bind) -> int:
    """Дедуп (tenant_id, channel_type, external_chat_id) с repoint'ом единственных
    FK-ссылателей: ``conversation_turns.thread_id`` и ``agent_runs.thread_id``.

    Survivor = самая ранняя (created_at, id) строка группы. Composite FK
    ``fk_agent_runs_turn_thread`` (turn_id, thread_id) → conversation_turns
    (id, thread_id) делает последовательный repoint невозможным (любой порядок
    UPDATE временно сиротит ссылающуюся сторону), поэтому на PG FK снимается
    на окно repoint'а и пересоздаётся после. SQLite FK не энфорсит без PRAGMA —
    ветка скипается (тесты ходатайствуют helper напрямую)."""
    has_dupes = bind.execute(sa.text(
        f"SELECT 1 FROM agent_threads d WHERE {_DOOMED_AGENT_THREADS} LIMIT 1"
    )).first()
    if has_dupes is None:
        return 0

    doomed_count = bind.execute(sa.text(
        f"SELECT count(*) FROM agent_threads d WHERE {_DOOMED_AGENT_THREADS}"
    )).scalar()
    bind.execute(sa.text(
        "CREATE TABLE agent_threads_dedup_archive_0085 AS "
        f"SELECT d.* FROM agent_threads d WHERE {_DOOMED_AGENT_THREADS}"
    ))

    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        bind.execute(sa.text(
            "ALTER TABLE agent_runs DROP CONSTRAINT fk_agent_runs_turn_thread"
        ))

    # Repoint doomed → survivor. Коррелированная форма БЕЗ UPDATE ... FROM и
    # без alias на DML-target (образец 0048) — портабельна PG/SQLite, один
    # текст на оба диалекта. Survivor = ORDER BY (created_at, id) LIMIT 1.
    # WHERE EXISTS ограничивает апдейт строками, чей thread реально doomed
    # (ссылающиеся на выживший thread не трогаем).
    for table in ("conversation_turns", "agent_runs"):
        bind.execute(sa.text(
            f"""
            UPDATE {table}
            SET thread_id = (
                SELECT s.id
                FROM agent_threads cur
                JOIN agent_threads s
                  ON s.tenant_id = cur.tenant_id
                 AND s.channel_type = cur.channel_type
                 AND s.external_chat_id = cur.external_chat_id
                WHERE cur.id = {table}.thread_id
                ORDER BY s.created_at ASC, s.id ASC
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1
                FROM agent_threads cur
                JOIN agent_threads smaller
                  ON smaller.tenant_id = cur.tenant_id
                 AND smaller.channel_type = cur.channel_type
                 AND smaller.external_chat_id = cur.external_chat_id
                 AND (smaller.created_at < cur.created_at
                      OR (smaller.created_at = cur.created_at
                          AND smaller.id < cur.id))
                WHERE cur.id = {table}.thread_id
            )
            """
        ))

    if is_pg:
        bind.execute(sa.text(
            "ALTER TABLE agent_runs ADD CONSTRAINT fk_agent_runs_turn_thread"
            " FOREIGN KEY (turn_id, thread_id)"
            " REFERENCES conversation_turns (id, thread_id)"
        ))

    bind.execute(sa.text(
        "DELETE FROM agent_threads WHERE id IN ("
        f"SELECT d.id FROM agent_threads d WHERE {_DOOMED_AGENT_THREADS})"
    ))
    return int(doomed_count)


def _preflight_users_max_account_id(bind) -> None:
    """Дубли max_account_id авто-слиянию НЕ подлежат: User — корневая сущность
    (memories, профили, напоминания, inbound/outbox ссылки), merge только
    ручной. Коллизия → громкая остановка deploy со списком ключей (значения —
    opaque числовые MAX-id, печатаются для remediation-запроса оператора)."""
    rows = bind.execute(sa.text(
        "SELECT max_account_id, count(*) AS c FROM users"
        " WHERE max_account_id IS NOT NULL"
        " GROUP BY max_account_id HAVING count(*) > 1"
    )).all()
    if rows:
        sample = ", ".join(sorted(r[0] for r in rows)[:5])
        raise RuntimeError(
            f"0085 preflight: {len(rows)} дублирующихся max_account_id в users "
            f"(примеры: {sample}). partial unique index НЕ создан. Слейте дубли "
            "вручную (переведите ссылки на выжившего User, второго удалите) и "
            "повторите миграцию."
        )


def _encrypt_existing_payloads(bind) -> int:
    """Перешифровка plaintext-payload'ов после ALTER JSONB→Text (паттерн
    0028/0029, идемпотентно по envelope-префиксу). Пустая таблица — скип БЕЗ
    импорта encryption (нет зависимости от SREDA_ENCRYPTION_KEY на пустом
    контуре; очередь за фичефлагом — типичный случай)."""
    rows = bind.execute(sa.text(
        "SELECT id, message_payload FROM message_jobs"
    )).all()
    plaintext = [(r[0], r[1]) for r in rows
                 if r[1] and not str(r[1]).startswith(_ENCRYPTED_PREFIXES)]
    if not plaintext:
        return 0
    # Lazy import — encryption module хочет настройки и crypto-bindings.
    from sreda.services.encryption import encrypt_value

    for row_id, value in plaintext:
        bind.execute(
            sa.text(
                "UPDATE message_jobs SET message_payload = :enc WHERE id = :id"
            ),
            {"enc": encrypt_value(str(value)), "id": row_id},
        )
    return len(plaintext)


def _decrypt_existing_payloads(bind) -> int:
    """Обратный шаг downgrade: расшифровка envelope-строк перед возвратом
    типа JSONB (иначе ``::jsonb``-cast упал бы на шифротексте)."""
    rows = bind.execute(sa.text(
        "SELECT id, message_payload FROM message_jobs"
        " WHERE message_payload LIKE 'v1:%' OR message_payload LIKE 'v2:%'"
    )).all()
    if not rows:
        return 0
    from sreda.services.encryption import decrypt_value

    for row_id, value in rows:
        bind.execute(
            sa.text(
                "UPDATE message_jobs SET message_payload = :plain WHERE id = :id"
            ),
            {"plain": decrypt_value(value), "id": row_id},
        )
    return len(rows)


def upgrade() -> None:
    bind = op.get_bind()
    _lock_tables(bind)

    # --- 1. message_jobs: +bot_key, дедуп, замена дедуп-констрейнта ---------
    op.add_column(
        "message_jobs",
        sa.Column(
            "bot_key",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'sreda'"),
        ),
    )
    _dedup_message_jobs(bind)
    # batch_alter_table: на PG операции исполняются прямыми ALTER, на SQLite —
    # copy-and-move (у SQLite нет ALTER CONSTRAINT/ALTER COLUMN TYPE). Один
    # код на оба диалекта, как и портабельный SQL дедуп-хелперов (образец 0048).
    # drop старого констрейнта — только PG: рефлексия SQLite не видит named
    # UNIQUE в table.constraints (batch-recreate пересоздаст таблицу уже без
    # него), и явный drop упал бы «No such constraint».
    with op.batch_alter_table("message_jobs") as b:
        if bind.dialect.name == "postgresql":
            b.drop_constraint(
                "uq_message_jobs_channel_external_update_id", type_="unique"
            )
        b.create_unique_constraint(
            "uq_message_jobs_channel_bot_update",
            ["channel", "external_update_id", "bot_key"],
        )
        # --- 2. message_payload: JSONB → Text (шифрование — следующим шагом)
        b.alter_column(
            "message_payload",
            existing_type=_JSONB,
            type_=sa.Text(),
            existing_nullable=False,
            postgresql_using="message_payload::text",
        )
    _encrypt_existing_payloads(bind)

    # --- 3. users.max_account_id: префлайт + partial unique ------------------
    _preflight_users_max_account_id(bind)
    op.create_index(
        "uq_users_max_account_id",
        "users",
        ["max_account_id"],
        unique=True,
        postgresql_where=sa.text("max_account_id IS NOT NULL"),
        sqlite_where=sa.text("max_account_id IS NOT NULL"),
    )

    # --- 4. agent_threads: дедуп + UNIQUE (tenant, channel, chat) -----------
    _dedup_agent_threads(bind)
    with op.batch_alter_table("agent_threads") as b:
        b.create_unique_constraint(
            "uq_agent_threads_tenant_channel_chat",
            ["tenant_id", "channel_type", "external_chat_id"],
        )

    # --- 5. menu_plan_items: дедуп + UNIQUE (plan, day, meal) ---------------
    _dedup_menu_plan_items(bind)
    with op.batch_alter_table("menu_plan_items") as b:
        b.create_unique_constraint(
            "uq_menu_plan_items_plan_day_meal",
            ["menu_plan_id", "day_of_week", "meal_type"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # drop-ы констрейнтов — только PG (на SQLite batch-recreate пересоздаёт
    # таблицы уже без них; см. комментарий в upgrade).
    with op.batch_alter_table("menu_plan_items") as b:
        if is_pg:
            b.drop_constraint("uq_menu_plan_items_plan_day_meal", type_="unique")
    with op.batch_alter_table("agent_threads") as b:
        if is_pg:
            b.drop_constraint("uq_agent_threads_tenant_channel_chat", type_="unique")
    op.drop_index("uq_users_max_account_id", table_name="users")

    # Дедупы намеренно НЕ откатываются: удалённые строки живут в архивных
    # таблицах *_dedup_archive_0085 (ручной restore), как в 0048.
    _decrypt_existing_payloads(bind)
    with op.batch_alter_table("message_jobs") as b:
        b.alter_column(
            "message_payload",
            existing_type=sa.Text(),
            type_=_JSONB,
            existing_nullable=False,
            postgresql_using="message_payload::jsonb",
        )
        if is_pg:
            b.drop_constraint("uq_message_jobs_channel_bot_update", type_="unique")
        b.create_unique_constraint(
            "uq_message_jobs_channel_external_update_id",
            ["channel", "external_update_id"],
        )
        b.drop_column("bot_key")
