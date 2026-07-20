"""Ре-экспорт фикстур из ``tests/unit/conftest.py`` в область харнесса #396.

``tests/regression_396`` — сосед ``tests/unit``, не потомок, поэтому autouse-фикстуры
unit (memory-checkpointer, тестовый ключ шифрования, очистка loop-bound state,
no-op админ-алертов) и ``db_session`` сюда сами не приходят. Импорт втягивает их в
scope этого conftest (autouse сохраняется). ``seed_telegram_user`` — обычная фабрика,
её тесты импортируют напрямую.
"""
from __future__ import annotations

import pytest

from tests.unit.conftest import (  # noqa: F401 — ре-экспорт фикстур в scope харнесса
    _clear_module_level_loop_bound_state,
    _default_encryption_key,
    _disable_admin_alerts_for_unit_tests,
    _force_memory_langgraph_checkpointer,
    _test_engine,
    db_session,
)


@pytest.fixture(autouse=True)
def _prod_parity_react_env(monkeypatch):
    """Прод-паритет ReAct-тракта для фикстур: ЕДИНЫЙ путь (#285) ВКЛ на всех тенантов,
    доменный роутер (#221) в режиме execute — как на проде. Preflight ВЫКЛ: харнесс
    детерминирован и ОФЛАЙН (chat/fact идёт на Фредди-стаб, без сети/ключей). Весь env
    выставлен ДО тела теста; в конце setup чистим кеш настроек, чтобы применился."""
    monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1")
    monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", "*")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "*")
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "0")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
