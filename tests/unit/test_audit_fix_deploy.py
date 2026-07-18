"""Регрессионные тесты фиксов аудита 2026-07-18 — отчёт deploy-ops, slug=deploy.

Покрывает:
- находку 5  (MINOR): uv.lock готов к коммиту и синхронен с pyproject
  (``httpx[socks]`` → socksio присутствует; ``uv lock --check`` зелёный);
- находку 3  (MAJOR): ``sreda_feature_eds_monitor`` — importable-but-empty stub,
  loader платформы скипает его через retired-prefilter (#181);
- находку 3  (deploy): ``deploy_private_features.sh`` проверяет импорт
  feature-модулей ДО safe_restart (fail до рестарта);
- находку 12 (MINOR): ``deploy/nginx/sredaspace.conf`` документирует установку
  snippet'а и rate-limit zone, на которые ссылается конфиг.

Тесты eds_monitor/deploy-скрипта работают с соседним worktree
``sreda-private-features-audit-wt`` — если его нет, скипаются (skip-guard).
Без сети, без PG.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PF_ROOT = REPO_ROOT.parent / "sreda-private-features-audit-wt"
PF_SRC = PF_ROOT / "src"
PF_PLUGIN = "sreda_feature_eds_monitor.plugin"

pf_available = pytest.mark.skipif(
    not (PF_SRC / "sreda_feature_eds_monitor" / "plugin.py").is_file(),
    reason="worktree sreda-private-features-audit-wt недоступен",
)


def _import_pf_plugin(monkeypatch: pytest.MonkeyPatch):
    """Импортирует stub плагина из приватного worktree в чистом sys.modules."""
    for name in [n for n in sys.modules if n.startswith("sreda_feature_eds_monitor")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(PF_SRC))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import sreda_feature_eds_monitor.plugin as plugin  # noqa: PLC0415
    return plugin


# ---------------------------------------------------------------------------
# Находка 5 (MINOR): uv.lock синхронен с pyproject (httpx[socks] -> socksio)
# ---------------------------------------------------------------------------


def test_uv_lock_exists_and_contains_socksio():
    lock = REPO_ROOT / "uv.lock"
    assert lock.is_file(), "uv.lock должен лежать в корне репо (готов к коммиту)"
    text = lock.read_text(encoding="utf-8")
    assert 'name = "socksio"' in text, "в uv.lock нет socksio (httpx[socks], #244)"


def test_pyproject_declares_httpx_socks():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "httpx[socks]" in pyproject


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv не установлен")
def test_uv_lock_check_passes():
    proc = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"uv lock --check: {proc.stdout}{proc.stderr}"


# ---------------------------------------------------------------------------
# Находка 3 (MAJOR): eds_monitor — importable-but-empty stub с deprecation
# ---------------------------------------------------------------------------


@pf_available
def test_eds_monitor_stub_imports_cleanly(monkeypatch):
    plugin = _import_pf_plugin(monkeypatch)
    # Импорт не тянет мёртвую реализацию (routes/service/delivery/...).
    leaked = [
        name
        for name in sys.modules
        if name.startswith("sreda_feature_eds_monitor.")
        and name.rsplit(".", 1)[-1] not in {"plugin", "__init__"}
    ]
    assert not leaked, f"stub не должен импортировать подмодули: {leaked}"
    assert plugin.feature_module.feature_key == "eds_monitor"


@pf_available
def test_eds_monitor_stub_warns_deprecation(monkeypatch):
    for name in [n for n in sys.modules if n.startswith("sreda_feature_eds_monitor")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(PF_SRC))
    with pytest.warns(DeprecationWarning, match="ретайрен"):
        import sreda_feature_eds_monitor.plugin  # noqa: F401, PLC0415


@pf_available
def test_eds_monitor_stub_register_is_noop(monkeypatch):
    plugin = _import_pf_plugin(monkeypatch)
    plugin.register(object())  # не падает и ничего не регистрирует
    plugin.feature_module.register_api(object())
    plugin.feature_module.register_runtime()
    plugin.feature_module.register_workers()


@pf_available
def test_platform_loader_skips_eds_monitor_stub(monkeypatch):
    """Текущий loader (без гарда config-integ) обязан скипать stub через
    retired-prefilter: feature_module.feature_key == 'eds_monitor' (#181)."""
    _import_pf_plugin(monkeypatch)
    from sreda.features.loader import load_feature_modules  # noqa: PLC0415
    from sreda.features.registry import FeatureRegistry  # noqa: PLC0415

    registry = FeatureRegistry()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        load_feature_modules([PF_PLUGIN], registry)
    assert not getattr(registry, "_modules", {}), "ретайренный stub не должен регистрироваться"


@pf_available
def test_eds_monitor_dead_implementation_removed():
    """Мёртвая реализация (находки 3/8/11/14/15) удалена из пакета и тестов."""
    pkg = PF_SRC / "sreda_feature_eds_monitor"
    for dead in (
        "routes.py",
        "service.py",
        "client.py",
        "auth.py",
        "delivery.py",
        "repository.py",
        "cron_poll.py",
        "manual_login.py",
        "manual_poll.py",
        "manual_deliver_outbox.py",
        "proactive.py",
        "graphs.py",
        "jobs.py",
        "schemas.py",
        "summary.py",
        "prompts.py",
    ):
        assert not (pkg / dead).exists(), f"{dead} должен быть удалён вместе с ретайром"
    assert not (pkg / "integrations").exists()
    assert not (pkg / "scripts").exists(), "refresh_eds_session.ps1 (находка 11) удалён"
    assert not (PF_ROOT / "tools" / "refresh_eds_session.ps1").exists()
    for dead_test in (
        "test_auth.py",
        "test_client.py",
        "test_delivery.py",
        "test_polling.py",
        "test_repository.py",
        "test_summary.py",
    ):
        assert not (PF_ROOT / "tests" / dead_test).exists()


# ---------------------------------------------------------------------------
# Находка 3 (deploy): pre-restart проверка импорта feature-модулей
# ---------------------------------------------------------------------------


@pf_available
def test_deploy_script_checks_feature_imports_before_restart():
    script = (PF_ROOT / "scripts" / "deploy" / "deploy_private_features.sh").read_text(
        encoding="utf-8"
    )
    marker = 'log "pre-restart проверка импорта:'
    assert marker in script, "нет pre-restart проверки импорта feature-модулей"
    check_at = script.index(marker)
    restart_log = script.index('log "safe_restart…"')
    assert check_at < restart_log, "проверка импорта должна идти ДО safe_restart"
    # Между проверкой и рестартом — явный выход с ошибкой (fail до рестарта).
    assert "exit 4" in script[check_at:restart_log]
    # Проверка идёт после fast-forward merge (валидирует НОВЫЙ код).
    merge_at = script.index('"${RUN_SREDA[@]}" git -C "$PF" merge --ff-only origin/main')
    assert merge_at < check_at
    # Импорт гоняется prod-venv python'ом от пользователя sreda, не root'ом.
    assert '"${RUN_SREDA[@]}" "$VENV_PY"' in script


def _git_bash() -> str | None:
    """Git Bash (msys) — НЕ WSL bash: WSL искажает Windows-пути и виснет на stdin."""
    candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


@pytest.mark.skipif(_git_bash() is None, reason="Git Bash недоступен")
@pf_available
def test_deploy_script_bash_syntax_ok():
    script_path = PF_ROOT / "scripts" / "deploy" / "deploy_private_features.sh"
    proc = subprocess.run(
        [_git_bash(), "-n", script_path.as_posix()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Находка 12 (MINOR): sredaspace.conf — deploy-инструкция ставит snippet и zone
# ---------------------------------------------------------------------------


def test_sredaspace_conf_documents_snippet_and_limit_zone():
    conf = (REPO_ROOT / "deploy" / "nginx" / "sredaspace.conf").read_text(encoding="utf-8")
    # Конфиг по-прежнему ссылается на snippet и zone...
    assert "include snippets/sreda-block-scanners.conf;" in conf
    assert "limit_req zone=sreda_general" in conf
    # ...и теперь инструкция объясняет, откуда они берутся (nginx -t не упадёт).
    header = conf[: conf.index("server {")]
    assert "sreda-block-scanners.conf /etc/nginx/snippets/" in header
    assert "limit_req_zone" in header and "zone=sreda_general" in header
    # Zone должна объявляться ровно один раз — предупреждение о дедупликации.
    assert "РОВНО ОДИН РАЗ" in header


def test_block_scanners_snippet_tracked_alongside_conf():
    assert (REPO_ROOT / "deploy" / "nginx" / "sreda-block-scanners.conf").is_file()
