"""encrypt message content + tenant/profile names (152-ФЗ Часть 2)

Завершает шифрование контента переписки. Часть 1 (миграции 0027/0028)
обезличила Telegram chat_id. Сейчас зашифровываем оставшиеся колонки
с PII / контентом сообщений:

  * tenants.name                                — display name тенанта
                                                  (Telegram first/last)
  * tenant_user_profiles.display_name           — имя обращения
                                                  («Борис», «Шеф»)
  * outbox_messages.payload_json                — LLM-ответы бота
  * inbound_messages.message_text_sanitized     — входящие текстовые
                                                  сообщения после
                                                  privacy_guard
  * inbound_events.payload_json                 — payload skill events
  * jobs.payload_json                           — job arguments

Стратегия как в 0028: raw SELECT plaintext → encrypt_value() через
encryption module → raw UPDATE с envelope `v2:`. Идемпотентно (ряды
с префиксом `v1:`/`v2:` пропускаются).

ВАЖНО: модели (`db/models/core.py`, `inbound_event.py`, `user_profile.py`)
после этой миграции декларируют те же колонки как `EncryptedString()`.
TypeDecorator расшифровывает прозрачно при чтении — поведение API не
меняется. Backwards-compat для legacy plaintext (без префикса) остаётся.

Revision ID: 20260428_0029
Revises: 20260428_0028
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260428_0029"
down_revision = "20260427_0028"
branch_labels = None
depends_on = None


_ENCRYPTED_PREFIXES = ("v1:", "v2:")


# (таблица, колонка) — шифруем все non-null значения этих колонок
_TARGETS: tuple[tuple[str, str], ...] = (
    ("tenants", "name"),
    ("tenant_user_profiles", "display_name"),
    ("outbox_messages", "payload_json"),
    ("inbound_messages", "message_text_sanitized"),
    ("inbound_events", "payload_json"),
    ("jobs", "payload_json"),
)


def _encrypt_table_column(conn: sa.engine.Connection, table: str, column: str) -> int:
    """Зашифровать все non-null значения колонки. Возвращает count
    обработанных рядов (без skipped). Идемпотентно: уже зашифрованные
    значения пропускаются.
    """
    from sreda.services.encryption import encrypt_value

    # Используем PK `id` для UPDATE'а; во всех затронутых таблицах PK
    # называется `id` (проверено).
    rows = conn.execute(
        sa.text(
            f"SELECT id, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} != ''"
        )
    ).fetchall()

    processed = 0
    for row_id, value in rows:
        if not isinstance(value, str):
            continue
        if value.startswith(_ENCRYPTED_PREFIXES):
            continue  # уже зашифровано — повторный прогон, скип
        ciphertext = encrypt_value(value)
        conn.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :v WHERE id = :rid"
            ),
            {"v": ciphertext, "rid": row_id},
        )
        processed += 1
    return processed


def _decrypt_table_column(conn: sa.engine.Connection, table: str, column: str) -> int:
    """Reverse of _encrypt_table_column. Расшифровывает обратно в
    plaintext (для downgrade). Идемпотентно: уже plaintext значения
    пропускаются.
    """
    from sreda.services.encryption import decrypt_value

    rows = conn.execute(
        sa.text(
            f"SELECT id, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} != ''"
        )
    ).fetchall()

    processed = 0
    for row_id, value in rows:
        if not isinstance(value, str):
            continue
        if not value.startswith(_ENCRYPTED_PREFIXES):
            continue  # уже plaintext — скип
        plaintext = decrypt_value(value)
        conn.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :v WHERE id = :rid"
            ),
            {"v": plaintext, "rid": row_id},
        )
        processed += 1
    return processed


def upgrade() -> None:
    conn = op.get_bind()
    for table, column in _TARGETS:
        _encrypt_table_column(conn, table, column)


def downgrade() -> None:
    """Расшифровать обратно в plaintext.

    Если ключ шифрования утерян — downgrade невозможен (data lost).
    Это осознанный риск 152-ФЗ Часть 2: невозможность recovery —
    фича, а не баг. Backup на проде снят перед миграцией
    (см. /Users/boris/sreda-backup/pre-part2-*.db).
    """
    conn = op.get_bind()
    for table, column in _TARGETS:
        _decrypt_table_column(conn, table, column)
