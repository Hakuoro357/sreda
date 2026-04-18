from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import FastAPI
from sqlalchemy.orm import Session

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


@dataclass(slots=True, frozen=True)
class MiniAppSection:
    """One Mini App home-screen entry contributed by an agent.

    Multiple agents may contribute sections with the same ``id``
    (e.g. reminders from housewife + teamlead) — the Mini App menu
    endpoint de-duplicates by id and sums counters. Order-stable:
    the section from the first-registered agent wins title/icon
    if there's a collision.
    """

    id: str
    title: str
    icon: str
    route: str
    subtitle: str | None = None
    count: int | None = None


@runtime_checkable
class MiniAppSectionsProvider(Protocol):
    """Optional extension: agents that contribute entries to the Mini
    App home screen (reminders list, tasks, etc.). Agents that only
    want to appear in the subscription catalog don't implement this.
    """

    feature_key: str

    def get_miniapp_sections(
        self, session: Session, tenant_id: str, user_id: str | None
    ) -> list[MiniAppSection]: ...
