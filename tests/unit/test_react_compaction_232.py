"""#232 шаг A — потребление выжимки истории (consumption side, БЕЗ генерации).

Чеклист приёмки (машинные пункты):
- summary=None → байт-идентично #194 (регрессия не ломает базу);
- выжимка вставляется fenced в ЕДИНЫЙ ведущий SystemMessage ТОЛЬКО при компакции;
- история влезает (нет компакции) → выжимка НЕ вставляется (полная история на месте);
- устаревший covered_hash / неизвестная версия / малформ / не-dict → fail-open (как #194), без падения;
- tool-последовательность проекции валидна при наличии выжимки;
- выжимка живёт в sp → не выкидывается даже при крошечном бюджете (недропаема по построению).
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime import react_compaction as rc
from sreda.runtime.react_compaction import build_model_input

FENCE = "[ДАННЫЕ О РАЗГОВОРЕ"  # маркер fenced-секции выжимки в ведущем SystemMessage


def _human(t):
    return HumanMessage(content=t)


def _ai(t):
    return AIMessage(content=t)


def _ai_tools(*ids):
    return AIMessage(content="", tool_calls=[
        {"name": "f", "args": {}, "id": i, "type": "tool_call"} for i in ids])


def _tool(tid, content):
    return ToolMessage(content=content, tool_call_id=tid, name="f")


def _long_history(n_turns=12, pad=40):
    msgs = []
    for k in range(n_turns):
        msgs += [_human(f"вопрос{k} " * pad), _ai(f"ответ{k} " * pad)]
    return msgs


def _summary_for(messages, covered_n, text="ВЫЖИМКА: ранее обсудили проект Орбита, дедлайн 14 марта."):
    return {
        "version": 1,
        "text": text,
        "covered_message_count": covered_n,
        "covered_hash": rc._history_prefix_hash(messages, covered_n),
    }


def test_summary_none_byte_identical_to_194(monkeypatch):
    """summary=None (и отсутствие параметра) → результат тот же, что у #194."""
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 300)
    msgs = _long_history()
    a = build_model_input("SP", msgs, enabled=True)
    b = build_model_input("SP", msgs, enabled=True, summary=None)
    assert [type(m) for m in a] == [type(m) for m in b]
    assert [m.content for m in a] == [m.content for m in b]


def test_summary_injected_when_compacted(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 300)
    msgs = _long_history()
    out = build_model_input("SP", msgs, enabled=True, summary=_summary_for(msgs, covered_n=8))
    sp = out[0].content
    assert FENCE in sp                 # fenced-секция в ведущем system
    assert "Орбита" in sp              # текст выжимки реально вставлен


def test_summary_not_injected_when_history_fits(monkeypatch):
    """Полная история влезает в бюджет → компакции нет → выжимка не нужна и не вставляется."""
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 100000)
    msgs = _long_history(n_turns=3)
    out = build_model_input("SP", msgs, enabled=True, summary=_summary_for(msgs, covered_n=2))
    assert FENCE not in out[0].content
    assert len(out) == 1 + len(msgs)   # passthrough


def test_summary_stale_hash_fail_open(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 300)
    msgs = _long_history()
    s = _summary_for(msgs, covered_n=8)
    s["covered_hash"] = "deadbeef"     # история изменилась → префикс не совпал
    out = build_model_input("SP", msgs, enabled=True, summary=s)
    assert FENCE not in out[0].content  # fail-open на обычную #194-проекцию


def test_summary_unknown_version_fail_open(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 300)
    msgs = _long_history()
    s = _summary_for(msgs, covered_n=8)
    s["version"] = 999
    out = build_model_input("SP", msgs, enabled=True, summary=s)
    assert FENCE not in out[0].content


@pytest.mark.parametrize("bad", [
    {}, {"text": "x"}, {"version": 1, "text": "x"}, "notadict", None,
    {"version": 1, "text": "x", "covered_message_count": "NaN", "covered_hash": "z"},
])
def test_summary_malformed_fail_open(monkeypatch, bad):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 300)
    msgs = _long_history()
    out = build_model_input("SP", msgs, enabled=True, summary=bad)  # не падает
    assert FENCE not in out[0].content


def test_tool_sequence_valid_with_summary(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 300)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 1)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 1)
    msgs = []
    for k in range(8):
        msgs += [_human(f"ход{k} " * 20), _ai_tools(f"t{k}"), _tool(f"t{k}", f"res{k} " * 20), _ai(f"ок{k}")]
    out = build_model_input("SP", msgs, enabled=True, summary=_summary_for(msgs, covered_n=4))
    assert rc._assert_valid_tool_sequence(out[1:]) is True


def test_summary_in_sp_survives_tiny_budget(monkeypatch):
    """Крошечный бюджет дропает сырые сообщения, но выжимка в sp остаётся (недропаема)."""
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 200)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 0)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 0)
    msgs = _long_history(n_turns=20)
    out = build_model_input("SP", msgs, enabled=True, summary=_summary_for(msgs, covered_n=10, text="ВЫЖИМКАКРИТ"))
    assert "ВЫЖИМКАКРИТ" in out[0].content


def test_prefix_hash_changes_with_history():
    """covered_hash меняется при изменении покрытого префикса (основа применимости)."""
    msgs = _long_history(n_turns=6)
    h1 = rc._history_prefix_hash(msgs, 4)
    msgs2 = list(msgs)
    msgs2[1] = _ai("ДРУГОЙ ответ")
    assert rc._history_prefix_hash(msgs2, 4) != h1
    assert rc._history_prefix_hash(msgs, 4) == h1  # детерминирован


# --- B1: ядро генерации (покрытие + запись), чисто, без LLM/графа --------------

def test_summary_coverage_long_history(monkeypatch):
    """Покрываем старый префикс (старые блоки минус хвост и current_turn); это ведущие N сообщений."""
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 2)
    msgs = _long_history(n_turns=10)  # 20 простых блоков
    covered_n, coverable = rc.summary_coverage(msgs)
    assert covered_n > 0
    assert covered_n == len(coverable)
    assert coverable == msgs[:covered_n]  # ведущий префикс (для covered_hash)
    # current_turn (последний human..конец) НЕ покрыт
    assert covered_n < len(msgs)


def test_summary_coverage_short_history_nothing(monkeypatch):
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 8)
    msgs = _long_history(n_turns=1)  # 2 сообщения — старого нечего сворачивать
    assert rc.summary_coverage(msgs) == (0, [])


def test_make_summary_record_caps_and_hashes():
    msgs = _long_history(n_turns=6)
    rec = rc.make_summary_record("Я" * 5000, msgs, covered_n=8)
    assert len(rec["text"]) <= rc.SUMMARY_MAX_CHARS         # пере-сжатие: потолок текста
    assert rec["covered_message_count"] == 8
    assert rec["covered_hash"] == rc._history_prefix_hash(msgs, 8)
    assert rec["version"] == rc._SUMMARY_VERSION


def test_make_summary_record_roundtrip_applicable(monkeypatch):
    """Запись от генерации ОБЯЗАНА быть применимой на потреблении (covered_hash совпал)."""
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 2)
    msgs = _long_history(n_turns=10)
    covered_n, _ = rc.summary_coverage(msgs)
    rec = rc.make_summary_record("ВЫЖИМКА теста", msgs, covered_n)
    assert rc._applicable_summary_text(rec, msgs) != ""     # generation→consumption замкнуто
