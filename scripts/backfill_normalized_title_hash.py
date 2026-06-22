#!/usr/bin/env python3
"""Backfill ``normalized_title_hash`` for legacy rows across 5 user-facing
tables (Sub-A10 / Group 3.1, Codex R2 MAJOR #5).

The Sub-A10 migration added ``normalized_title_hash`` columns but left
existing rows at NULL — intentional at the time, since the planner-flow
wasn't yet using semantic dedup. With planner-flow now active, legacy
rows need their hashes filled in so "уже есть такое" lookups work for
data that predates the migration.

Target tables (entity_type → ORM class → has EncryptedString title):

  * shopping_list_items  → ShoppingListItem  → yes
  * recipes              → Recipe            → yes
  * checklists           → Checklist         → no (plaintext String(200))

NB (#163 Фаза 2а): family_reminders / tasks_items НЕ здесь — у них TIME-AWARE
semantic_key (название+время), title-only бэкфилл им не подходит (см. TABLES).

For EncryptedString titles we go through the ORM so the descriptor
auto-decrypts at attribute access. Plaintext titles work the same way.

Pagination uses a primary-key cursor (``WHERE id > last_id ORDER BY id
LIMIT batch_size``) rather than OFFSET. As rows get filled in, the
``IS NULL`` filter narrows, so OFFSET would skip rows. PK-cursor with
``IS NULL`` filter is safe under concurrent writes from production:
new rows added during backfill get their hash from the write-path code,
not us, and our cursor just walks past them.

Empty-hash handling: ``compute_normalized_title_hash`` returns ``""``
when ``normalize_for_dedup(title)`` collapses to nothing (whitespace-only
or punctuation-only titles). We leave such rows NULL — an empty hash
can't dedup against anything anyway. The PK-cursor advances past them
so we don't loop forever.

Nullable user_id (family_reminders only): coerced to ``""`` for the
hash. Matches the production write-path convention — see
``compute_normalized_title_hash`` docstring: "Other scope args may be
blank".

Idempotency: filter ``WHERE normalized_title_hash IS NULL`` means
re-running on already-filled rows is a no-op. Safe to re-run after
partial completion or after a crash mid-batch.

Throttle: ``--sleep-ms`` between batches (default 100ms). At batch
size 500 and 100ms throttle, ~5000 rows/second of wall-clock budget
on the throttle side; actual rate is bounded by DB roundtrip.

Pre-flight:
  * Migration 0051 already applied (columns exist NULL).
  * SREDA_ENCRYPTION_KEY env var set (otherwise the hash falls back
    to plain SHA-256 and the function logs a warning — same key
    EncryptedString already needs to decrypt titles).

Usage:

    # Dev — preview row counts without writing
    python scripts/backfill_normalized_title_hash.py --dry-run

    # Dev — actual fill, default throttle
    python scripts/backfill_normalized_title_hash.py

    # Prod (run under sreda service account, env from /etc/sreda/.env)
    sudo systemd-run --uid=sreda --gid=sreda \\
        --working-directory=/opt/sreda \\
        -p EnvironmentFile=/etc/sreda/.env \\
        --wait --collect --pipe \\
        /opt/sreda/.venv/bin/python \\
        /opt/sreda/scripts/backfill_normalized_title_hash.py \\
        --sleep-ms 200
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sreda.db.models.checklists import Checklist
from sreda.db.models.housewife_food import Recipe, ShoppingListItem
from sreda.db.session import get_session_factory
from sreda.services.operation_id import compute_normalized_title_hash


@dataclass(frozen=True)
class TableSpec:
    entity_type: str
    model: type[Any]
    label: str


TABLES: tuple[TableSpec, ...] = (
    TableSpec("shopping_list_item", ShoppingListItem, "shopping_list_items"),
    # #163 Фаза 2а: family_reminders/tasks_items используют TIME-AWARE semantic_key
    # (название+время), а НЕ title-only. Title-only бэкфилл здесь схлопнул бы записи с разным
    # временем → ЛОЖНЫЕ коллизии в partial-unique индексе (Фаза 2в). По плану #163 их НЕ
    # бэкфиллим (индекс работает на новых строках; старые NULL-hash не участвуют). Понадобится —
    # отдельный TIME-AWARE бэкфилл (с extra=trigger/date+time+rrule), не этот title-only.
    TableSpec("recipe", Recipe, "recipes"),
    TableSpec("checklist", Checklist, "checklists"),
)


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("backfill_title_hash")
    log.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(h)
    log.propagate = False
    return log


def _count_pending(session: Session, model: type[Any]) -> int:
    from sqlalchemy import func

    return session.execute(
        select(func.count())
        .select_from(model)
        .where(model.normalized_title_hash.is_(None))
    ).scalar_one()


def _backfill_table(
    spec: TableSpec,
    *,
    batch_size: int,
    sleep_seconds: float,
    dry_run: bool,
    log: logging.Logger,
) -> tuple[int, int, int]:
    """Backfill one table.

    Returns ``(filled, skipped_empty, scanned)``:
      * filled — rows that got a non-empty hash written
      * skipped_empty — rows whose title normalized to empty string
        (left NULL — can't dedup an empty hash)
      * scanned — total rows visited (filled + skipped_empty)
    """
    sf = get_session_factory()
    model = spec.model
    filled = 0
    skipped_empty = 0
    scanned = 0
    last_id = ""

    with sf() as session:
        total_pending = _count_pending(session, model)
    log.info("[%s] %d rows pending", spec.label, total_pending)
    if total_pending == 0:
        return 0, 0, 0

    while True:
        with sf() as session:
            rows = (
                session.execute(
                    select(model)
                    .where(model.normalized_title_hash.is_(None))
                    .where(model.id > last_id)
                    .order_by(model.id)
                    .limit(batch_size)
                )
                .scalars()
                .all()
            )
            if not rows:
                break

            for row in rows:
                scanned += 1
                title = row.title or ""
                user_id = getattr(row, "user_id", None) or ""
                tenant_id = row.tenant_id or ""
                h = compute_normalized_title_hash(
                    title,
                    entity_type=spec.entity_type,
                    tenant_id=str(tenant_id),
                    user_id=str(user_id),
                )
                if h:
                    if not dry_run:
                        row.normalized_title_hash = h
                    filled += 1
                else:
                    skipped_empty += 1

            last_id = rows[-1].id

            if dry_run:
                session.rollback()
            else:
                session.commit()

        log.info(
            "[%s] batch done: scanned=%d filled=%d skipped_empty=%d last_id=%s%s",
            spec.label,
            scanned,
            filled,
            skipped_empty,
            last_id,
            " (DRY-RUN)" if dry_run else "",
        )

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return filled, skipped_empty, scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows per batch (default: 500).",
    )
    parser.add_argument(
        "--sleep-ms",
        type=int,
        default=100,
        help="Sleep between batches in ms (default: 100).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute hashes but do not commit; logs what WOULD be filled.",
    )
    parser.add_argument(
        "--only",
        choices=[s.label for s in TABLES],
        default=None,
        help="Restrict to a single table (default: all 5).",
    )
    args = parser.parse_args(argv)

    log = _setup_logging()
    sleep_seconds = max(args.sleep_ms, 0) / 1000.0
    targets = (
        TABLES if args.only is None else tuple(s for s in TABLES if s.label == args.only)
    )

    log.info(
        "Starting backfill: tables=%s batch_size=%d sleep_ms=%d dry_run=%s",
        [s.label for s in targets],
        args.batch_size,
        args.sleep_ms,
        args.dry_run,
    )

    total_filled = 0
    total_skipped = 0
    total_scanned = 0
    for spec in targets:
        try:
            filled, skipped, scanned = _backfill_table(
                spec,
                batch_size=args.batch_size,
                sleep_seconds=sleep_seconds,
                dry_run=args.dry_run,
                log=log,
            )
        except Exception:  # noqa: BLE001
            log.exception("[%s] backfill failed — re-run is safe (IS NULL filter)", spec.label)
            return 1
        log.info(
            "[%s] done: filled=%d skipped_empty=%d scanned=%d",
            spec.label,
            filled,
            skipped,
            scanned,
        )
        total_filled += filled
        total_skipped += skipped
        total_scanned += scanned

    log.info(
        "DONE total_filled=%d total_skipped_empty=%d total_scanned=%d dry_run=%s",
        total_filled,
        total_skipped,
        total_scanned,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
