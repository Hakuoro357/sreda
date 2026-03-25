from __future__ import annotations

from importlib import import_module

from sreda.features.registry import FeatureRegistry


def load_feature_modules(module_paths: list[str], registry: FeatureRegistry) -> None:
    for module_path in module_paths:
        module = import_module(module_path)

        if hasattr(module, "register"):
            module.register(registry)
            continue

        feature_module = getattr(module, "feature_module", None)
        if feature_module is not None:
            registry.register(feature_module)
            continue

        raise RuntimeError(f"Feature module '{module_path}' does not expose register() or feature_module")
