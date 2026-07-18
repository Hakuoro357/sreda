# -*- coding: utf-8 -*-
"""Регрессионные тесты фиксов аудита 2026-07-18 (slug tests-ci).

Покрывает ключевые точки, которые НЕ защищены существующими тестами:
  1. Модуль test_llm_provider_dispatch больше НЕ скипнут на уровне
     модуля (21 тест dispatch/fallback покрытия вернулся в свит) и
     несёт собственный offline-гарда (DB-шов _factory_for + socket/httpx).
  2. Network-ban гард functional-сьюита поднят на уровень httpx
     network-транспортов (ConnectEx-обход ProactorEventLoop на Windows)
     и selector-политика ставится при импорте, а не задепрекеченной
     фикстурой event_loop_policy.
  3. CI-контракты: functional-job подключён; PG-job выставляет единые
     SREDA_TEST_POSTGRES_URL + DESTRUCTIVE_OPT_IN и включает test_344;
     существует gitleaks secret-scan workflow.

Source-уровневые ассерты (шов-тесты) — в стиле tests/functional/test_seams.py.
Все тесты офлайн, без PG, без сети.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DISPATCH = _REPO / "tests" / "unit" / "test_llm_provider_dispatch.py"
_FUNC_CONFTEST = _REPO / "tests" / "functional" / "conftest.py"
_CI_TESTS_YML = _REPO / ".github" / "workflows" / "ci-tests.yml"
_SECRET_SCAN_YML = _REPO / ".github" / "workflows" / "secret-scan.yml"


def _load_dispatch_module():
    spec = importlib.util.spec_from_file_location(
        "audit_fix_dispatch_module", _DISPATCH,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- 1. dispatch-модуль расскиплен и offline-гардирован ---------------------

def test_dispatch_module_has_no_module_level_skip():
    mod = _load_dispatch_module()
    pytestmark = getattr(mod, "pytestmark", [])
    marks = pytestmark if isinstance(pytestmark, list) else [pytestmark]
    skip_marks = [m for m in marks if getattr(m, "name", "") == "skip"]
    assert not skip_marks, (
        "module-level skip вернулся — 21 тест dispatch/fallback снова "
        "выпал из свита (аудит 2026-07-18, MAJOR #3)"
    )
    assert not hasattr(mod, "_FIXTURE_BROKEN"), (
        "per-test skip-маркер _FIXTURE_BROKEN вернулся — фикстура должна "
        "быть починена (патч MimoChatOpenAI в модуле-источнике), не скипнута"
    )


def test_dispatch_module_carries_offline_guards():
    mod = _load_dispatch_module()
    for fixture_name in ("_patch_chat_openai", "_offline_provider_db",
                         "_no_network_guard"):
        assert callable(getattr(mod, fixture_name, None)), (
            f"в test_llm_provider_dispatch нет фикстуры {fixture_name} — "
            f"без неё модуль возвращается к реальным network calls / виснет"
        )


def test_dispatch_module_counts_21_tests():
    mod = _load_dispatch_module()
    tests = [n for n in dir(mod) if n.startswith("test_")]
    assert len(tests) == 21, (
        f"ожидался 21 dispatch-тест, найдено {len(tests)} — модуль "
        f"повреждён или часть покрытия удалена"
    )


# --- 2. functional network-ban: транспортный слой + политика при импорте ----

def test_functional_conftest_patches_httpx_transports():
    src = _FUNC_CONFTEST.read_text(encoding="utf-8")
    assert "httpx.AsyncHTTPTransport" in src, (
        "гарда на уровне AsyncHTTPTransport нет — ProactorEventLoop на "
        "Windows соединяется через ConnectEx МИМО socket.connect, и внешний "
        "HTTP из теста молча проходит (аудит 2026-07-18, MINOR #4)"
    )
    assert "httpx.HTTPTransport" in src, (
        "гарда на уровне sync HTTPTransport нет — sync-обход остаётся открытым"
    )


def test_functional_conftest_policy_set_at_import_not_fixture():
    src = _FUNC_CONFTEST.read_text(encoding="utf-8")
    assert "asyncio.set_event_loop_policy(" in src, (
        "selector-политика должна ставиться при ИМПОРТЕ conftest — "
        "session-фикстура event_loop_policy на pytest-asyncio 1.x "
        "задепрекечена и не применяется"
    )
    assert "def event_loop_policy" not in src, (
        "задепрекеченная фикстура event_loop_policy вернулась — "
        "pytest-asyncio 1.x её не применяет, гард деградирует молча"
    )


def test_functional_harness_skips_without_private_plugin():
    src = _FUNC_CONFTEST.read_text(encoding="utf-8")
    assert "importorskip" in src and "sreda_feature_housewife_assistant" in src, (
        "harness должен честно скипаться без приватного feature-плагина — "
        "иначе functional-job в публичном CI падает ImportError'ом"
    )


# --- 3. CI-контракты ---------------------------------------------------------

def test_ci_runs_functional_suite():
    yml = _CI_TESTS_YML.read_text(encoding="utf-8")
    assert "tests/functional" in yml, (
        "functional-сьюит снова не подключён к CI (аудит 2026-07-18, MAJOR #1)"
    )


def test_ci_pg_job_unifies_destructive_gates():
    yml = _CI_TESTS_YML.read_text(encoding="utf-8")
    for needle in ("SREDA_TEST_POSTGRES_URL",
                   "SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN",
                   "test_344_delivery_contour_pg.py",
                   "sreda_test"):
        assert needle in yml, (
            f"в pg-job нет «{needle}» — PG-конкурентные money-path сьюиты "
            f"и delivery-контур #344 снова недостижимы в CI "
            f"(аудит 2026-07-18, MAJOR #2)"
        )


def test_secret_scan_workflow_exists():
    assert _SECRET_SCAN_YML.exists(), (
        "нет .github/workflows/secret-scan.yml — gitleaks не подключён "
        "(аудит 2026-07-18, cross-cutting)"
    )
    yml = _SECRET_SCAN_YML.read_text(encoding="utf-8")
    assert "gitleaks" in yml
