"""#150: Mercury-2 в каталоге провайдеров /admin/llm."""
from __future__ import annotations

from sreda.admin.routes import _LLM_PROVIDERS_METADATA
from sreda.config.settings import Settings


def test_mercury2_present_in_catalogue() -> None:
    by_key = {e["key"]: e for e in _LLM_PROVIDERS_METADATA}
    assert "inception-mercury2" in by_key
    entry = by_key["inception-mercury2"]
    assert entry["static_model"] == "mercury-2"
    assert entry["resolver"] == "resolve_inception_api_key"


def test_all_resolvers_exist_on_settings() -> None:
    # каждый resolver обязан быть реальным атрибутом Settings — иначе /admin/llm
    # упадёт при рендере каталога (резолвер дёргается на странице).
    s = Settings()
    for entry in _LLM_PROVIDERS_METADATA:
        assert hasattr(s, entry["resolver"]), entry["resolver"]


def test_no_duplicate_provider_keys() -> None:
    keys = [e["key"] for e in _LLM_PROVIDERS_METADATA]
    assert len(keys) == len(set(keys)), "дубли ключей провайдеров"
