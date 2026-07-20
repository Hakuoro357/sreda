"""Регрессионные тесты фиксов аудита 2026-07-18 — slug ``secrets``.

Покрывает находки deploy-ops-review в ``sreda/scripts/**``:

- [CRITICAL] dev/benchmark_neuraldeep.py — hardcoded API-ключ удалён,
  fail-fast без ``NEURALDEEP_API_KEY``.
- [MAJOR] monitor_health.py — admin chat id из env
  (``SREDA_ADMIN_TELEGRAM_CHAT_ID``), без него алерт пропускается, а не
  падает.
- [MAJOR] wipe_tenant.sh — SSH user@host только из ``SREDA_VDS_HOST``
  (fail-fast), адрес VDS не захардкожен.
- [MAJOR] backup_postgres.sh — конфигурируемая offsite-копия
  (``SREDA_BACKUP_OFFSITE_CMD``, безопасный дефолт + WARN) и отсутствие
  plaintext-артефактов на failure-путях.
- [MAJOR] PII (TG/MAX-id пользователей и владельца) вынесена из кода
  one-shot/dev/replay скриптов в env/argv.

Без сети и без PostgreSQL. Bash-динамика — со skip-guard'ом.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

# PII-литералы собраны из фрагментов, чтобы сам тест не содержал их целиком
# (полные id остаются в легаси-тестах — их зачищает tests-воркер).
_FORBIDDEN_LITERALS = [
    "352" + "612382",
    "1089" + "832184",
    "755" + "682022",
    "634" + "496616",
    "409" + "21122",
    "62.113.41." + "104",
    "boris" + "@",
]


def _load_module(name: str, path: Path):
    """Загрузить скрипт по пути (имена с цифр/дефисов не import-ируются)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


# ---------------------------------------------------------------------------
# CRITICAL: benchmark_neuraldeep.py — ключ только из env, fail-fast
# ---------------------------------------------------------------------------


def test_benchmark_neuraldeep_no_hardcoded_fallback() -> None:
    text = (SCRIPTS / "dev" / "benchmark_neuraldeep.py").read_text(encoding="utf-8")
    assert 'or "sk-' not in text
    assert "NEURALDEEP_API_KEY" in text
    assert "SystemExit" in text


def test_benchmark_neuraldeep_fail_fast_without_key(monkeypatch) -> None:
    monkeypatch.delenv("NEURALDEEP_API_KEY", raising=False)
    try:
        _load_module(
            "audit_fix_benchmark_neuraldeep",
            SCRIPTS / "dev" / "benchmark_neuraldeep.py",
        )
    except SystemExit as exc:
        assert "NEURALDEEP_API_KEY" in str(exc.code)
        return
    except ImportError as exc:  # dev-зависимости (langchain_openai) не установлены
        pytest.skip(f"dev deps unavailable: {exc}")
    pytest.fail("benchmark_neuraldeep imported without NEURALDEEP_API_KEY — fail-fast missing")


# ---------------------------------------------------------------------------
# MAJOR: monitor_health.py — ADMIN_CHAT_ID из env, skip без него
# ---------------------------------------------------------------------------


def test_monitor_health_admin_chat_id_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "123456")
    mod = _load_module("audit_fix_monitor_health_env", SCRIPTS / "monitor_health.py")
    assert mod.ADMIN_CHAT_ID == "123456"
    assert "352" + "612382" not in (SCRIPTS / "monitor_health.py").read_text(encoding="utf-8")


def test_monitor_health_admin_chat_id_absent_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", raising=False)
    mod = _load_module("audit_fix_monitor_health_noenv", SCRIPTS / "monitor_health.py")
    # /etc/sreda/.env на dev-машине отсутствует → пусто, но НЕ падение.
    assert mod.ADMIN_CHAT_ID == ""


def test_monitor_health_alert_skips_without_chat_id(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", raising=False)
    mod = _load_module("audit_fix_monitor_health_skip", SCRIPTS / "monitor_health.py")
    monkeypatch.setattr(mod, "_ENV", {"SREDA_TELEGRAM_BOT_TOKEN": "dummy"})
    monkeypatch.setattr(mod, "ADMIN_CHAT_ID", "")
    mod.send_telegram_alert("test")  # НЕ должно уйти в сеть
    err = capsys.readouterr().err
    assert "SREDA_ADMIN_TELEGRAM_CHAT_ID" in err


# ---------------------------------------------------------------------------
# MAJOR: wipe_tenant.sh — SSH host из env, fail-fast
# ---------------------------------------------------------------------------


def test_wipe_tenant_requires_env_host() -> None:
    text = (SCRIPTS / "wipe_tenant.sh").read_text(encoding="utf-8")
    assert "62.113.41." not in text
    assert "boris" + "@" not in text
    assert "SREDA_VDS_HOST is not set" in text


def _msys_bash() -> str | None:
    """git-bash (MSYS) для shell-динамики; WSL bash не понимает /c/... — skip."""
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        probe = subprocess.run(
            [bash, "-c", "uname -s"], capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if "MINGW" in probe.stdout or "MSYS" in probe.stdout:
        return bash
    return None


@pytest.mark.skipif(_msys_bash() is None, reason="MSYS bash недоступен")
def test_wipe_tenant_fails_fast_without_env() -> None:
    env = {k: v for k, v in os.environ.items() if k != "SREDA_VDS_HOST"}
    script = (SCRIPTS / "wipe_tenant.sh").as_posix()  # C:/pro/...
    if len(script) > 2 and script[1] == ":":
        script = "/" + script[0].lower() + script[2:]  # /c/pro/... для MSYS
    proc = subprocess.run(
        [_msys_bash(), script, "tenant_tg_123"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 1
    assert "SREDA_VDS_HOST" in proc.stderr


# ---------------------------------------------------------------------------
# MAJOR: backup_postgres.sh — offsite-копия + гигиена plaintext на failure
# ---------------------------------------------------------------------------


def test_backup_postgres_offsite_configurable_with_safe_default() -> None:
    text = (SCRIPTS / "backup_postgres.sh").read_text(encoding="utf-8")
    assert "SREDA_BACKUP_OFFSITE_CMD" in text
    # Безопасный дефолт: без переменной — WARN, а не молчание/падение
    assert "no offsite copy configured" in text
    # Провал offsite не валит локальный бэкап
    assert "offsite copy FAILED (local encrypted backup intact)" in text


def test_backup_postgres_no_plaintext_leftovers_on_failure() -> None:
    text = (SCRIPTS / "backup_postgres.sh").read_text(encoding="utf-8")
    # INTEGRITY FAIL больше не оставляет .CORRUPT plaintext навсегда
    assert "CORRUPT" not in text
    # openssl-failure чистится trap'ом
    assert "trap cleanup_on_error ERR" in text
    # retention чистит и plaintext-остатки failed-прогонов
    assert "sreda-*.dump.gz" in text


# ---------------------------------------------------------------------------
# MAJOR: 204_cancel_legacy_voice_subs.py — tenant-set из env
# ---------------------------------------------------------------------------


def test_204_expected_tenants_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "SREDA_204_EXPECTED_TENANTS", " tenant_tg_111 , tenant_tg_222 "
    )
    mod = _load_module(
        "audit_fix_204_env", SCRIPTS / "204_cancel_legacy_voice_subs.py"
    )
    assert mod.EXPECTED_TENANT_IDS == frozenset({"tenant_tg_111", "tenant_tg_222"})


def test_204_main_fail_fast_without_env(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SREDA_204_EXPECTED_TENANTS", raising=False)
    mod = _load_module(
        "audit_fix_204_noenv", SCRIPTS / "204_cancel_legacy_voice_subs.py"
    )
    assert mod.EXPECTED_TENANT_IDS == frozenset()
    rc = mod.main([])
    assert rc == 2
    assert "SREDA_204_EXPECTED_TENANTS" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# MAJOR: one-shot cleanup — tenant из --tenant/env, fail-fast
# ---------------------------------------------------------------------------


def test_oneshot_cleanup_requires_tenant(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SREDA_ONESHOT_TARGET_TENANT", raising=False)
    monkeypatch.setattr(sys, "argv", ["_ONESHOT_cleanup_dup_tasks_2026_05_15.py"])
    mod = _load_module(
        "audit_fix_oneshot", SCRIPTS / "_ONESHOT_cleanup_dup_tasks_2026_05_15.py"
    )
    assert mod.TARGET_TENANT == ""
    rc = mod.main()
    assert rc == 1
    assert "--tenant" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# MAJOR: replay one-shot'ы — tenant из env, fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["replay/diag_replies.py", "replay/export_archive.py"])
def test_replay_scripts_fail_fast_without_tenant(monkeypatch, rel: str) -> None:
    monkeypatch.delenv("SREDA_REPLAY_TENANT", raising=False)
    name = "audit_fix_" + rel.replace("/", "_").replace(".py", "")
    with pytest.raises(SystemExit):
        _load_module(name, SCRIPTS / rel)


# ---------------------------------------------------------------------------
# MAJOR: debug_checklists_dump — tenant через argv, fail-fast
# ---------------------------------------------------------------------------


def test_debug_checklists_dump_requires_argv(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["debug_checklists_dump.py"])
    mod = _load_module("audit_fix_dbg_lists", SCRIPTS / "debug_checklists_dump.py")
    with pytest.raises(SystemExit):
        mod.main()


# ---------------------------------------------------------------------------
# MINOR #13: monitor_health — HTML-escape, since при critical→warning, DSN
# ---------------------------------------------------------------------------


def test_monitor_health_escapes_html_in_message(monkeypatch) -> None:
    monkeypatch.delenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", raising=False)
    mod = _load_module("audit_fix_mh_html", SCRIPTS / "monitor_health.py")
    res = mod.ProbeResult("pg", "critical", "psql failed: FATAL: <password> & <x>")
    msg = mod.format_alert("pg", "ok", res, "host", None)
    assert "<password>" not in msg
    assert "&lt;password&gt;" in msg and "&amp;" in msg


def test_monitor_health_since_kept_on_critical_to_warning(monkeypatch) -> None:
    """critical→warning (оба не-ok) не должен сбрасывать ProbeState.since."""
    text = (SCRIPTS / "monitor_health.py").read_text(encoding="utf-8")
    assert 'prev_status != "ok" and result.status != "ok"' in text
    assert "since_iso = prev.since" in text


def test_monitor_health_dsn_password_with_special_chars(monkeypatch) -> None:
    monkeypatch.delenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", raising=False)
    mod = _load_module("audit_fix_mh_dsn", SCRIPTS / "monitor_health.py")
    dsn = "postgresql+psycopg://sreda:p%40ss:w%2Frd@localhost:5432/sreda"
    assert mod._pg_password_from_dsn(dsn) == "p@ss:w/rd"
    assert mod._pg_password_from_dsn("not-a-dsn") is None


# ---------------------------------------------------------------------------
# MINOR #6: wipe_tenant.sh — no-FK таблицы с переписками в списке DELETE
# ---------------------------------------------------------------------------


def test_wipe_tenant_covers_no_fk_trace_tables() -> None:
    text = (SCRIPTS / "wipe_tenant.sh").read_text(encoding="utf-8")
    for table in (
        "react_turn_trace",
        "react_checkpoint",
        "conversation_turns",
        "poller_heartbeats",
    ):
        assert f"DELETE FROM {table}" in text


# ---------------------------------------------------------------------------
# Сводный страж: PII-литералы не возвращаются в sreda/scripts/**
# ---------------------------------------------------------------------------


def test_no_pii_literals_in_scripts() -> None:
    offenders: list[str] = []
    for path in sorted(SCRIPTS.rglob("*")):
        if path.suffix not in (".py", ".sh", ".ps1") or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lit in _FORBIDDEN_LITERALS:
            if lit in text:
                offenders.append(f"{path.relative_to(ROOT)}: {lit[:4]}…")
    assert offenders == []


# R1 C9: прод entity-id (task_/checklist_/reminder_/rem_ + 24 hex) — это
# конкретные строки БД конкретного пользователя. В one-shot скриптах публичного
# репо их хардкодить нельзя (id + пользовательский контент рядом) — только из
# argv/env. Страж ловит регрессию класса, который закрыл C9.
_PROD_ENTITY_ID_RE = re.compile(r"\b(?:task|checklist|reminder|rem)_([0-9a-f]{24})\b")


def test_no_prod_entity_ids_in_scripts() -> None:
    offenders: list[str] = []
    for path in sorted(SCRIPTS.rglob("*")):
        if path.suffix not in (".py", ".sh", ".ps1") or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for hex_part in _PROD_ENTITY_ID_RE.findall(text):
            # Синтетические mock/replay-id (низкая энтропия: `aaaa…`, `0000…`,
            # `1234…`) — не прод; реальный uuid4.hex[:24] имеет ≥8 разных hex.
            if len(set(hex_part)) < 8:
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {hex_part[:10]}…")
    assert offenders == [], f"хардкод прод entity-id в скриптах: {offenders}"
