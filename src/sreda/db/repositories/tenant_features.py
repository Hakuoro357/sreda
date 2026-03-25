from sqlalchemy import select

from sreda.db.models.core import TenantFeature
from sreda.db.repositories.base import Repository


class TenantFeatureRepository(Repository):
    def is_enabled(self, tenant_id: str, feature_key: str) -> bool:
        stmt = select(TenantFeature).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature_key == feature_key,
            TenantFeature.enabled.is_(True),
        )
        return self.session.execute(stmt).scalar_one_or_none() is not None
