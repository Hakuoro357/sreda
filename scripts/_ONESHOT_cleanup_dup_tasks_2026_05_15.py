"""R-32 Sub-step B: One-shot cleanup для tg_755682022 task duplicates.

User feedback (tg_755682022, 2026-05-15): «Расписание работает
некорректно. Многие пункты задвоились. Найти причину, дубли удалить».

Root cause (R-32 Sub-step A — already deployed): mimo иногда эмитит
byte-equal duplicate tool_calls в одном LLM response. Этот script
очищает существующие duplicates до того момента когда A protection
заработала.

Default: **dry-run** — читает + показывает план DELETE без actual delete.
Use `--execute` flag для actual DELETE.

Logic:
1. SELECT все tasks tg_755682022 с status='pending', recurrence_rule IS NULL
   (recurring tasks могут legit повторяться — не трогаем).
2. ORM-level decrypt `title` (через EncryptedString property).
3. Group в Python by (scheduled_date, title_norm, time_start).
4. Для каждой группы с count > 1 → keep earliest created_at, mark rest для DELETE.
5. Print audit log: for each task → id, decrypted title, decision (keep/delete).
6. Если --execute → DELETE marked rows + verify count = 0 post-delete.

Audit trail: print task_id'ы для каждого decision — Borisus reviews dry-run
output, approves, runs `--execute`.

Created: 2026-05-15 для R-32 cleanup.
Issue: vex-assistant#41.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime

# Sreda imports — assumes running from /opt/sreda с venv activated и .env loaded
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import text

from sreda.db.models.tasks import Task
from sreda.db.session import get_session_factory


TARGET_TENANT = "tenant_tg_755682022"


def _normalize_title(title: str | None) -> str:
    """Lowercase + strip whitespace для grouping."""
    return (title or "").strip().lower()


def _group_key(t: Task) -> tuple:
    """Composite key для duplicate detection."""
    return (
        t.scheduled_date.isoformat() if t.scheduled_date else None,
        _normalize_title(t.title),
        t.time_start.isoformat() if t.time_start else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R-32 one-shot cleanup of duplicate tasks for tg_755682022"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually DELETE marked rows (default: dry-run только print).",
    )
    parser.add_argument(
        "--tenant",
        default=TARGET_TENANT,
        help=f"Tenant ID to clean (default: {TARGET_TENANT}).",
    )
    args = parser.parse_args()

    sf = get_session_factory()
    with sf() as session:
        # Step 1: SELECT pending non-recurring tasks
        tasks = (
            session.query(Task)
            .filter(
                Task.tenant_id == args.tenant,
                Task.status == "pending",
                Task.recurrence_rule.is_(None),
            )
            .order_by(Task.created_at)
            .all()
        )
        print(f"[+] Loaded {len(tasks)} pending non-recurring tasks for {args.tenant}")

        # Step 2 + 3: Group by composite key
        groups: dict[tuple, list[Task]] = defaultdict(list)
        for t in tasks:
            groups[_group_key(t)].append(t)

        # Step 4: identify duplicates per group
        to_delete: list[Task] = []
        to_keep: list[Task] = []
        for key, members in groups.items():
            if len(members) <= 1:
                continue  # no duplicates in this group
            # already ordered by created_at ASC → first is earliest
            keeper = members[0]
            to_keep.append(keeper)
            to_delete.extend(members[1:])

        # Step 5: audit log
        print()
        print("=" * 80)
        print(f"DUPLICATE GROUPS: {sum(1 for m in groups.values() if len(m) > 1)}")
        print(f"  Rows to KEEP (earliest per group): {len(to_keep)}")
        print(f"  Rows to DELETE: {len(to_delete)}")
        print("=" * 80)
        print()

        for key, members in groups.items():
            if len(members) <= 1:
                continue
            scheduled_date, title_norm, time_start = key
            print(f"GROUP: date={scheduled_date} time={time_start} title={title_norm!r}")
            for i, t in enumerate(members):
                decision = "KEEP" if i == 0 else "DELETE"
                print(
                    f"  [{decision}] id={t.id} created={t.created_at} "
                    f"title={(t.title or '')[:80]!r}"
                )
            print()

        if not to_delete:
            print("[+] No duplicates found — nothing to do.")
            return 0

        # Step 6: execute if --execute flag
        if not args.execute:
            print()
            print(f"[DRY-RUN] Would DELETE {len(to_delete)} rows. Pass --execute to actually delete.")
            return 0

        print(f"[!] EXECUTING DELETE for {len(to_delete)} task rows...")
        delete_ids = [t.id for t in to_delete]

        # Safety guard: explicit DELETE with tenant + id filter (no
        # accidental cross-tenant delete)
        deleted = session.execute(
            text(
                "DELETE FROM tasks_items "
                "WHERE id = ANY(:ids) AND tenant_id = :tid"
            ),
            {"ids": delete_ids, "tid": args.tenant},
        ).rowcount
        session.commit()
        print(f"[+] DELETE complete: {deleted} rows removed")

        # Verify count = 0
        post_tasks = (
            session.query(Task)
            .filter(
                Task.tenant_id == args.tenant,
                Task.status == "pending",
                Task.recurrence_rule.is_(None),
            )
            .all()
        )
        post_groups: dict[tuple, list[Task]] = defaultdict(list)
        for t in post_tasks:
            post_groups[_group_key(t)].append(t)
        remaining_dups = sum(1 for m in post_groups.values() if len(m) > 1)
        print(f"[+] Post-delete verification: {remaining_dups} duplicate groups remaining (expected 0)")
        return 0 if remaining_dups == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
