from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, TenantFeature
from sreda.services.features import tenant_has_feature


def test_tenant_has_feature_lookup() -> None:
    # #181 Phase B: the eds_monitor-specific helpers were removed with the
    # retired skill. The generic ``tenant_has_feature`` still backs per-tenant
    # feature checks (e.g. voice_transcription, housewife_assistant).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(
        TenantFeature(
            id="tenant_1:voice_transcription",
            tenant_id="tenant_1",
            feature_key="voice_transcription",
            enabled=True,
        )
    )
    session.commit()

    assert tenant_has_feature(session, "tenant_1", "voice_transcription") is True
    assert tenant_has_feature(session, "tenant_1", "core_assistant") is False
