from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from sqlalchemy.orm import Session

from sreda.features.contracts import FeatureModule, ManifestedFeatureModule
from sreda.features.skill_contracts import SkillManifestBase


SkillJobHandler = Callable[..., Awaitable[Any]]
"""Signature: ``async def handler(session, job, run_id, attempt_id) -> None``.

Concrete keyword signature is flexible — the runner passes ``session``
positionally and the rest as kwargs, so handlers should accept
``(session, *, job, run_id, attempt_id)`` or ``(session, **kwargs)``."""


@dataclass(frozen=True)
class RegisteredSkillJobHandler:
    feature_key: str
    job_type: str
    handler: SkillJobHandler


class FeatureRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, FeatureModule] = {}
        self._skill_job_handlers: dict[str, RegisteredSkillJobHandler] = {}

    # ------------------------------------------------------------- modules

    def register(self, module: FeatureModule) -> None:
        self._modules[module.feature_key] = module

    def register_api(self, app: FastAPI) -> None:
        for module in self._modules.values():
            module.register_api(app)

    def register_runtime(self) -> None:
        for module in self._modules.values():
            module.register_runtime()

    def register_workers(self) -> None:
        for module in self._modules.values():
            module.register_workers()

    @property
    def modules(self) -> dict[str, FeatureModule]:
        return dict(self._modules)

    # ----------------------------------------------------------- manifests

    def get_manifest(self, feature_key: str) -> SkillManifestBase | None:
        module = self._modules.get(feature_key)
        if module is None:
            return None
        getter = getattr(module, "get_manifest", None)
        if getter is None:
            return None
        return getter()

    def iter_manifests(self) -> list[SkillManifestBase]:
        out: list[SkillManifestBase] = []
        for module in self._modules.values():
            if isinstance(module, ManifestedFeatureModule):
                out.append(module.get_manifest())
        return out

    def initialize_tenant_skill_states(self, session: Session, tenant_id: str) -> None:
        """Lazy-create platform state rows for every registered skill that
        has a manifest. Idempotent. Callers own the transaction."""
        from sreda.db.repositories.skill_platform import SkillPlatformRepository

        repo = SkillPlatformRepository(session)
        for manifest in self.iter_manifests():
            repo.ensure_manifest_state(tenant_id, manifest)

    # -------------------------------------------------- skill job handlers

    def register_skill_job_handler(
        self,
        *,
        feature_key: str,
        job_type: str,
        handler: SkillJobHandler,
    ) -> None:
        """Register a handler invoked by ``SkillPlatformJobProcessor`` when
        it picks up a pending ``Job`` of the given ``job_type``. The handler
        runs inside a ``skill_run_attempt`` wrapper that already created the
        platform run/attempt rows."""
        if job_type in self._skill_job_handlers:
            raise ValueError(
                f"skill job handler already registered for job_type={job_type!r}"
            )
        self._skill_job_handlers[job_type] = RegisteredSkillJobHandler(
            feature_key=feature_key,
            job_type=job_type,
            handler=handler,
        )

    def get_skill_job_handler(self, job_type: str) -> RegisteredSkillJobHandler | None:
        return self._skill_job_handlers.get(job_type)

    def skill_job_types(self) -> list[str]:
        return list(self._skill_job_handlers.keys())
