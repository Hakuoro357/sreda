from sqlalchemy.orm import Session

from sreda.db.repositories.tenant_features import TenantFeatureRepository


def tenant_has_feature(session: Session, tenant_id: str, feature_key: str) -> bool:
    repository = TenantFeatureRepository(session)
    return repository.is_enabled(tenant_id, feature_key)
