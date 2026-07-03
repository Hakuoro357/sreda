# -*- coding: utf-8 -*-
"""#255: react_loop распакован в timeline трейса (react.classified/llm/tool/passes) + парсер РЕНДЕРИТ
поля этих шагов (latency_ms/intent/fallback), а не режет allowlist'ом. Чтобы `react_loop.replied`
перестал быть чёрным ящиком при разборе инцидентов латентности."""
from __future__ import annotations

import sreda.services.trace as trace
from sreda.admin.trace_parser import _filter_safe_meta
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
    assert llms[0].duration_ms == 10980 and llms[0].meta["provider_key"] == "openrouter-deepseek"
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
