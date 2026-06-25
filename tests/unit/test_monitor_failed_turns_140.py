# -*- coding: utf-8 -*-
"""#140 — probe_failed_turns_rate должна считать провалом «ход отдал
поломку», а НЕ iters==0.

`iters` — счётчик старого tool-loop; у планировщика (plan-execute) он ВСЕГДА
0, даже на успехе. Прецедент 2026-06-13: тестовый прогон протолкнул n≥5
iters=0 → ложный CRITICAL; и сами успешные ходы Бориса (iters=0) проба
считала провалами → KPI ≥95% недостоверен на трафике планировщика.

Честный сигнал: трейс несёт `outcome=ok|breakdown` (ставится в единой точке
показа «поломки» через note_breakdown → trace.mark_outcome).
"""
from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "monitor_health.py"


def _load_monitor():
    spec = importlib.util.spec_from_file_location("monitor_health_140", _MONITOR)
    mod = importlib.util.module_from_spec(spec)
    # dataclass-аннотации монитора резолвятся через sys.modules — регистрируем
    # ДО exec, иначе dataclasses падает на ClassVar-lookup.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pg_out(counts: dict[str, int]) -> str:
    """Эмуляция psql -tA `SELECT outcome, count(*) ... GROUP BY outcome`:
    строки вида ``outcome|count`` (разделитель | по умолчанию)."""
    return "\n".join(f"{oc}|{c}" for oc, c in counts.items())


# #227: проба читает БД react_turn_trace (источник истины ReAct), НЕ trace.log
# (там у ReAct всегда outcome=ok — react_loop не зовёт mark_outcome → проба была
# слепа к провалам на всём проде). Провал = safe_reply | breakdown.
def test_safe_reply_counted_failed(monkeypatch):
    # РЕГРЕСС #227: ReAct-провал = safe_reply (краш / сетевой сбой LLM). 2/5=40% → critical.
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query",
                        lambda sql: _pg_out({"ok": 3, "safe_reply": 2}))
    r = mod.probe_failed_turns_rate()
    assert r.status == "critical", f"2/5 safe_reply (40%) → critical: {r.message}"


def test_low_traffic_burst_counted_failed(monkeypatch):
    # НОЧНОЙ СЦЕНАРИЙ #227: низкий трафик (n<5, rate-гейт не сработает), но 3
    # safe_reply = всплеск краша/сетевого сбоя → critical по абсолютному порогу.
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query", lambda sql: _pg_out({"safe_reply": 3}))
    r = mod.probe_failed_turns_rate()
    assert r.status == "critical", f"3 safe_reply при n<5 → critical (всплеск): {r.message}"


def test_breakdown_still_counted_failed(monkeypatch):
    # back-compat: legacy breakdown по-прежнему провал.
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query",
                        lambda sql: _pg_out({"ok": 3, "breakdown": 2}))
    r = mod.probe_failed_turns_rate()
    assert r.status == "critical", f"2/5 breakdown (40%) → critical: {r.message}"


def test_all_ok_not_counted_failed(monkeypatch):
    # ГЛАВНОЕ (наследие #140): успешные ходы НЕ провал.
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query", lambda sql: _pg_out({"ok": 10}))
    r = mod.probe_failed_turns_rate()
    assert r.status == "ok", f"10/10 ok → не провал: {r.message}"


def test_below_sample_size_not_critical(monkeypatch):
    # n<5 → не алертим даже при высокой доле (1 safe_reply из 2 = 50%, но n=2).
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query",
                        lambda sql: _pg_out({"ok": 1, "safe_reply": 1}))
    r = mod.probe_failed_turns_rate()
    assert r.status == "ok", f"n<5 → не critical: {r.message}"


def test_pg_query_failure_is_warning(monkeypatch):
    # psql упал (None) → warning, не ложный critical/ok.
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query", lambda sql: None)
    r = mod.probe_failed_turns_rate()
    assert r.status == "warning", f"psql fail → warning: {r.message}"


def test_no_finished_turns_is_ok(monkeypatch):
    # пустой результат (нет завершённых ходов) → ok, не падение.
    mod = _load_monitor()
    monkeypatch.setattr(mod, "_pg_query", lambda sql: "")
    r = mod.probe_failed_turns_rate()
    assert r.status == "ok", f"нет ходов → ok: {r.message}"


def test_emit_block_writes_outcome():
    # trace.mark_outcome → emit_block пишет outcome= в строку TOTAL.
    # Свой handler прямо на логгер (НЕ caplog): в полном наборе другой тест
    # переконфигурирует "sreda.trace" через dictConfig, и caplog не ловит.
    import logging

    from sreda.services import trace as trace_mod

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("sreda.trace")
    handler = _Capture()
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    ctx = trace_mod.TraceContext(
        trace_id="trace_test140", user_id="u", tenant_id="t",
        channel="telegram", started_at=datetime.now(timezone.utc),
        started_monotonic=time.monotonic(),
    )
    ctx.events.append(trace_mod.TraceEvent(at_ms=0, step="webhook.received",
                                           duration_ms=0, meta={}))
    trace_mod.set_current(ctx)
    try:
        trace_mod.mark_outcome("breakdown")
        trace_mod.emit_block(ctx)
    finally:
        trace_mod.set_current(None)
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled
    blob = "\n".join(captured)
    assert "outcome=breakdown" in blob, f"нет outcome в TOTAL: {blob!r}"


def _capture_emit(ctx) -> str:
    """Эмитит trace-блок, ловя вывод своим handler'ом (устойчиво к dictConfig)."""
    import logging

    from sreda.services import trace as trace_mod
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("sreda.trace")
    handler = _Capture()
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        trace_mod.emit_block(ctx)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled
    return "\n".join(captured)


def test_outbox_roundtrip_preserves_outcome():
    # Codex CRITICAL: финальный трейс эмитит ВОРКЕР из десериализованного ctx.
    # outcome обязан пережить serialize→deserialize, иначе поломки → outcome=ok.
    from sreda.services import trace as trace_mod
    ctx = trace_mod.TraceContext(
        trace_id="trace_rt140", user_id="u", tenant_id="t",
        channel="telegram", started_at=datetime.now(timezone.utc),
        started_monotonic=time.monotonic(),
    )
    ctx.events.append(trace_mod.TraceEvent(at_ms=0, step="webhook.received",
                                           duration_ms=0, meta={}))
    trace_mod.set_current(ctx)
    try:
        trace_mod.mark_outcome("breakdown")
    finally:
        trace_mod.set_current(None)
    payload = trace_mod.serialize_for_outbox(ctx)
    assert payload.get("outcome") == "breakdown", "outcome не сериализован"
    worker_ctx = trace_mod.deserialize_from_outbox(payload)
    assert worker_ctx.outcome == "breakdown", "outcome потерян при десериализации"
    blob = _capture_emit(worker_ctx)
    assert "outcome=breakdown" in blob, f"воркер не записал outcome: {blob!r}"


def test_mark_outcome_breakdown_is_sticky():
    # Codex MINOR: после breakdown последующий 'ok' не должен понизить исход.
    from sreda.services import trace as trace_mod
    ctx = trace_mod.TraceContext(
        trace_id="trace_sticky", user_id="u", tenant_id="t",
        channel="telegram", started_at=datetime.now(timezone.utc),
        started_monotonic=time.monotonic(),
    )
    trace_mod.set_current(ctx)
    try:
        trace_mod.mark_outcome("breakdown")
        trace_mod.mark_outcome("ok")  # попытка понизить
    finally:
        trace_mod.set_current(None)
    assert ctx.outcome == "breakdown", "breakdown не липкий — понижен до ok"


def test_note_breakdown_marks_trace_outcome():
    # note_breakdown (единая точка показа поломки) ставит outcome=breakdown
    # на текущий trace-контекст.
    from sreda.services import trace as trace_mod
    from sreda.services.composer.breakdown_messages import note_breakdown
    ctx = trace_mod.TraceContext(
        trace_id="trace_test140b", user_id="u", tenant_id="t",
        channel="telegram", started_at=datetime.now(timezone.utc),
        started_monotonic=time.monotonic(),
    )
    trace_mod.set_current(ctx)
    try:
        note_breakdown("test:source", "деталь", alert=False)
    finally:
        trace_mod.set_current(None)
    assert ctx.outcome == "breakdown", "note_breakdown не пометил trace"
