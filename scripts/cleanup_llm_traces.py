#!/usr/bin/env python3
"""Issue #68 — cleanup old llm-trace day-folders.

Runs from systemd timer ``sreda-llm-traces-cleanup.timer`` daily.

Strategy:
- Parse folder names ``YYYY-MM-DD`` (UTC). NOT mtime — mtime может быть
  обновлён по cron access patterns; folder name = source of truth.
- Delete folders strictly older than ``today_utc - keep_days``.
- Logs N deleted, M kept. Non-zero exit on hard errors (root path missing
  / unreadable); otherwise exit 0.

Usage:
    python cleanup_llm_traces.py [--root PATH] [--keep-days N] [--dry-run]

Plan: plans/mellow-discovering-conway-final.md, Section 12.
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ROOT = Path("/var/lib/sreda/private/llm-traces")
DEFAULT_KEEP_DAYS = 5
DAY_FOLDER_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

logger = logging.getLogger("sreda.llm_trace.cleanup")


def cleanup(root: Path, keep_days: int, dry_run: bool = False) -> tuple[int, int]:
    """Удалить day-folders с именем 'YYYY-MM-DD' старше today_utc - keep_days.

    Returns (deleted_count, kept_count).
    """
    if not root.exists():
        logger.warning("root path %s does not exist; nothing to clean", root)
        return (0, 0)
    if not root.is_dir():
        raise RuntimeError(f"root {root} is not a directory")

    today_utc = datetime.now(timezone.utc).date()
    cutoff = today_utc - timedelta(days=keep_days)
    deleted = 0
    kept = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = DAY_FOLDER_RE.match(child.name)
        if not m:
            # Unknown folder structure — skip, don't touch
            continue
        try:
            folder_date = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        if folder_date < cutoff:
            if dry_run:
                logger.info("[DRY-RUN] would remove %s (date=%s, cutoff=%s)",
                            child, folder_date, cutoff)
            else:
                logger.info("removing %s (date=%s < cutoff %s)",
                            child, folder_date, cutoff)
                shutil.rmtree(child)
            deleted += 1
        else:
            kept += 1
    return (deleted, kept)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT,
        help=f"Trace root directory (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--keep-days", type=int, default=DEFAULT_KEEP_DAYS,
        help=f"Keep folders younger than N days (default: {DEFAULT_KEEP_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print actions without deleting",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.keep_days < 0:
        logger.error("--keep-days must be >= 0")
        return 2
    try:
        deleted, kept = cleanup(args.root, args.keep_days, args.dry_run)
    except RuntimeError as e:
        logger.error("cleanup failed: %s", e)
        return 1
    logger.info("cleanup done: deleted=%d kept=%d root=%s keep_days=%d%s",
                deleted, kept, args.root, args.keep_days,
                " [DRY-RUN]" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
