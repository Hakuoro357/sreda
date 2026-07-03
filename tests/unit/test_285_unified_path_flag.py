"""#285 Фаза 0: пин-тесты флага единого пути ReAct.

Чеклист приёмки #285, пункт 11 («Флаги»): отдельный react_unified_path_enabled;
react_preflight_enabled НЕ переиспользован (остаётся kill-switch'ем сплита).
Поведение пока НЕ меняется — флаг спящий (Фаза A подключит TurnPolicy-shadow).
"""

from __future__ import annotations


def _fresh_settings(monkeypatch, **env: str):
    from sreda.config import settings as sm

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sm.get_settings.cache_clear()
    return sm.get_settings()


def test_unified_path_default_off(monkeypatch):
    """Дефолт OFF: без env-переменной флаг выключен (byte-identical, спящая раскатка)."""
    monkeypatch.delenv("SREDA_REACT_UNIFIED_PATH_ENABLED", raising=False)
    s = _fresh_settings(monkeypatch)
    assert s.react_unified_path_enabled is False


def test_unified_path_env_alias(monkeypatch):
    """Флаг читается из SREDA_REACT_UNIFIED_PATH_ENABLED."""
    s = _fresh_settings(monkeypatch, SREDA_REACT_UNIFIED_PATH_ENABLED="1")
    assert s.react_unified_path_enabled is True


def test_unified_tenants_gate(monkeypatch):
    """Companion-список (R1 CodexM m4): пусто → никому; тенант → только он; ``*`` → все."""
    s = _fresh_settings(monkeypatch, SREDA_REACT_UNIFIED_PATH_ENABLED="1")
    monkeypatch.delenv("SREDA_REACT_UNIFIED_TENANTS", raising=False)
    from sreda.config import settings as sm
    sm.get_settings.cache_clear()
    assert "tenant_x" not in sm.get_settings().react_unified_tenants
    s = _fresh_settings(monkeypatch, SREDA_REACT_UNIFIED_TENANTS="tenant_x")
    assert "tenant_x" in s.react_unified_tenants and "tenant_y" not in s.react_unified_tenants
    s = _fresh_settings(monkeypatch, SREDA_REACT_UNIFIED_TENANTS="*")
    assert "anyone" in s.react_unified_tenants


def test_unified_path_independent_of_preflight(monkeypatch):
    """НЕ переиспользуем react_preflight_enabled (урок settings R4: обе семантики заняты).

    Все четыре комбинации флагов независимы — включение одного не тянет другой.
    """
    for pre, uni in [("0", "0"), ("0", "1"), ("1", "0"), ("1", "1")]:
        s = _fresh_settings(
            monkeypatch,
            SREDA_REACT_PREFLIGHT_ENABLED=pre,
            SREDA_REACT_UNIFIED_PATH_ENABLED=uni,
        )
        assert s.react_preflight_enabled is (pre == "1")
        assert s.react_unified_path_enabled is (uni == "1")
