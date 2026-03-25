from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import User
from sreda.db.repositories.seed import SeedRepository

INITIAL_TELEGRAM_ID = "100000001"
UPDATED_TELEGRAM_ID = "100000002"


def test_seed_repository_updates_existing_user_telegram_account_id() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    repository = SeedRepository(session)

    repository.ensure_tenant_bundle(
        tenant_id="tenant_1",
        tenant_name="Tenant 1",
        workspace_id="workspace_1",
        workspace_name="Workspace 1",
        user_id="user_1",
        telegram_account_id=INITIAL_TELEGRAM_ID,
        assistant_id="assistant_1",
        assistant_name="Assistant 1",
        eds_monitor_enabled=True,
    )
    repository.ensure_tenant_bundle(
        tenant_id="tenant_1",
        tenant_name="Tenant 1",
        workspace_id="workspace_1",
        workspace_name="Workspace 1",
        user_id="user_1",
        telegram_account_id=UPDATED_TELEGRAM_ID,
        assistant_id="assistant_1",
        assistant_name="Assistant 1",
        eds_monitor_enabled=True,
    )

    user = session.get(User, "user_1")

    assert user is not None
    assert user.telegram_account_id == UPDATED_TELEGRAM_ID
