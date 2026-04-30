"""SQLite → PostgreSQL data migration.

Читает все таблицы из SQLite snapshot и копирует в пустой PG в правильном
FK-порядке через `Base.metadata.sorted_tables`. Boolean / DateTime /
EncryptedString конвертируются автоматически благодаря SQLAlchemy Core
type-processors (с raw SQL `op.execute()` это бы упало — отсюда фиксы
в migrations 0017/0018).

Usage:
    SQLITE_URL='sqlite:////var/lib/sreda/sreda.db.dryrun-snapshot' \\
    PG_URL='postgresql+psycopg://sreda:<pwd>@localhost:5432/sreda_dryrun' \\
    python scripts/migrate_sqlite_to_pg.py

Защита:
- alembic_version пропускается (уже создан alembic upgrade head в PG)
- Перед загрузкой: SAVE row counts SQLite
- После: VERIFY counts в PG, fail fast при расхождении
- Каждая таблица в своей транзакции, batch INSERT по 1000 row
"""
from __future__ import annotations

import os
import sys
import time
from typing import Iterable

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

# Triggers all model imports → registers in Base.metadata
import sreda.db.models  # noqa: F401
from sreda.db.base import Base


SQLITE_URL = os.environ["SQLITE_URL"]
PG_URL = os.environ["PG_URL"]
BATCH_SIZE = 1000

# Tables to skip — managed by alembic, not data migration.
SKIP_TABLES = {"alembic_version"}


def _chunks(rows: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _row_count(conn, table) -> int:
    return conn.execute(select(func.count()).select_from(table)).scalar_one()


def main() -> int:
    print(f"[start] SQLite={SQLITE_URL}")
    print(f"[start] PG={PG_URL.split('@')[-1] if '@' in PG_URL else PG_URL}")

    sqlite_engine = create_engine(SQLITE_URL, future=True)
    pg_engine = create_engine(PG_URL, future=True)

    # ------------------------------------------------------------------
    # Phase 1: snapshot row counts in SQLite (source of truth).
    # ------------------------------------------------------------------
    src_counts: dict[str, int] = {}
    with sqlite_engine.connect() as src:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP_TABLES:
                continue
            src_counts[table.name] = _row_count(src, table)
    total_rows = sum(src_counts.values())
    print(f"[snapshot] {len(src_counts)} tables, {total_rows} total rows")

    # ------------------------------------------------------------------
    # Phase 2: TRUNCATE PG tables in reverse FK order (clears alembic
    # seed rows like subscription_plans без drop/recreate базы).
    # ------------------------------------------------------------------
    with pg_engine.begin() as dst:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in SKIP_TABLES:
                continue
            dst.execute(table.delete())
    print(f"[truncate] cleared {len(src_counts)} target tables")

    # ------------------------------------------------------------------
    # Phase 3: copy each table in FK-dependency order.
    # ------------------------------------------------------------------
    t_start = time.time()
    SqliteSession = sessionmaker(sqlite_engine)
    PgSession = sessionmaker(pg_engine)

    for table in Base.metadata.sorted_tables:
        if table.name in SKIP_TABLES:
            continue
        n_expected = src_counts[table.name]
        if n_expected == 0:
            print(f"[skip ] {table.name:<40} 0 rows")
            continue

        t0 = time.time()
        with sqlite_engine.connect() as src:
            rows = [dict(r._mapping) for r in src.execute(select(table)).all()]

        # Insert into PG в одной транзакции на таблицу.
        n_inserted = 0
        with pg_engine.begin() as dst:
            for batch in _chunks(rows, BATCH_SIZE):
                dst.execute(table.insert(), batch)
                n_inserted += len(batch)

        elapsed = time.time() - t0
        rate = n_inserted / elapsed if elapsed > 0 else float("inf")
        print(f"[load ] {table.name:<40} {n_inserted:>6} rows in {elapsed:>5.2f}s ({rate:>6.0f} rps)")

    # ------------------------------------------------------------------
    # Phase 4: verify counts match.
    # ------------------------------------------------------------------
    print()
    print("[verify] comparing counts...")
    mismatches: list[tuple[str, int, int]] = []
    with pg_engine.connect() as dst:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP_TABLES:
                continue
            expected = src_counts[table.name]
            actual = _row_count(dst, table)
            if actual != expected:
                mismatches.append((table.name, expected, actual))
                print(f"[MISMATCH] {table.name}: SQLite={expected} PG={actual}")

    total_elapsed = time.time() - t_start
    if mismatches:
        print(f"\n[FAIL] {len(mismatches)} table(s) mismatched after migration. Total time {total_elapsed:.1f}s.")
        return 1

    print(f"\n[ok] {len(src_counts)} tables, {total_rows} rows migrated in {total_elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
