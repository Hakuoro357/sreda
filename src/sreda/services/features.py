from sqlalchemy.orm import Session

from sreda.db.repositories.tenant_features import TenantFeatureRepository
from sreda.domain.tenants.features import EDS_MONITOR, is_feature_enabled


def tenant_can_use_eds_monitor(feature_map: dict[str, bool]) -> bool:
    return is_feature_enabled(feature_map, EDS_MONITOR)


def tenant_has_feature(session: Session, tenant_id: str, feature_key: str) -> bool:
    repository = TenantFeatureRepository(session)
    return repository.is_enabled(tenant_id, feature_key)


def tenant_can_use_eds_monitor_from_db(session: Session, tenant_id: str) -> bool:
    return tenant_has_feature(session, tenant_id, EDS_MONITOR)
