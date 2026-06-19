from __future__ import annotations

from importlib import import_module

from sreda.domain.tenants.features import is_feature_disabled
from sreda.features.registry import FeatureRegistry


def load_feature_modules(module_paths: list[str], registry: FeatureRegistry) -> None:
    for module_path in module_paths:
        module = import_module(module_path)

        # #181 defense-in-depth prefilter: a module that exposes a
        # ``feature_module`` for a retired skill is skipped before any
        # registration side-effect. (FeatureRegistry.register also guards;
        # this stops the import-driven ``register()`` path from wiring API
        # routes / runtime / workers for a disabled feature.)
        feature_key = getattr(getattr(module, "feature_module", None), "feature_key", None)
        if feature_key is not None and is_feature_disabled(feature_key):
            continue

        if hasattr(module, "register"):
            module.register(registry)
            continue

        feature_module = getattr(module, "feature_module", None)
        if feature_module is not None:
            registry.register(feature_module)
            continue

        raise RuntimeError(f"Feature module '{module_path}' does not expose register() or feature_module")
