"""#366: PII-safe стек-трейс для логов прод-крашей ReAct.

Прод 2026-07-13: ход падал под ролью sreda_app (RLS), но внешняя страховка
логировала ТОЛЬКО тип ошибки («type=AttributeError»), БЕЗ стека - потому что
str(exc) у SQLAlchemy несёт SQL с параметрами (PII, урок g-039). Итог: слепота
«где упало» → диагностика заняла часы вместо секунд.

Фикс: safe_traceback(exc) логирует ЦЕПОЧКУ КАДРОВ (file:line:func) + типы причин,
но НЕ str(exc)/значения - стек-кадры PII не содержат, место падения видно.
"""
from __future__ import annotations

from sreda.config.log_redaction import safe_traceback


def _raise_chain():
    """AttributeError, вызванный «ProgrammingError» с PII в тексте - как на проде."""
    try:
        try:
            raise ValueError(
                "INSERT INTO react_checkpoint ... "
                "parameters: {'text': 'Что у меня в списке кино', 'secret': 'hunter2'}")
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
    """PII из str(exc) НЕ течёт: ни текста сообщения юзера, ни SQL-параметров,
    ни секрета."""
    tb = safe_traceback(_raise_chain())
    assert "Что у меня в списке" not in tb
    assert "hunter2" not in tb
    assert "parameters:" not in tb
    assert "INSERT INTO" not in tb


def test_safe_traceback_cause_types_366():
    """Цепочка причин - ТОЛЬКО имена типов причин (верхний тип логируется
    отдельным `type=%s` в вызывающем; safe_traceback даёт стек + causes)."""
    tb = safe_traceback(_raise_chain())
    assert "caused-by=ValueError" in tb  # причина по типу
    # но НЕ текст причины/сообщения
    assert "has no attribute" not in tb
    assert "plan_key" not in tb


def test_safe_traceback_bounded_366():
    """Ограничение глубины (не раздуваем лог): дефолт ≤12 кадров = ≤11 разделителей."""
    def _deep(n):
        if n <= 0:
            raise RuntimeError("boom")
        _deep(n - 1)
    try:
        _deep(50)
    except RuntimeError as exc:
        tb = safe_traceback(exc, limit=12)
    assert tb.count(" <- ") <= 11  # 12 кадров → 11 связок


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
    denylist конкретных ПД-строк. Кадры file:line:func, опц. causes."""
    import re
    tb = safe_traceback(_raise_chain())
    assert re.fullmatch(
        r"[\w.]+:\d+:[\w<>]+( <- [\w.]+:\d+:[\w<>]+)*"
        r"( caused-by=\w+(>\w+)*)?", tb), tb


def test_safe_traceback_ctrl_chars_stripped_366():
    """R1 sol MINOR: управляющие символы в co_name (dynamic-compiled) вырезаны."""
    src = "def bad():\n raise RuntimeError('x')\n"
    ns: dict = {}
    exec(compile(src.replace("bad", "bad"), "<dyn\namic>", "exec"), ns)  # noqa: S102
    try:
        ns["bad"]()
    except RuntimeError as exc:
        tb = safe_traceback(exc)
    assert "\n" not in tb and "\x01" not in tb  # переносы/ctrl из co_filename срезаны


def test_safe_traceback_no_exc_366():
    """Без исключения в контексте - пустая безопасная строка, не падает."""
    assert safe_traceback(None) == ""


# ── проводка: реальная точка-заглушка пишет стек, но не PII (g-055) ──────────

def test_trace_persist_logs_stack_no_pii_366(caplog, monkeypatch):
    """persist_trace_start при сбое сессии логирует at=<стек> (место падения),
    но НЕ SQL/ПД. Мок сессии ПАДАЕТ (g-055: мок проигрывает сбойный путь)."""
    import logging

    from sreda.runtime import react_trace_persist as tp

    monkeypatch.setattr(tp, "trace_enabled", lambda: True)

    def _boom_session():
        raise RuntimeError(
            "INSERT INTO react_turn_trace ... "
            "parameters: {'origin_user_text': 'Что у меня в списке кино'}")
    monkeypatch.setattr(tp, "_session", _boom_session)

    with caplog.at_level(logging.WARNING, logger="sreda.react_trace"):
        tp.persist_trace_start(
            tenant_id="t", user_id="u", thread_id="th", channel="telegram",
            turn_key="tk", origin_user_text="Что у меня в списке кино")
    rec = " ".join(r.getMessage() for r in caplog.records)
    assert "start failed type=RuntimeError" in rec
    assert "at=" in rec and "react_trace_persist.py:" in rec   # стек есть
    assert "Что у меня в списке" not in rec                     # ПД нет
    assert "parameters:" not in rec and "INSERT INTO" not in rec
