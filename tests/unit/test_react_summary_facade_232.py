"""#232 шаг B — фасад run_post_turn_summary (генерация+запись выжимки), сторожа оркестрации.

FakeGraph + asyncio.run (без реального чекпойнтера/БД). Покрывает: durable-гейт, пропуск на паузе,
базовый триггер (мало старого), happy-path (aupdate_state с as_node="chat" + запись covered_hash +
учёт task_type="summary"), пропуск при смене head между чтением и записью (гонка R3/R4), анти-петлю.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from sreda.runtime import react_loop as rl
from sreda.runtime import react_compaction as rc


def _hist(n_turns=12, pad=10):
    msgs = []
    for k in range(n_turns):
        msgs += [HumanMessage(f"вопрос{k} " * pad), AIMessage(f"ответ{k} " * pad)]
    return msgs


class _Snap:
    def __init__(self, values, paused=False):
        self.values = values
        self.tasks = []
        self.next = ("chat",) if paused else None
        self.created_at = None


class _FakeGraph:
    def __init__(self, snaps):
        self._snaps = list(snaps)
        self._i = 0
        self.updates = []

    async def aget_state(self, cfg):
        s = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return s

    async def aupdate_state(self, cfg, values, as_node=None):
        self.updates.append({"values": values, "as_node": as_node})


class _FakeSession:
    def get_bind(self):
        return None

    def close(self):
        pass


@pytest.fixture
def wired(monkeypatch):
    """Заглушки общих зависимостей фасада; тест подменяет _build_graph/snap под сценарий."""
    monkeypatch.setattr(rl, "_persist_enabled", lambda: True)
    monkeypatch.setattr(rl, "build_slice_tools", lambda *a, **k: [])
    monkeypatch.setattr(rl, "react_provider", lambda tid: "inception-mercury2")
    monkeypatch.setattr(rl, "_extract_usage", lambda resp: (10, 5))
    monkeypatch.setattr("sreda.db.session.get_session_factory", lambda: (lambda: _FakeSession()))
    monkeypatch.setattr("sreda.services.llm.get_chat_llm", lambda *a, **k: object())  # не None
    monkeypatch.setattr(rl, "invoke_with_per_call_timeout",
                        lambda llm, msgs, **k: AIMessage("ВЫЖИМКА: важные факты сохранены."))
    recorded = []
    monkeypatch.setattr(rl, "_record_react_usage", lambda **kw: recorded.append(kw))
    return {"recorded": recorded}


def _run():
    asyncio.run(rl.run_post_turn_summary(
        tenant_id="tenant_tg_1", user_id="u1", thread_id="react:tenant_tg_1:1", channel="telegram"))


def test_skip_when_not_persist(monkeypatch, wired):
    monkeypatch.setattr(rl, "_persist_enabled", lambda: False)
    built = []
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: built.append(1))
    _run()
    assert built == []  # durable выкл → даже граф не строим


def test_skip_on_pause(monkeypatch, wired):
    g = _FakeGraph([_Snap({"messages": _hist()}, paused=True)])
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: g)
    _run()
    assert g.updates == []  # paused → не пишем (resume не ломаем)


def test_skip_when_too_little_history(monkeypatch, wired):
    g = _FakeGraph([_Snap({"messages": _hist(n_turns=2)})])  # старого мало
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: g)
    _run()
    assert g.updates == []


def test_happy_path_writes_summary(monkeypatch, wired):
    msgs = _hist(n_turns=12)
    g = _FakeGraph([_Snap({"messages": msgs}), _Snap({"messages": msgs})])  # read + re-read одинаковы
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: g)
    _run()
    assert len(g.updates) == 1
    upd = g.updates[0]
    assert upd["as_node"] == "chat"                       # обязателен (иначе InvalidUpdateError на проде)
    rec = upd["values"]["history_summary"]
    covered_n, _ = rc.summary_coverage(msgs)
    assert rec["covered_message_count"] == covered_n
    assert rec["covered_hash"] == rc._history_prefix_hash(msgs, covered_n)
    assert rec["version"] == rc._SUMMARY_VERSION
    # учёт: записан как summary с credits=0-паттерном (#175)
    assert wired["recorded"] and wired["recorded"][0]["task_type"] == "summary"


def test_skip_on_head_change_before_write(monkeypatch, wired):
    msgs = _hist(n_turns=12)
    changed = list(msgs)
    changed[0] = HumanMessage("СОВСЕМ ДРУГОЕ первое сообщение")  # покрытый префикс разошёлся
    g = _FakeGraph([_Snap({"messages": msgs}), _Snap({"messages": changed})])
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: g)
    _run()
    assert g.updates == []  # head сменился между чтением и записью → skip, не перезапись


def test_anti_loop_already_covered(monkeypatch, wired):
    msgs = _hist(n_turns=12)
    covered_n, _ = rc.summary_coverage(msgs)
    prev = {"version": 1, "text": "старая выжимка", "covered_message_count": covered_n,
            "covered_hash": rc._history_prefix_hash(msgs, covered_n)}
    g = _FakeGraph([_Snap({"messages": msgs, "history_summary": prev})])
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: g)
    _run()
    assert g.updates == []  # префикс уже покрыт не меньше → повторно не пересказываем
