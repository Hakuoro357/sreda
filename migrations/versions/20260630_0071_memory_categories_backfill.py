"""#262 A2: backfill — Common всем (tenant,user) с памятью + проставить category_id. Идемпотентна.

ВАЖНО (порядок деплоя, план #262): запускать ПОСЛЕ выкатки+рестарта кода, где save() резолвит Common —
иначе факты, записанные между backfill и рестартом, останутся с category_id=NULL и уронят A3 (NOT NULL).
Прод = Postgres (вся цепочка миграций не гоняется на SQLite — есть pre-existing ALTER-only миграции).

Ревью R1:
- M2: миграция-локальный Core-SQL через op.get_bind() — БЕЗ ORM Session и БЕЗ session.commit() (коммитит
  alembic). Не импортируем MemoryRepository (историческая стабильность миграции).
- name_normalized у Common берём через normalize_for_dedup (ЧИСТАЯ leaf-функция, не ORM) — чтобы значение
  СОВПАДАЛО с тем, что repo.ensure_common пишет в том же прод-venv (иначе у backfill-юзеров «Общее» от
  пользователя не конфликтовало бы с их Common; C2). Это значение-консистентность, а не поведенческая связь.
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "20260630_0071"
down_revision = "20260630_0070"
branch_labels = None
depends_on = None

_SLUG = "common"
_NAME = "Общее"
# Заморожено (R2): normalize_for_dedup("Общее") == "общий" (pymorphy3) на момент написания. НЕ импортируем
# нормализатор в миграцию (историческая стабильность — alembic-ревизии не должны зависеть от будущего
# поведения app-кода). Дрейф сторожит unit-тест test_common_name_normalized_frozen_literal: если лемма
# изменится, тест покраснеет → обновить этот литерал И значение в repo не разойдётся (там оно считается живо).
_NAME_NORMALIZED = "общий"


def upgrade() -> None:
    bind = op.get_bind()
    norm = _NAME_NORMALIZED  # замороженный литерал (см. выше); repo считает то же значение живо

    # 1) Common на каждый (tenant,user) с памятью, у кого его ещё нет.
    #    ON CONFLICT по partial-unique uq_memory_categories_one_system → идемпотентно (повтор не плодит).
    pairs = bind.execute(
        sa.text("SELECT DISTINCT tenant_id, user_id FROM assistant_memories WHERE category_id IS NULL")
    ).fetchall()
    insert_common = sa.text(
        """
        INSERT INTO memory_categories
            (id, tenant_id, user_id, slug, name, name_normalized, is_system, created_at)
        VALUES (:id, :tenant_id, :user_id, :slug, :name, :norm, true, now())
        ON CONFLICT (tenant_id, user_id) WHERE is_system DO NOTHING
        """
    )
    for tenant_id, user_id in pairs:
        bind.execute(
            insert_common,
            {"id": "memcat_" + uuid.uuid4().hex[:24], "tenant_id": tenant_id, "user_id": user_id,
             "slug": _SLUG, "name": _NAME, "norm": norm},
        )

    # 2) Проставить category_id фактам без неё → Common их (tenant,user). Скоуп: только NULL, только своя Common.
    bind.execute(
        sa.text(
            """
            UPDATE assistant_memories AS a
            SET category_id = c.id
            FROM memory_categories AS c
            WHERE a.category_id IS NULL
              AND c.is_system = true AND c.slug = :slug
              AND c.tenant_id = a.tenant_id AND c.user_id = a.user_id
            """
        ),
        {"slug": _SLUG},
    )


def downgrade() -> None:
    # M3 (ревью R1): НЕ обнуляем category_id — это стёрло бы и пользовательские назначения, сделанные после
    # backfill (потеря данных). Под nullable-схемой 0070 NULL не требуется; при дальнейшем downgrade до 0070
    # колонка и таблица memory_categories дропаются целиком. Поэтому downgrade здесь — осознанный no-op.
    pass
