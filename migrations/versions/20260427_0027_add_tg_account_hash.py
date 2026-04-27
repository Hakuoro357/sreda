"""add tg_account_hash to users + backfill (152-ФЗ обезличивание Часть 1)

Plaintext `telegram_account_id` — личный идентификатор юзера в Telegram,
по 152-ФЗ это ПДн. Чтобы платформа не была «оператором ПДн», переходим
на двухколоночную схему:

  * `tg_account_hash` (новая колонка, эта миграция) — HMAC-SHA256 от
    chat_id под salt'ом из env `SREDA_TG_ACCOUNT_SALT`. По нему идёт
    O(1) lookup при входящем webhook'е.
  * `telegram_account_id` (существующая колонка) — становится
    зашифрованной (миграция 0028 переписывает все ряды через
    `encrypt_value`).

Эта миграция:
  1. Добавляет колонку `tg_account_hash` (String 64, nullable, unique).
  2. Создаёт индекс `ix_users_tg_account_hash`.
  3. Backfill — для каждого user'а с непустым `telegram_account_id`
     вычисляет hash и пишет в новую колонку.

Если salt не задан в env — миграция падает (RuntimeError). Это
сознательный выбор: молча создать колонку без backfill = тихая поломка
lookup'а после рестарта (юзеры станут «незнакомцами»).

Revision ID: 20260427_0027
Revises: 20260425_0026
Create Date: 2026-04-27
"""

from __future__ import annotations

import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op

revision = "20260427_0027"
down_revision = "20260425_0026"
branch_labels = None
depends_on = None


def _resolve_salt() -> str:
    """Достаёт SREDA_TG_ACCOUNT_SALT напрямую из env, минуя Settings.

    Settings импортирует pydantic-settings + лезет в .env, что в
    alembic-окружении (особенно в тестах с in-memory db) — лишний
    сайд-эффект. Прямое чтение env проще и надёжнее.
    """
    salt = (os.environ.get("SREDA_TG_ACCOUNT_SALT") or "").strip()
    if not salt:
        raise RuntimeError(
            "SREDA_TG_ACCOUNT_SALT не задан — миграция 0027 (backfill "
            "tg_account_hash) не может вычислить hash. Сгенерируй: "
            "`python -c 'import secrets; print(secrets.token_hex(32))'` "
            "и добавь в env (launch-sreda.sh) перед `alembic upgrade head`."
        )
    return salt


def _hash(salt: str, chat_id: str) -> str:
    return hmac.new(
        salt.encode("utf-8"),
        str(chat_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tg_account_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_users_tg_account_hash",
        "users",
        ["tg_account_hash"],
        unique=True,
    )

    # Backfill — единственная стратегия, при которой существующие 8
    # тенантов на проде продолжают находиться lookup'ом по hash после
    # рестарта.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, telegram_account_id FROM users "
            "WHERE telegram_account_id IS NOT NULL "
            "AND telegram_account_id != ''"
        )
    ).fetchall()
    if rows:
        salt = _resolve_salt()
        for user_id, plain in rows:
            h = _hash(salt, plain)
            conn.execute(
                sa.text(
                    "UPDATE users SET tg_account_hash = :h WHERE id = :uid"
                ),
                {"h": h, "uid": user_id},
            )


def downgrade() -> None:
    op.drop_index("ix_users_tg_account_hash", table_name="users")
    op.drop_column("users", "tg_account_hash")
