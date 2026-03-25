from __future__ import annotations

from fastapi import FastAPI

from sreda.features.contracts import FeatureModule


class FeatureRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, FeatureModule] = {}

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
