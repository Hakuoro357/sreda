"""#187 Фаза 0 — `is_tenant_active` fail-closed (чеклист приёмки A12).

A12: Given нет строки тенанта → False; given `deleted_at IS NULL` → True;
given `deleted_at` не null → False. Источник истины рубильника soft-delete.
RED до реализации `sreda.services.tenant_lifecycle`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tests.unit.conftest import seed_telegram_user
from sreda.services.tenant_lifecycle import is_tenant_active
from sreda.db.models.core import Tenant


def test_active_tenant_is_active(db_session):
    seed_telegram_user(db_session, tenant_id="t_active")
    db_session.flush()  # симметрия с deleted-тестом; autoflush=True и так материализовал бы
    assert is_tenant_active(db_session, "t_active") is True


def test_deleted_tenant_is_not_active(db_session):
    seed_telegram_user(db_session, tenant_id="t_deleted")
    db_session.get(Tenant, "t_deleted").deleted_at = datetime.now(timezone.utc)
    db_session.flush()
    assert is_tenant_active(db_session, "t_deleted") is False


def test_missing_tenant_is_not_active(db_session):
    # fail-closed: нет строки → False (НЕ True, НЕ исключение)
    assert is_tenant_active(db_session, "no_such_tenant") is False
