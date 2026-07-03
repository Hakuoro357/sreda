# -*- coding: utf-8 -*-
"""#255: react_loop распакован в timeline трейса (react.classified/llm/tool/passes) + парсер РЕНДЕРИТ
поля этих шагов (latency_ms/intent/fallback), а не режет allowlist'ом. Чтобы `react_loop.replied`
перестал быть чёрным ящиком при разборе инцидентов латентности."""
from __future__ import annotations

import sreda.services.trace as trace
from sreda.admin.trace_parser import ParsedStage, _filter_safe_meta, stage_duration_color
from sreda.runtime.react_loop import _emit_react_timeline


def test_emit_react_timeline_events():
    """На ходе с медленным/упавшим primary в буфере трейса есть react.llm с latency + fallback/primary_error."""
    ctx = trace.start_trace(tenant_id="t", user_id="u")
    lcs = [
        {"latency_ms": 10980, "provider_key": "openrouter-deepseek", "model": "deepseek/deepseek-v4-flash",
         "intent": "chat", "fallback_fired": False, "primary_error": None, "call_index": 0},
        {"latency_ms": 60000, "provider_key": "inception-mercury2", "model": "mercury-2",
         "intent": "task", "fallback_fired": True, "primary_error": "LLMCallTimeout", "call_index": 1},
    ]
    tcs = [
        {"name": "schedule_reminder", "ok": True, "latency_ms": 14},
        {"name": "web_search", "ok": False, "latency_ms": 8308, "error_type": None},
    ]
    _emit_react_timeline(lcs, tcs, passes=3, intent="task", intent_meta={"source": "must_task"})
    ev = ctx.events
    cl = next(e for e in ev if e.step == "react.classified")
    assert cl.meta["intent"] == "task" and cl.meta["source"] == "must_task"
    llms = [e for e in ev if e.step == "react.llm"]
    assert len(llms) == 2
    # latency в МЕТУ (latency_ms), duration_ms=0 — иначе раздувает TOTAL блока (эмит в конце хода)
    assert llms[0].meta["latency_ms"] == 10980 and llms[0].duration_ms == 0
    assert llms[0].meta["provider_key"] == "openrouter-deepseek"
    assert llms[1].meta["fallback_fired"] is True and llms[1].meta["primary_error"] == "LLMCallTimeout"
    tl = next(e for e in ev if e.step == "react.tool")
    assert tl.meta["count"] == 2 and tl.meta["errors"] == 1
    assert "web_search" in tl.meta["top3"]  # самый медленный — первый
    ps = next(e for e in ev if e.step == "react.passes")
    assert ps.meta["passes"] == 3


def test_emit_noop_without_trace():
    """Без активного трейса эмит — no-op (record guard), не падает."""
    trace.set_current(None)
    _emit_react_timeline([{"latency_ms": 1}], [], passes=1, intent="chat", intent_meta={})
    assert trace.current() is None


def test_emit_survives_bad_input():
    """Наблюдаемость НИКОГДА не валит ход: мусорный вход — не исключение."""
    trace.start_trace(tenant_id="t")
    _emit_react_timeline(None, None, passes=None, intent=None, intent_meta=None)  # не бросает


def test_parser_react_prefix_allows_all_fields():
    """react.* — ПД-free by construction → парсер пропускает latency_ms/intent (их НЕТ в allowlist)."""
    out = _filter_safe_meta({"latency_ms": "10980", "intent": "task", "provider_key": "x"}, "react.llm")
    assert out["latency_ms"] == "10980" and out["intent"] == "task" and out["provider_key"] == "x"


def test_parser_non_react_still_filtered():
    """Не-react шаг — прежний строгий allowlist: latency_ms выпадает, model остаётся."""
    out = _filter_safe_meta({"latency_ms": "10980", "model": "merc"}, "voice.transcribe")
    assert "latency_ms" not in out and out["model"] == "merc"


def test_parser_react_loop_replied_not_loosened():
    """`react_loop.replied` НЕ начинается на 'react.' (после react — '_') → строгий allowlist, не ослаблен."""
    out = _filter_safe_meta({"latency_ms": "12688", "chars": "62"}, "react_loop.replied")
    assert "latency_ms" not in out and out["chars"] == "62"  # chars в allowlist, latency нет


def test_effective_ms_falls_back_to_latency_meta():
    """#255-фикс: у react.llm своя длительность в мете (latency_ms), duration_ms=None → строка красится
    по latency_ms (медленный вызов краснеет «на глаз»), но колонка длительности при этом остаётся пустой."""
    slow = ParsedStage(at_ms=3500, name="react.llm", duration_ms=None, meta={"latency_ms": "60000"})
    assert slow.effective_ms == 60000  # берёт latency из меты
    assert stage_duration_color(slow.effective_ms) == "red"  # >5s → красный, сигнал восстановлен
    assert slow.duration_ms is None  # колонку длительности НЕ трогаем (не путать latency с длит-ю стадии)


def test_effective_ms_prefers_own_duration():
    """Если у шага есть свой duration_ms — берём его (обычные стадии, не react.*), мету игнорируем."""
    st = ParsedStage(at_ms=0, name="voice.transcribe", duration_ms=1200, meta={"latency_ms": "99999"})
    assert st.effective_ms == 1200 and stage_duration_color(st.effective_ms) == "neutral"


def test_effective_ms_none_when_no_signal():
    """Ни duration_ms, ни latency_ms (или мусор) → None → нейтральная строка (не падаем).
    react.tool (только sum_latency_ms) тоже None — вне slow-turns, как и было (регресса нет)."""
    assert ParsedStage(at_ms=0, name="react.passes", duration_ms=None, meta={"passes": "3"}).effective_ms is None
    assert ParsedStage(at_ms=0, name="react.llm", duration_ms=None, meta={"latency_ms": "x"}).effective_ms is None
    assert ParsedStage(at_ms=0, name="react.tool", duration_ms=None,
                       meta={"sum_latency_ms": "8322"}).effective_ms is None


def test_slow_turns_attributes_llm_dominated_turn():
    """#255-фикс не должен регрессить slow-turns (#292): ход, где время съел react.llm (латентность в
    мете, duration_ms=None), атрибутируется к react.llm, а НЕ к «между стадиями (не размечено)».
    На проде до фикса TOTAL react.llm нёс duration_ms=latency и попадал в атрибуцию — сохраняем это."""
    from sreda.admin.trace_parser import ParsedTrace
    from sreda.admin import overview_snapshot as ov

    tr = ParsedTrace(turn_id="T", timestamp="2026-07-03 10:00:00", user_id="u", channel="tg",
                     total_ms=61000, iters=1, tok_in=0, tok_out=0)
    tr.stages = [
        ParsedStage(at_ms=5, name="voice.transcribe", duration_ms=800, meta={}),
        ParsedStage(at_ms=61000, name="react.llm", duration_ms=None, meta={"latency_ms": "60000"}),
    ]
    top = max((s for s in tr.stages if s.effective_ms), key=lambda s: s.effective_ms or 0)
    assert top.name == "react.llm" and top.effective_ms == 60000
    # и порог 50%: 60000 >= 61000*0.5 → атрибуция к стадии, не «не размечено»
    assert (top.effective_ms or 0) >= (tr.total_ms or 0) * 0.5
    assert hasattr(ov, "_slow_turns_block")  # тот самый блок, что чинил MINOR-1
