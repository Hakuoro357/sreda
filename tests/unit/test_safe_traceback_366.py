"""#366: PII-safe стек-трейс для логов прод-крашей ReAct.

Прод 2026-07-13: ход падал под ролью sreda_app (RLS), но внешняя страховка
логировала ТОЛЬКО тип ошибки («type=AttributeError»), БЕЗ стека - потому что
str(exc) у SQLAlchemy несёт SQL с параметрами (PII, урок g-039). Итог: слепота
«где упало» → диагностика заняла часы вместо секунд.

Фикс: safe_traceback(exc) логирует ЦЕПОЧКУ КАДРОВ (file:line:func) + типы причин,
но НЕ str(exc)/значения - стек-кадры PII не содержат, место падения видно.
Компоненты кадра/типа - через строгий ASCII-allowlist (R2: dynamic-compiled код
мог бы нести не-ASCII/ПД в co_filename/co_name/имени класса).
"""
from __future__ import annotations

import logging
import re

from sreda.config.log_redaction import safe_traceback, safe_type_name

_CYR_SK = "СК"          # «СК» кириллицей - как ПД в co_filename
_CYR_CLASS = "Класс"  # «Класс» кириллицей


def _raise_chain():
    """AttributeError, вызванный ValueError с PII в тексте - как на проде."""
    try:
        try:
            raise ValueError(
                "INSERT INTO react_checkpoint ... "
                "parameters: {'text': 'my movie list', 'secret': 'hunter2'}")
        except ValueError as inner:
            raise AttributeError("'NoneType' object has no attribute 'plan_key'") from inner
    except AttributeError as exc:
        return exc


def test_safe_traceback_has_frames_366():
    """Кадры стека (file:line:func) - место падения видно."""
    tb = safe_traceback(_raise_chain())
    assert "test_safe_traceback_366.py:" in tb
    assert "_raise_chain" in tb


def test_safe_traceback_no_pii_366():
    """PII из str(exc) НЕ течёт: ни текста сообщения, ни SQL-параметров, ни секрета."""
    tb = safe_traceback(_raise_chain())
    assert "my movie list" not in tb
    assert "hunter2" not in tb
    assert "parameters:" not in tb
    assert "INSERT INTO" not in tb


def test_safe_traceback_cause_types_366():
    """Цепочка причин - ТОЛЬКО имена типов причин (верхний тип логируется отдельным
    `type=%s` в вызывающем; safe_traceback даёт стек + causes)."""
    tb = safe_traceback(_raise_chain())
    assert "caused-by=ValueError" in tb
    assert "has no attribute" not in tb
    assert "plan_key" not in tb


def test_safe_traceback_bounded_366():
    """Ограничение глубины: дефолт ≤12 кадров = ≤11 разделителей."""
    def _deep(n):
        if n <= 0:
            raise RuntimeError("boom")
        _deep(n - 1)
    try:
        _deep(50)
    except RuntimeError as exc:
        tb = safe_traceback(exc, limit=12)
    assert tb.count(" <- ") <= 11


def test_safe_traceback_clamp_limit_366():
    """R1 sol/terra MINOR: limit=0/отрицательный НЕ даёт все кадры (frames[-0:])."""
    def _deep(n):
        if n <= 0:
            raise RuntimeError("boom")
        _deep(n - 1)
    for bad in (0, -5):
        try:
            _deep(30)
        except RuntimeError as exc:
            tb = safe_traceback(exc, limit=bad)
        assert tb.count(" <- ") <= 0  # clamp к 1 кадру


def test_safe_traceback_failsafe_exotic_366():
    """R1 все трое: экзотический exc (свойство __cause__ бросает) НЕ роняет
    лог-путь - возврат маркера, не проброс (иначе маскирует исходную ошибку)."""
    class _Evil(Exception):
        @property
        def __cause__(self):
            raise ValueError("boom-in-cause")
    try:
        raise _Evil("x")
    except _Evil as exc:
        out = safe_traceback(exc)
    assert out == "<traceback-unavailable>"


def test_safe_traceback_structure_allowlist_366():
    """R1 субагент MINOR: вывод структурно ограничен (allowlist) - не только
    denylist конкретных ПД-строк."""
    tb = safe_traceback(_raise_chain())
    assert re.fullmatch(
        r"[\w.]+:\d+:[\w<>]+( <- [\w.]+:\d+:[\w<>]+)*"
        r"( caused-by=\w+(>\w+)*)?", tb), tb


def test_safe_traceback_hostile_metadata_placeholdered_366():
    """R2 sol/terra MAJOR: НЕ-ASCII/ПД/разделители в co_filename → строгий
    allowlist заменяет на «?», не течёт в лог."""
    src = "def bad():\n raise RuntimeError('x')\n"
    ns: dict = {}
    fname = "<qwe\n" + _CYR_SK + "\x01>"   # co_filename с ПД + перенос + ctrl
    exec(compile(src, fname, "exec"), ns)  # noqa: S102
    try:
        ns["bad"]()
    except RuntimeError as exc:
        tb = safe_traceback(exc)
    assert "\n" not in tb and "\x01" not in tb
    assert _CYR_SK not in tb   # кириллица-ПД срезана allowlist'ом
    assert "?" in tb           # плейсхолдеры на месте вырезанного


def test_safe_traceback_ascii_text_pii_placeholdered_366():
    """R3 sol/terra MAJOR: произвольный ASCII-ТЕКСТ (пробелы/SQL - класс ПД g-039)
    в co_filename режется ЦЕЛИКОМ (structural allowlist), не частично."""
    src = "def bad():\n raise RuntimeError('x')\n"
    ns: dict = {}
    exec(compile(src, "SELECT text FROM t WHERE msg='my movie list'", "exec"), ns)  # noqa: S102
    try:
        ns["bad"]()
    except RuntimeError as exc:
        tb = safe_traceback(exc)
    assert "my movie list" not in tb
    assert "SELECT" not in tb and "WHERE" not in tb
    assert "?:" in tb  # компонент co_filename заменён целиком


def test_safe_traceback_truncation_marker_366():
    """R3 sol/terra MINOR: обход прерван потолком → явный маркер, лог не выглядит
    полным без точки сбоя."""
    def _deep(n):
        if n <= 0:
            raise RuntimeError("boom")
        _deep(n - 1)
    try:
        _deep(700)  # > _MAX_TRAVERSE(500)
    except RuntimeError as exc:
        tb = safe_traceback(exc, limit=8)
    assert "truncated" in tb


def test_safe_type_name_366():
    """R2 sol MAJOR: имя типа для call-sites санитизировано; обычные - как есть."""
    assert safe_type_name(ValueError("x")) == "ValueError"
    Evil = type("Bad\n" + _CYR_CLASS, (Exception,), {})
    assert "\n" not in safe_type_name(Evil())
    assert _CYR_CLASS not in safe_type_name(Evil())
    assert safe_type_name(None) == "NoneType"  # не падает


def test_safe_traceback_deep_bounded_alloc_366():
    """R2 sol/terra MINOR: очень глубокий traceback - потолок обхода не виснет,
    в выводе только хвост."""
    def _deep(n):
        if n <= 0:
            raise RuntimeError("boom")
        _deep(n - 1)
    try:
        _deep(600)  # больше _MAX_TRAVERSE
    except RuntimeError as exc:
        tb = safe_traceback(exc, limit=8)
    assert tb.count(" <- ") <= 7 and tb  # хвост, не 600 кадров; не пусто


def test_safe_traceback_no_exc_366():
    """Без исключения в контексте - пустая безопасная строка, не падает."""
    assert safe_traceback(None) == ""


# ── проводка: реальная точка-заглушка пишет стек, но не PII (g-055) ──────────

def test_trace_persist_logs_stack_no_pii_366(caplog, monkeypatch):
    """persist_trace_start при сбое сессии логирует at=<стек> (место падения),
    но НЕ SQL/ПД. Мок сессии ПАДАЕТ (g-055: мок проигрывает сбойный путь)."""
    from sreda.runtime import react_trace_persist as tp

    monkeypatch.setattr(tp, "trace_enabled", lambda: True)

    def _boom_session():
        raise RuntimeError(
            "INSERT INTO react_turn_trace ... "
            "parameters: {'origin_user_text': 'my movie list'}")
    monkeypatch.setattr(tp, "_session", _boom_session)

    with caplog.at_level(logging.WARNING, logger="sreda.react_trace"):
        tp.persist_trace_start(
            tenant_id="t", user_id="u", thread_id="th", channel="telegram",
            turn_key="tk", origin_user_text="my movie list")
    rec = " ".join(r.getMessage() for r in caplog.records)
    assert "start failed type=RuntimeError" in rec
    assert "at=" in rec and "react_trace_persist.py:" in rec   # стек есть
    assert "my movie list" not in rec                          # ПД нет
    assert "parameters:" not in rec and "INSERT INTO" not in rec
