from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import FastAPI

from sreda.features.skill_contracts import SkillManifestBase


class FeatureModule(Protocol):
    """Protocol every feature module (= skill package) implements.

    ``get_manifest()`` is optional — existing skills without a manifest
    continue to work because the registry accesses it via ``getattr(..., None)``.
    New skills (and skills migrated to the Phase 0 platform contract) should
    return a ``SkillManifestBase``.
    """

    feature_key: str

    def register_api(self, app: FastAPI) -> None: ...

    def register_runtime(self) -> None: ...

    def register_workers(self) -> None: ...


@runtime_checkable
class ManifestedFeatureModule(Protocol):
    """Optional extension: skills that want platform-level lifecycle/config.

    Used at ``isinstance(module, ManifestedFeatureModule)`` time to detect
    whether a registered module exposes a manifest we can store."""

    feature_key: str

    def get_manifest(self) -> SkillManifestBase: ...
