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


def _block(trace_id: str, *, type_: str = "text", iters: int = 0,
           outcome: str = "ok") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return (
        f"=== TRACE {trace_id} {ts} user=u chan=telegram ===\n"
        f"      0ms  webhook.received       type={type_}\n"
        f"      5ms  ack.sent  [10ms] phrase=ack\n"
        f"  100ms  planner.plan           [90ms]\n"
        f"------- TOTAL 100ms iters={iters} tok_in=0 tok_out=0 outcome={outcome}\n"
    )


def _write_log(tmp_path, blocks: list[str]) -> Path:
    p = tmp_path / "trace.log"
    p.write_text("\n".join(blocks), encoding="utf-8")
    return p


def test_planner_success_iters0_not_counted_failed(tmp_path, monkeypatch):
    # ГЛАВНОЕ: успешные ходы планировщика (iters=0, outcome=ok) НЕ провал.
    mod = _load_monitor()
    log = _write_log(tmp_path, [_block(f"t{i}", iters=0, outcome="ok")
                                for i in range(5)])
    monkeypatch.setattr(mod, "TRACE_LOG", log)
    r = mod.probe_failed_turns_rate()
    assert r.status == "ok", (
        f"успешные ходы планировщика (iters=0) не должны считаться провалом: "
        f"{r.message}"
    )


def test_breakdown_turns_counted_failed(tmp_path, monkeypatch):
    # Поломки считаются: 2 из 5 = 40% > 20% → critical.
    mod = _load_monitor()
    blocks = ([_block(f"ok{i}", outcome="ok") for i in range(3)]
              + [_block(f"bd{i}", outcome="breakdown") for i in range(2)])
    log = _write_log(tmp_path, blocks)
    monkeypatch.setattr(mod, "TRACE_LOG", log)
    r = mod.probe_failed_turns_rate()
    assert r.status == "critical", f"2/5 поломок (40%) → critical: {r.message}"


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
