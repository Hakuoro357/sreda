"""#138 Ф5-5b: юниты identity-фазы — резолв-хелпер + INSERT-only провижн.

PG-специфика (DEFINER, роли, permission denied) — в red-suite
``tests/integration/test_138_identity_pg.py``. Здесь — семантика Python-слоя.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ── resolve_external_identity: маппинг match_count ──────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeBind:
    class dialect:  # noqa: N801 — имитируем sqlalchemy dialect
        name = "postgresql"


class _FakeSession:
    """Ловит SQL + параметры, отдаёт канированные строки. bind=PG → DEFINER-путь."""

    bind = _FakeBind()

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        return _FakeResult(self.rows)


def _resolve_with(rows, channel="telegram", key="352612382"):
    from sreda.services.identity_resolve import resolve_external_identity

    fake = _FakeSession(rows)
    out = resolve_external_identity(channel, key, session=fake)
    return out, fake


def test_resolve_unknown_returns_none():
    out, _ = _resolve_with([])
    assert out is None


def test_resolve_single_returns_resolved():
    approved = datetime(2026, 7, 1, tzinfo=timezone.utc)
    out, _ = _resolve_with([("tenant_tg_1", "user_tg_1", approved, None, 1)])
    assert out.tenant_id == "tenant_tg_1"
    assert out.user_id == "user_tg_1"
    assert out.approved_at == approved
    assert out.tenant_deleted_at is None


def test_resolve_ambiguous_raises():
    from sreda.services.identity_resolve import AmbiguousExternalIdentity

    with pytest.raises(AmbiguousExternalIdentity):
        _resolve_with([(None, None, None, None, 2)], channel="max", key="40921122")


def test_resolve_telegram_key_is_hashed_max_is_raw():
    """TG: в SQL уходит ХЕШ (152-ФЗ), не сырой chat_id; MAX: сырой account_id."""
    from sreda.services.tg_account_hash import hash_tg_account

    _, fake_tg = _resolve_with([], channel="telegram", key="352612382")
    assert fake_tg.calls[0][1]["k"] == hash_tg_account("352612382")
    assert "352612382" not in str(fake_tg.calls[0][1]["k"])

    _, fake_max = _resolve_with([], channel="max", key="40921122")
    assert fake_max.calls[0][1]["k"] == "40921122"


def test_resolve_rejects_unknown_channel():
    with pytest.raises(ValueError):
        _resolve_with([], channel="icq", key="x")


# ── INSERT-only провижн: идемпотентность без pre-SELECT/UPDATE ───────────────

@pytest.fixture()
def session(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False)()
    yield s
    s.close()


def _provision(session, **kw):
    from sreda.db.repositories.seed import insert_only_bundle

    defaults = dict(
        tenant_id="tenant_tg_99", tenant_name="Семья",
        workspace_id="ws_99", workspace_name="Дом",
        user_id="user_tg_99", telegram_account_id="99",
        assistant_id="as_99", assistant_name="Среда",
    )
    defaults.update(kw)
    return insert_only_bundle(session, **defaults)


def test_insert_only_bundle_creates_approved_tenant(session):
    from sreda.db.models.core import Tenant, TenantFeature, User

    _provision(session)
    t = session.get(Tenant, "tenant_tg_99")
    assert t is not None
    assert t.approved_at is not None, "авто-одобрение в INSERT, не UPDATE'ом после"
    assert session.get(User, "user_tg_99").tenant_id == "tenant_tg_99"
    assert session.get(TenantFeature, "tenant_tg_99:core_assistant").enabled is True


def test_insert_only_bundle_double_call_is_noop(session):
    from sqlalchemy import func, select

    from sreda.db.models.core import User

    _provision(session)
    _provision(session)  # ретрай/гонка — не падает и не дублирует
    n = session.execute(
        select(func.count()).select_from(User).where(User.tenant_id == "tenant_tg_99")
    ).scalar()
    assert n == 1


def test_insert_only_bundle_never_updates_existing(session):
    """Существующий бандл НЕ трогается (перепривязка — отдельный tenant-фазный путь)."""
    from sreda.db.models.core import User

    _provision(session)
    u = session.get(User, "user_tg_99")
    u.last_bot_key = "bot_custom"
    session.commit()

    _provision(session)  # повтор не перезаписывает
    session.expire_all()
    assert session.get(User, "user_tg_99").last_bot_key == "bot_custom"
