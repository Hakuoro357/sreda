"""encrypt existing telegram_account_id rows (152-ФЗ обезличивание Часть 1)

Завершает обезличивание Telegram-идентификатора:

  * Колонка `users.telegram_account_id` остаётся типом Text в SQL.
  * Модель `User` теперь декларирует её как `EncryptedString` —
    TypeDecorator поверх Text. На read возвращает plaintext (с
    backwards-compat для legacy plaintext-рядов без `v1:` envelope),
    на write шифрует.
  * Эта миграция rewrite'ит все ряды: читает текущий plaintext,
    шифрует через `encrypt_value()`, пишет обратно. После — каждое
    значение в БД имеет envelope `v2:...` и в дампе невозможно
    восстановить plain chat_id без ключа шифрования.

Strategy: raw SQL для чтения + python-уровень шифрование + raw SQL
UPDATE. ORM не используем, потому что ORM для записи через
EncryptedString пошлёт обратно зашифрованное — но мы хотим явный
контроль над «было plain, стало encrypted».

Идемпотентность: пропускаем ряды, у которых значение уже начинается
на `v1:` или `v2:` (envelope от encryption module). Это безопасно
запускать повторно.

Revision ID: 20260427_0028
Revises: 20260427_0027
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260427_0028"
down_revision = "20260427_0027"
branch_labels = None
depends_on = None


_ENCRYPTED_PREFIXES = ("v1:", "v2:")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, telegram_account_id FROM users "
            "WHERE telegram_account_id IS NOT NULL "
            "AND telegram_account_id != ''"
        )
    ).fetchall()
    if not rows:
        return

    # Lazy import — encryption module хочет настройки и crypto-bindings,
    # которые не нужны в тестах с пустой БД.
    from sreda.services.encryption import encrypt_value

    for user_id, value in rows:
        if not isinstance(value, str):
            continue
        if value.startswith(_ENCRYPTED_PREFIXES):
            # уже зашифровано — миграция повторная, скип
            continue
        ciphertext = encrypt_value(value)
        conn.execute(
            sa.text(
                "UPDATE users SET telegram_account_id = :v WHERE id = :uid"
            ),
            {"v": ciphertext, "uid": user_id},
        )


def downgrade() -> None:
    """Расшифровываем обратно (для отката миграции).

    Если ключ шифрования утерян — downgrade невозможен; это
    осознанный риск (152-ФЗ: невозможность recovery — фича, а не баг).
    """
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, telegram_account_id FROM users "
            "WHERE telegram_account_id IS NOT NULL "
            "AND telegram_account_id != ''"
        )
    ).fetchall()
    if not rows:
        return

    from sreda.services.encryption import decrypt_value

    for user_id, value in rows:
        if not isinstance(value, str):
            continue
        if not value.startswith(_ENCRYPTED_PREFIXES):
            # уже plain — скип
            continue
        plaintext = decrypt_value(value)
        conn.execute(
            sa.text(
                "UPDATE users SET telegram_account_id = :v WHERE id = :uid"
            ),
            {"v": plaintext, "uid": user_id},
        )
