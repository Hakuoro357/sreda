"""#159-rollout — режим «всем» (`*`) для ReAct-гейтов loop/prune.

Гейт `SREDA_REACT_LOOP_ENABLED_TENANTS` (и `SREDA_REACT_PRUNE_TENANTS`) был allowlist-only.
Для раскатки на ВСЕ аккаунты добавлен wildcard `*` → `tenant_id in gate` истинно для любого
тенанта (включая будущие регистрации). privacy-allowlist'ы (debug/admin) и osa — без `*`.
"""
from __future__ import annotations

import pytest

from sreda.config import settings as st_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    st_mod.get_settings.cache_clear()
    yield
    st_mod.get_settings.cache_clear()


# ───────────────────────── loop-гейт ─────────────────────────
def test_loop_wildcard_enables_any_tenant(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_loop_enabled_tenants
    assert "tenant_any_99999" in g  # будущая/незнакомая регистрация
    assert "tenant_max_40921122" in g
    assert bool(g) is True  # гейт активен (важно: пустой-но-«всем» не должен быть falsy)


def test_loop_wildcard_with_spaces(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "  *  ")
    st_mod.get_settings.cache_clear()
    assert "tenant_whoever" in st_mod.get_settings().react_loop_enabled_tenants


def test_loop_explicit_list_unchanged(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "tenant_a, tenant_b")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_loop_enabled_tenants
    assert "tenant_a" in g and "tenant_b" in g
    assert "tenant_c" not in g  # НЕ из списка → не включён (allowlist не сломан)
    assert bool(g) is True


def test_loop_empty_disables_everyone(monkeypatch):
    monkeypatch.delenv("SREDA_REACT_LOOP_ENABLED_TENANTS", raising=False)
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_loop_enabled_tenants
    assert "tenant_a" not in g
    assert bool(g) is False  # пусто → никому, гейт неактивен


def test_literal_comma_list_does_not_trigger_all(monkeypatch):
    """«a,b» НЕ должен включать всех — только `*` (одиночный) = режим «всем»."""
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "tenant_a,tenant_b")
    st_mod.get_settings.cache_clear()
    assert "tenant_z" not in st_mod.get_settings().react_loop_enabled_tenants


# ───────────────────────── prune-гейт (#165) ─────────────────────────
def test_prune_wildcard_enables_any_tenant(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_prune_tenants
    assert "tenant_any_99999" in g
    assert bool(g) is True


def test_prune_explicit_list_unchanged(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "tenant_a")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_prune_tenants
    assert "tenant_a" in g and "tenant_b" not in g


# ───────────── privacy-allowlist'ы и osa-эксперимент: БЕЗ `*` (строгие) ─────────────
def test_debug_gate_treats_star_literally_not_all(monkeypatch):
    """react_debug_tenants — privacy allowlist; `*` НЕ должен раскрывать всех на дебаг."""
    monkeypatch.setenv("SREDA_REACT_DEBUG_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_debug_tenants
    assert "tenant_random" not in g  # `*` тут — обычная строка-член, не режим «всем»


def test_osa_gate_treats_star_literally_not_all(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_OSA_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_osa_tenants
    assert "tenant_random" not in g


def test_admin_preview_gate_treats_star_literally(monkeypatch):
    """admin preview — privacy allowlist; `*` НЕ должен раскрыть всех (Codex R1 MINOR)."""
    monkeypatch.setenv("SREDA_ADMIN_ALERT_PREVIEW_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    assert "tenant_random" not in st_mod.get_settings().admin_alert_preview_tenants


# ───────────── контракт режима «всем» (Codex/mimo R1 MINOR) ─────────────
def test_wildcard_rejects_none_and_empty(monkeypatch):
    """Режим «всем»: None/«»/нестрока НЕ члены (малформный id не «включается»)."""
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_loop_enabled_tenants
    assert (None in g) is False
    assert ("" in g) is False
    assert ("tenant_x" in g) is True


def test_wildcard_contract_len_iter_bool(monkeypatch):
    """Контракт режима «всем»: membership-only API — len==0, итерация пуста, но bool=True.
    Закрепляем «странную» семантику, чтобы будущий код не принял «всем» за «никому»."""
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_loop_enabled_tenants
    assert len(g) == 0
    assert list(g) == []
    assert bool(g) is True


def test_mixed_star_in_list_is_not_all_mode(monkeypatch):
    """«tenant_a,*» — НЕ режим «всем» (только одиночный `*`); `*` тут буквальный член."""
    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "tenant_a,*")
    st_mod.get_settings.cache_clear()
    g = st_mod.get_settings().react_loop_enabled_tenants
    assert "tenant_a" in g
    assert "*" in g  # буквальный член списка
    assert "tenant_random" not in g  # НЕ режим «всем»
