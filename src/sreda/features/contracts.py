from __future__ import annotations

from typing import Protocol

from fastapi import FastAPI


class FeatureModule(Protocol):
    feature_key: str

    def register_api(self, app: FastAPI) -> None: ...

    def register_runtime(self) -> None: ...

    def register_workers(self) -> None: ...
