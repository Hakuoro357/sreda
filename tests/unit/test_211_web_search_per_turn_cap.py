# -*- coding: utf-8 -*-
"""#211 — жёсткий per-ход лимит web_search (отсечка шторма Tavily-квоты).

Один спутанный ход загонял web_search ×35 и выжирал дневную Tavily-квоту.
Фикс: счётчик в замыкании build_web_search_tool (тулсет пересобирается каждый
ход → счётчик per-turn). После N вызовов — машинный статус лимита, БЕЗ сети.
"""
from __future__ import annotations

from types import SimpleNamespace

import sreda.services.web_search_tool as wst


def _stub_settings(cap: int = 4):
    return SimpleNamespace(tavily_api_key="", react_web_search_per_turn_cap=cap)


def test_web_search_per_turn_cap_blocks_after_n(monkeypatch):
    """N веб-поисков проходят, (N+1)-й → лимит, и в сеть НЕ ходит."""
    net = []
    monkeypatch.setattr(
        wst, "_ddg_fallback", lambda q, *a, **k: (net.append(q), "ddg-stub")[1]
    )
    monkeypatch.setattr(wst, "get_settings", lambda: _stub_settings())
    tool = wst.build_web_search_tool(session=None, per_turn_cap=2)

    r1 = tool.invoke({"query": "первый"})
    r2 = tool.invoke({"query": "второй"})
    r3 = tool.invoke({"query": "третий"})

    assert r1 == "ddg-stub" and r2 == "ddg-stub"
    assert r3.startswith("error:web_search_turn_limit"), r3
    assert len(net) == 2, "3-й поиск дошёл до сети — отсечка не сработала ДО вызова"


def test_web_search_per_turn_cap_resets_per_build(monkeypatch):
    """Счётчик per-turn: новый build (= новый ход) обнуляет лимит."""
    monkeypatch.setattr(wst, "_ddg_fallback", lambda q, *a, **k: "ddg")
    monkeypatch.setattr(wst, "get_settings", lambda: _stub_settings())

    t1 = wst.build_web_search_tool(session=None, per_turn_cap=1)
    assert t1.invoke({"query": "a"}) == "ddg"
    assert t1.invoke({"query": "b"}).startswith("error:web_search_turn_limit")

    # новый ход → новый tool → счётчик с нуля
    t2 = wst.build_web_search_tool(session=None, per_turn_cap=1)
    assert t2.invoke({"query": "c"}) == "ddg", "лимит не сбросился на новом ходе"


def test_web_search_per_turn_cap_zero_disables(monkeypatch):
    """cap=0 → без per-ход лимита (прежнее поведение, регресс-гард)."""
    monkeypatch.setattr(wst, "_ddg_fallback", lambda q, *a, **k: "ddg")
    monkeypatch.setattr(wst, "get_settings", lambda: _stub_settings(cap=0))

    t = wst.build_web_search_tool(session=None, per_turn_cap=0)
    for _ in range(10):
        assert t.invoke({"query": "x"}) == "ddg"


def test_web_search_per_turn_cap_default_from_settings(monkeypatch):
    """per_turn_cap=None → берётся из настроек (дефолт)."""
    monkeypatch.setattr(wst, "_ddg_fallback", lambda q, *a, **k: "ddg")
    monkeypatch.setattr(wst, "get_settings", lambda: _stub_settings(cap=1))

    t = wst.build_web_search_tool(session=None)  # per_turn_cap=None → из настроек (1)
    assert t.invoke({"query": "a"}) == "ddg"
    assert t.invoke({"query": "b"}).startswith("error:web_search_turn_limit")


def test_web_search_per_turn_cap_concurrent_no_overrun(monkeypatch):
    """Codex R1 MAJOR: при КОНКУРЕНТНЫХ вызовах (plan-execute read-batch через
    asyncio.gather / thread-pool) lock не даёт переполнить лимит — РОВНО cap
    доходят до сети, остальные получают статус лимита."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    net = []
    net_lock = threading.Lock()

    def _net(q, *a, **k):
        with net_lock:
            net.append(q)
        return "ddg"

    monkeypatch.setattr(wst, "_ddg_fallback", _net)
    monkeypatch.setattr(wst, "get_settings", lambda: _stub_settings())
    tool = wst.build_web_search_tool(session=None, per_turn_cap=3)

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(lambda i: tool.invoke({"query": f"q{i}"}), range(20)))

    assert len(net) == 3, f"в сеть ушло {len(net)} вместо 3 — гонка переполнила лимит"
    limited = sum(1 for r in results if r.startswith("error:web_search_turn_limit"))
    assert limited == 17


def test_web_search_negative_cap_clamped_to_disabled(monkeypatch):
    """Codex R1 MINOR: отрицательный cap клампится к 0 (выкл), не падает."""
    monkeypatch.setattr(wst, "_ddg_fallback", lambda q, *a, **k: "ddg")
    monkeypatch.setattr(wst, "get_settings", lambda: _stub_settings(cap=-1))

    t = wst.build_web_search_tool(session=None)  # cap из настроек = -1 → 0
    for _ in range(8):
        assert t.invoke({"query": "x"}) == "ddg"
