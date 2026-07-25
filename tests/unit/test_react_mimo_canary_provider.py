"""Канарейка MiMo: react_provider — per-tenant переключение ReAct на mimo-v2.5-pro.

По образцу #184 («Оса»). Оверрайд ТОЛЬКО для ReAct-входа, планировщик не затронут.
Главный тест здесь — `..._other_tenant_stays_planner`: он защищает ВЕСЬ прод от регрессии.
"""
from __future__ import annotations

from sreda.config import settings as st_mod
from sreda.runtime.react_loop import react_provider
from sreda.services import llm as llm_mod

_MIMO = "mimo-v2.5-pro"
_OSA = "groq-gpt-oss-120b"
_PLANNER = "inception-mercury2"


class _Stub:
    def __init__(self, mimo: set[str], osa: set[str], planner: str = _PLANNER) -> None:
        self.react_mimo_tenants = frozenset(mimo)
        self.react_osa_tenants = frozenset(osa)
        self.planner_provider = planner


def test_mimo_tenant_gets_mimo(monkeypatch):
    # (а) тенант в списке канарейки → mimo-v2.5-pro
    monkeypatch.setattr(st_mod, "get_settings",
                        lambda: _Stub({"tenant_tg_352612382"}, set()))
    assert react_provider("tenant_tg_352612382") == _MIMO


def test_other_tenant_stays_planner(monkeypatch):
    # (б) ГЛАВНЫЙ: не-флагнутый тенант → planner_provider, ноль изменений для прода
    monkeypatch.setattr(st_mod, "get_settings",
                        lambda: _Stub({"tenant_tg_352612382"}, set()))
    assert react_provider("tenant_tg_999") == _PLANNER


def test_empty_flag_nobody_affected(monkeypatch):
    # (в) пустая настройка → поведение байт-в-байт прежнее (ПРАВИЛО #1)
    monkeypatch.setattr(st_mod, "get_settings", lambda: _Stub(set(), set()))
    assert react_provider("tenant_tg_352612382") == _PLANNER
    assert react_provider("anyone") == _PLANNER


def test_empty_mimo_flag_does_not_disturb_osa(monkeypatch):
    # (в') пустая канарейка не ломает существующий оверрайд «Осы»
    monkeypatch.setattr(st_mod, "get_settings", lambda: _Stub(set(), {"tenant_osa"}))
    assert react_provider("tenant_osa") == _OSA
    assert react_provider("tenant_other") == _PLANNER


def test_mimo_wins_over_osa_when_tenant_in_both(monkeypatch):
    # (г) задокументированный приоритет: канарейка MiMo проверяется ПЕРВОЙ и бьёт «Осу»
    monkeypatch.setattr(st_mod, "get_settings",
                        lambda: _Stub({"tenant_both"}, {"tenant_both", "tenant_osa"}))
    assert react_provider("tenant_both") == _MIMO
    # тенант только в списке «Осы» — прежнее поведение сохранено
    assert react_provider("tenant_osa") == _OSA


def test_mimo_provider_registered_and_resolves():
    # ключ провайдера существует в реестре и резолвится в ту же модель
    assert _MIMO in llm_mod.CHAT_PROVIDERS
    assert llm_mod._MIMO_MODEL_BY_PROVIDER[_MIMO] == _MIMO


def test_react_mimo_tenants_settings_parse(monkeypatch):
    # env-парсинг allowlist'а (по образцу react_osa_tenants)
    monkeypatch.setenv("SREDA_REACT_MIMO_TENANTS", "tenant_a, tenant_b ,")
    s = st_mod.Settings()
    assert s.react_mimo_tenants == frozenset({"tenant_a", "tenant_b"})


def test_react_mimo_tenants_default_empty(monkeypatch):
    monkeypatch.delenv("SREDA_REACT_MIMO_TENANTS", raising=False)
    monkeypatch.delenv("sreda_react_mimo_tenants", raising=False)
    assert st_mod.Settings().react_mimo_tenants == frozenset()
