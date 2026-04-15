"""Process-wide cached ``FeatureRegistry`` for the running app.

The API process builds the registry once in ``create_app()`` and then
stashes it on ``app.state``. The job runner, however, runs in the same
process but outside the FastAPI lifecycle — it needs the same registry
so that ``SkillPlatformJobProcessor`` knows which skill job handlers are
registered.

This module exposes a single ``get_feature_registry()`` singleton.
Tests that need a clean registry should use ``FeatureRegistry`` directly
(the tests in ``test_skill_platform.py`` do exactly that)."""

from __future__ import annotations

from functools import lru_cache

from sreda.config.settings import get_settings
from sreda.features.builtin import CoreAssistantFeature
from sreda.features.loader import load_feature_modules
from sreda.features.registry import FeatureRegistry


@lru_cache
def get_feature_registry() -> FeatureRegistry:
    settings = get_settings()
    registry = FeatureRegistry()
    registry.register(CoreAssistantFeature())
    load_feature_modules(settings.feature_modules, registry)
    return registry
