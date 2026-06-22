"""#187 soft-delete — рубильник жизненного цикла тенанта (Фаза 0: `is_tenant_active`).

Источник истины soft-delete: один флаг `tenants.deleted_at` + одна детерминированная
проверка `is_tenant_active`. Проверка вынесена ОТДЕЛЬНО, НЕ внутрь `EntitlementGate.check`
(тот зовётся из 8 мест, а `allowed` чтит лишь в 2 — голос/инструменты её пропустят; см.
plan `db-fix-tenant-deletion-plan.md`). Вызывается fail-closed во всех дверях отсечения.

Дальнейшие фазы добавят сюда `soft_delete_tenant` / `restore_tenant` (флаг + барьер + drain).
"""
from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from sreda.db.models.core import Tenant


def is_tenant_active(session: Session, tenant_id: str) -> bool:
    """True ⟺ тенант существует И НЕ помечен удалённым (`deleted_at IS NULL`).

    **Fail-closed:** нет строки тенанта → ``False`` (не исключение, не ``True``).
    Реализовано через ``EXISTS`` — не тащит строку, дёшево на горячем пути.
    """
    stmt = select(
        exists().where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
    )
    return bool(session.execute(stmt).scalar())
