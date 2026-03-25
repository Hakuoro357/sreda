from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, TenantFeature
from sreda.services.features import tenant_can_use_eds_monitor_from_db


def test_eds_monitor_feature_lookup() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(
        TenantFeature(
            id="tenant_1:eds_monitor",
            tenant_id="tenant_1",
            feature_key="eds_monitor",
            enabled=True,
        )
    )
    session.commit()

    assert tenant_can_use_eds_monitor_from_db(session, "tenant_1") is True
