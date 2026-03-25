from __future__ import annotations

from fastapi import FastAPI


class CoreAssistantFeature:
    feature_key = "core_assistant"

    def register_api(self, app: FastAPI) -> None:
        _ = app

    def register_runtime(self) -> None:
        return None

    def register_workers(self) -> None:
        return None
