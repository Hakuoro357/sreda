"""inbound_messages: partial unique index on (channel_type, bot_key, external_update_id)

Phase 2 of the second-Telegram-bot feature (second-tg-bot plan).

Problem: Telegram update_id counters are per-bot, so bot-A's update 42
and bot-B's update 42 are different events.  The old dedup check filtered
only on ``external_update_id``, causing a false-duplicate when both bots
deliver the same numeric id.

Fix: widen the uniqueness key to (channel_type, bot_key, external_update_id).
Only non-NULL external_update_ids are constrained (NULL rows are synthetic
events that must remain freely insertable).

Also backfills existing inbound_messages rows that have a NULL bot_key to
'sreda' (the legacy Telegram bot) — the column was added with a NOT NULL
default in migration 0035, so NULL rows should not exist in practice, but
this guard runs anyway for safety.

Pre-existing duplicates: the legacy dedup was a query-before-insert check
(no DB constraint), so a past race could persist the same Telegram update
more than once.  Such duplicates would foil the unique index, so this
migration first collapses them — keeping the lowest id per
(channel_type, bot_key, external_update_id) group, repointing the only
inbound FK (agent_runs.inbound_message_id) to the survivor — before
creating the index.

Revision ID: 20260603_0048
Revises: 20260520_0047
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0048"
down_revision = "20260520_0047"
branch_labels = None
depends_on = None

_INDEX_NAME = "ux_inbound_dedup_channel_bot_update"


def _dedup_inbound_duplicates(bind) -> None:
    """Collapse pre-existing duplicate inbound_messages rows that share
    (channel_type, bot_key, external_update_id) so the partial unique index
    can be created.

    Survivor = the lowest-``id`` row of each group.  ``agent_runs`` is the ONLY
    table whose FK references ``inbound_messages`` (verified against the
    production schema — see catalog check in the deploy runbook); its
    ``inbound_message_id`` is nullable and not unique, so we repoint every
    reference to a doomed row onto the survivor before deleting the duplicates —
    referential integrity is preserved and one survivor may legitimately be
    referenced by many runs.

    Notes
    -----
    * A row is a *duplicate* iff another row in the same group has a smaller
      ``id``.  We test that with ``EXISTS (… o.id < d.id …)`` rather than
      ``NOT IN (SELECT MIN(id) …)`` for two reasons (Codex high R1):
        - it is NULL-safe (``NOT IN`` is poisoned if any value is NULL); and
        - the equality joins mean a row with a NULL key column never matches a
          sibling and is therefore left untouched — matching PostgreSQL's
          partial-unique-index semantics, where NULLs compare as distinct.
    * Repoint MUST precede delete so no ``agent_runs`` row is ever left pointing
      at a removed inbound row.
    * Duplicates are re-ingests of the SAME Telegram update (per-bot update_id
      counter), so any survivor is semantically equivalent.  On the production
      DB zero ``agent_runs`` rows reference the affected group, so the repoint
      is a verified no-op there; it exists for correctness on other envs.
    * Portable across PostgreSQL and SQLite: correlated subqueries only, no
      dialect-specific ``UPDATE … FROM`` / ``DELETE … USING`` and no alias on
      the DML target (the target table is referenced by name in the subquery).
    * The doomed rows are copied into ``inbound_messages_dedup_archive_0048``
      before deletion so the destructive cleanup is recoverable in-DB — a belt
      alongside the mandatory pre-deploy backup (Codex medium+high R1 flagged
      unaudited deletion). The archive table is created ONLY when duplicates
      actually exist, so a healthy/fresh database is never littered with an
      empty table.
    """
    _DOOMED_WHERE = (
        "d.external_update_id IS NOT NULL"
        " AND EXISTS ("
        "   SELECT 1 FROM inbound_messages o"
        "   WHERE o.channel_type = d.channel_type"
        "     AND o.bot_key = d.bot_key"
        "     AND o.external_update_id = d.external_update_id"
        "     AND o.id < d.id"
        " )"
    )

    # 0. Archive doomed rows (only if any exist) for in-DB recoverability.
    has_dupes = bind.execute(sa.text(
        f"SELECT 1 FROM inbound_messages d WHERE {_DOOMED_WHERE} LIMIT 1"
    )).first()
    if has_dupes is not None:
        bind.execute(sa.text(
            "CREATE TABLE inbound_messages_dedup_archive_0048 AS "
            f"SELECT d.* FROM inbound_messages d WHERE {_DOOMED_WHERE}"
        ))

    # 1. Repoint agent_runs referencing a duplicate (non-survivor) onto the
    #    surviving row (lowest id) of the same group.
    bind.execute(sa.text(
        """
        UPDATE agent_runs
        SET inbound_message_id = (
            SELECT MIN(s.id)
            FROM inbound_messages cur
            JOIN inbound_messages s
              ON s.channel_type = cur.channel_type
             AND s.bot_key = cur.bot_key
             AND s.external_update_id = cur.external_update_id
            WHERE cur.id = agent_runs.inbound_message_id
        )
        WHERE inbound_message_id IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM inbound_messages cur
            JOIN inbound_messages smaller
              ON smaller.channel_type = cur.channel_type
             AND smaller.bot_key = cur.bot_key
             AND smaller.external_update_id = cur.external_update_id
             AND smaller.id < cur.id
            WHERE cur.id = agent_runs.inbound_message_id
              AND cur.external_update_id IS NOT NULL
          )
        """
    ))

    # 2. Delete duplicates — exactly the rows archived in step 0 (the predicate
    #    is reused verbatim so the archive and the delete can never diverge).
    bind.execute(sa.text(
        "DELETE FROM inbound_messages WHERE id IN ("
        f"SELECT d.id FROM inbound_messages d WHERE {_DOOMED_WHERE})"
    ))


def upgrade() -> None:
    bind = op.get_bind()

    # Lock the table for the whole migration so no concurrent writer can race
    # the dedup → CREATE UNIQUE INDEX window (Codex medium+high R1). EXCLUSIVE
    # MODE blocks every write to inbound_messages AND the ROW SHARE lock that an
    # ``agent_runs`` FK-insert would take on it — so during the migration no one
    # can add a new duplicate, nor insert an agent_run pointing at a row we are
    # about to delete. Plain SELECTs (ACCESS SHARE) still proceed, and our own
    # statements are unaffected (a tx never conflicts with its own locks). The
    # lock is held until this transaction commits. ``lock_timeout`` makes us fail
    # fast (and roll back) instead of hanging if another session holds a
    # conflicting lock. PostgreSQL only — SQLite serialises writers already and
    # has no LOCK TABLE.
    #
    # We deliberately keep the plain (blocking) CREATE INDEX rather than
    # CONCURRENTLY: CONCURRENTLY cannot run inside Alembic's transaction and can
    # leave an INVALID index on failure; for a table this size (~2.3k rows on
    # prod) the blocking build is sub-second, simpler, and atomic. The whole
    # migration relies on Alembic's default transactional DDL on PostgreSQL, so
    # the dedup + index creation are atomic — a failed index build rolls the
    # dedup (and archive) back, as observed on the first deploy attempt.
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        bind.execute(sa.text("LOCK TABLE inbound_messages IN EXCLUSIVE MODE"))

    # Safety backfill: any legacy row with NULL bot_key gets 'sreda'.
    # The column was added NOT NULL with server_default in 0035, so this
    # should be a no-op on a healthy DB, but it prevents the unique index
    # from being foiled by unexpected NULLs in the key columns.
    bind.execute(sa.text(
        "UPDATE inbound_messages SET bot_key = 'sreda' WHERE bot_key IS NULL"
    ))

    # Collapse any pre-existing duplicates (legacy race) before the unique
    # index — otherwise CREATE UNIQUE INDEX fails on the duplicate key.
    _dedup_inbound_duplicates(bind)

    op.create_index(
        _INDEX_NAME,
        "inbound_messages",
        ["channel_type", "bot_key", "external_update_id"],
        unique=True,
        postgresql_where=sa.text("external_update_id IS NOT NULL"),
        sqlite_where=sa.text("external_update_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Only the index is reversible. The dedup is intentionally NOT undone: the
    # deleted duplicate rows live on in ``inbound_messages_dedup_archive_0048``
    # (created by upgrade when duplicates existed), which is the manual-restore
    # path — so we deliberately leave that archive table in place on downgrade.
    op.drop_index(_INDEX_NAME, table_name="inbound_messages")
