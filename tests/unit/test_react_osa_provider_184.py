"""#184: react_provider — per-tenant выбор модели ReAct («Оса» vs planner_provider).

Планировщик НЕ затронут — оверрайд только для ReAct-входа.
"""
from __future__ import annotations

from sreda.config import settings as st_mod
from sreda.runtime.react_loop import react_provider
from sreda.services import llm as llm_mod


class _Stub:
    def __init__(self, osa: set[str], planner: str) -> None:
        self.react_osa_tenants = frozenset(osa)
        self.planner_provider = planner


def test_react_provider_osa_tenant_gets_groq(monkeypatch):
    monkeypatch.setattr(st_mod, "get_settings",
                        lambda: _Stub({"tenant_osa"}, "inception-mercury2"))
    assert react_provider("tenant_osa") == "groq-gpt-oss-120b"


def test_react_provider_other_tenant_stays_planner(monkeypatch):
    monkeypatch.setattr(st_mod, "get_settings",
                        lambda: _Stub({"tenant_osa"}, "inception-mercury2"))
    # не-флагнутый тенант → planner_provider (Mercury), ноль изменений
    assert react_provider("tenant_other") == "inception-mercury2"


def test_react_provider_empty_flag_all_planner(monkeypatch):
    monkeypatch.setattr(st_mod, "get_settings",
                        lambda: _Stub(set(), "inception-mercury2"))
    assert react_provider("anyone") == "inception-mercury2"


def test_groq_osa_provider_registered_and_resolves():
    # Оса должна быть валидным провайдером и резолвиться в gpt-oss-120b
    assert "groq-gpt-oss-120b" in llm_mod.CHAT_PROVIDERS
    assert llm_mod._GROQ_MODEL_BY_PROVIDER["groq-gpt-oss-120b"] == "openai/gpt-oss-120b"
    assert llm_mod._GROQ_EXTRA_BODY_BY_PROVIDER["groq-gpt-oss-120b-low"] == {
        "reasoning_effort": "low"}


def test_react_osa_tenants_settings_parse(monkeypatch):
    # env-парсинг allowlist'а (по образцу react_loop_enabled_tenants)
    monkeypatch.setenv("SREDA_REACT_OSA_TENANTS", "tenant_a, tenant_b ,")
    s = st_mod.Settings()
    assert s.react_osa_tenants == frozenset({"tenant_a", "tenant_b"})
