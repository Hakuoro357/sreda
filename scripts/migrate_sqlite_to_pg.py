"""SQLite → PostgreSQL data migration via schema reflection.

Reflection даёт плоские Text-колонки вместо TypeDecorator (EncryptedString),
поэтому копируем base64-envelope как сырые строки — bit-perfect, без
re-encryption. Это критично т.к. в БД могут лежать v1-ключи которых нет
в текущем encryption_service config (они читались бы через legacy
decrypt path в ORM, но при copy нам это не нужно).

FK-cycle (connect_sessions ↔ tenant_eds_accounts) обходится через
session_replication_role=replica — отключает FK-triggers на время
bulk INSERT. Требует SUPERUSER на момент миграции.

Usage:
    SQLITE_URL='sqlite:////var/lib/sreda/sreda.db.dryrun-snapshot' \\
    PG_URL='postgresql+psycopg://sreda:<pwd>@localhost:5432/sreda_dryrun' \\
    python scripts/migrate_sqlite_to_pg.py
"""
from __future__ import annotations

import os
import sys
import time
from typing import Iterable

import sqlalchemy as sa
from sqlalchemy import MetaData, create_engine, func, select


SQLITE_URL = os.environ["SQLITE_URL"]
PG_URL = os.environ["PG_URL"]
BATCH_SIZE = 1000

# Tables managed by alembic, не data migration.
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
    # Reflect SQLite schema (source). Плоские типы (Text/Integer/etc),
    # без TypeDecorator → копируем raw envelope-строки без encryption.
    # ------------------------------------------------------------------
    sqlite_meta = MetaData()
    sqlite_meta.reflect(bind=sqlite_engine)
    pg_meta = MetaData()
    pg_meta.reflect(bind=pg_engine)

    # Список таблиц для миграции (по PG как авторитетной — alembic
    # уже создал нужные). Использовать sqlite_meta order для FK,
    # но включать только те что есть в pg_meta.
    target_tables = [
        sqlite_meta.tables[t.name]
        for t in sqlite_meta.sorted_tables
        if t.name in pg_meta.tables and t.name not in SKIP_TABLES
    ]
    pg_tables_by_name = {t.name: t for t in pg_meta.sorted_tables}

    # ------------------------------------------------------------------
    # Phase 1: snapshot row counts.
    # ------------------------------------------------------------------
    src_counts: dict[str, int] = {}
    with sqlite_engine.connect() as src:
        for table in target_tables:
            src_counts[table.name] = _row_count(src, table)
    total_rows = sum(src_counts.values())
    print(f"[snapshot] {len(src_counts)} tables, {total_rows} total rows")

    # ------------------------------------------------------------------
    # Phase 2: TRUNCATE PG. session_replication_role=replica отключает
    # FK-triggers (cycle bypass + clears alembic seed rows).
    # ------------------------------------------------------------------
    with pg_engine.begin() as dst:
        dst.execute(sa.text("SET session_replication_role = replica"))
        for table in reversed(target_tables):
            dst_table = pg_tables_by_name[table.name]
            dst.execute(dst_table.delete())
    print(f"[truncate] cleared {len(target_tables)} target tables")

    # ------------------------------------------------------------------
    # Phase 3: copy data. ONE connection — replica-mode держится.
    # ------------------------------------------------------------------
    t_start = time.time()
    with pg_engine.begin() as dst:
        dst.execute(sa.text("SET session_replication_role = replica"))
        for src_table in target_tables:
            n_expected = src_counts[src_table.name]
            if n_expected == 0:
                print(f"[skip ] {src_table.name:<40} 0 rows")
                continue

            t0 = time.time()
            with sqlite_engine.connect() as src:
                rows = [dict(r._mapping) for r in src.execute(select(src_table)).all()]

            dst_table = pg_tables_by_name[src_table.name]
            n_inserted = 0
            for batch in _chunks(rows, BATCH_SIZE):
                dst.execute(dst_table.insert(), batch)
                n_inserted += len(batch)

            elapsed = time.time() - t0
            rate = n_inserted / elapsed if elapsed > 0 else float("inf")
            print(f"[load ] {src_table.name:<40} {n_inserted:>6} rows in {elapsed:>5.2f}s ({rate:>6.0f} rps)")

    # ------------------------------------------------------------------
    # Phase 4: verify counts.
    # ------------------------------------------------------------------
    print()
    print("[verify] comparing counts...")
    mismatches: list[tuple[str, int, int]] = []
    with pg_engine.connect() as dst:
        for src_table in target_tables:
            expected = src_counts[src_table.name]
            actual = _row_count(dst, pg_tables_by_name[src_table.name])
            if actual != expected:
                mismatches.append((src_table.name, expected, actual))
                print(f"[MISMATCH] {src_table.name}: SQLite={expected} PG={actual}")

    total_elapsed = time.time() - t_start
    if mismatches:
        print(f"\n[FAIL] {len(mismatches)} table(s) mismatched. Total time {total_elapsed:.1f}s.")
        return 1

    print(f"\n[ok] {len(target_tables)} tables, {total_rows} rows migrated in {total_elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
