from datetime import datetime, timezone

from sreda.db.models.core import Assistant, Tenant, TenantFeature, User, Workspace
from sreda.db.repositories.base import Repository


class SeedRepository(Repository):
    def ensure_tenant_bundle(
        self,
        *,
        tenant_id: str,
        tenant_name: str,
        workspace_id: str,
        workspace_name: str,
        user_id: str,
        telegram_account_id: str,
        assistant_id: str,
        assistant_name: str,
        eds_monitor_enabled: bool,
    ) -> None:
        now = datetime.now(timezone.utc)

        tenant = self.session.get(Tenant, tenant_id)
        if tenant is None:
            self.session.add(Tenant(id=tenant_id, name=tenant_name, created_at=now))

        workspace = self.session.get(Workspace, workspace_id)
        if workspace is None:
            self.session.add(Workspace(id=workspace_id, tenant_id=tenant_id, name=workspace_name))

        user = self.session.get(User, user_id)
        if user is None:
            self.session.add(User(id=user_id, tenant_id=tenant_id, telegram_account_id=telegram_account_id))
        else:
            user.telegram_account_id = telegram_account_id

        assistant = self.session.get(Assistant, assistant_id)
        if assistant is None:
            self.session.add(
                Assistant(
                    id=assistant_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    name=assistant_name,
                )
            )

        self._ensure_feature(tenant_id, "core_assistant", True)
        self._ensure_feature(tenant_id, "eds_monitor", eds_monitor_enabled)
        self.session.commit()

    def _ensure_feature(self, tenant_id: str, feature_key: str, enabled: bool) -> None:
        entity = (
            self.session.query(TenantFeature)
            .filter(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key == feature_key,
            )
            .one_or_none()
        )
        if entity is None:
            entity = TenantFeature(
                id=f"{tenant_id}:{feature_key}",
                tenant_id=tenant_id,
                feature_key=feature_key,
                enabled=enabled,
            )
            self.session.add(entity)
            return
        entity.enabled = enabled
