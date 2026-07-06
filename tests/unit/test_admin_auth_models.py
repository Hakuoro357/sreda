"""#305 Фаза 1 — схема admin auth (admin_sessions + admin_login_challenges).

Машинные пункты чеклиста: модели в Base.metadata (иначе не мигрируются),
ключевые колонки/уникальность на месте, схема аддитивна (create_all).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import AdminLoginChallenge, AdminSession


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)  # схема-first аддитивна
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_models_registered_in_metadata():
    tables = Base.metadata.tables
    assert "admin_sessions" in tables
    assert "admin_login_challenges" in tables


def test_admin_session_columns_and_unique(session):
    now = datetime.now(UTC)
    session.add(AdminSession(
        id="s1", session_hash="h1", tg_id="352612382", auth_method="telegram",
        created_at=now, expires_at=now + timedelta(hours=24)))
    session.commit()
    row = session.execute(select(AdminSession)).scalar_one()
    assert row.tg_id == "352612382" and row.revoked_at is None
    # session_hash unique
    session.add(AdminSession(
        id="s2", session_hash="h1", tg_id="x", auth_method="telegram",
        created_at=now, expires_at=now + timedelta(hours=24)))
    with pytest.raises(IntegrityError):
        session.commit()


def test_challenge_columns_and_defaults(session):
    now = datetime.now(UTC)
    ch = AdminLoginChallenge(
        id="c1", challenge_id="pubhandle1", browser_bind_hash="bh1",
        human_code="AB12", status="pending", client_ip="1.2.3.4",
        expires_at=now + timedelta(minutes=5), created_at=now)
    session.add(ch)
    session.commit()
    row = session.execute(select(AdminLoginChallenge)).scalar_one()
    assert row.status == "pending"
    assert row.button_send_claimed_at is None      # claim-слот пуст
    assert row.tg_id is None and row.message_id is None and row.consumed_at is None
    # challenge_id unique
    session.add(AdminLoginChallenge(
        id="c2", challenge_id="pubhandle1", browser_bind_hash="bh2",
        human_code="CD34", status="pending", expires_at=now, created_at=now))
    with pytest.raises(IntegrityError):
        session.commit()


def test_single_alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"ожидалась одна голова, а не {heads}"
