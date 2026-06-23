"""#194 — компакция истории ReAct как prompt-view (блочная проекция, БЕЗ LLM).

Покрывает чеклист приёмки: валидность пар (вкл. N>1), current_turn неприкосновенен, char-бюджет,
graceful при current>бюджета, структурное превью с ref, copy-on-write, non-string content, флаг OFF
byte-identical, passthrough, идемпотентность, fail-closed на невалидной проекции.
"""
from __future__ import annotations

import copy

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from sreda.runtime import react_compaction as rc
from sreda.runtime.react_compaction import build_model_input


def _ai_tools(*ids):
    return AIMessage(content="", tool_calls=[
        {"name": "f", "args": {}, "id": i, "type": "tool_call"} for i in ids])


def _tool(tid, content):
    return ToolMessage(content=content, tool_call_id=tid, name="f")


def _human(t):
    return HumanMessage(content=t)


def _ai(t):
    return AIMessage(content=t)


# --- сегментация / пары -----------------------------------------------------

def test_segment_tool_group_multi():
    """1 AIMessage с 2 tool_calls + 2 ToolMessage → ОДИН блок (неделимая tool-группа)."""
    msgs = [_human("привет"), _ai_tools("a", "b"), _tool("a", "r1"), _tool("b", "r2"), _ai("готово")]
    blocks = rc.segment_messages(msgs)
    assert len(blocks) == 3  # human | tool-группа(3 сообщения) | ai
    grp = blocks[1]
    assert grp.is_tool_group and len(grp.messages) == 3
    assert {m.tool_call_id for m in grp.messages if isinstance(m, ToolMessage)} == {"a", "b"}


def test_assert_valid_sequence_detects_broken():
    ok = [_ai_tools("a"), _tool("a", "r")]
    broken_missing = [_ai_tools("a", "b"), _tool("a", "r")]  # нет результата b
    orphan = [_tool("x", "r")]  # сирота
    assert rc._assert_valid_tool_sequence(ok) is True
    assert rc._assert_valid_tool_sequence(broken_missing) is False
    assert rc._assert_valid_tool_sequence(orphan) is False


def test_compact_preserves_tool_pairs_after_toolcalls(monkeypatch):
    """Компакция СРАЗУ после tool_calls (edge): проекция валидна, пары целы."""
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 100)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 0)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 0)
    msgs = []
    for k in range(6):  # 6 ходов с парами
        msgs += [_human(f"ход{k} " * 10), _ai_tools(f"t{k}"), _tool(f"t{k}", f"res{k} " * 20), _ai(f"ответ{k}")]
    out = build_model_input("SP", msgs, enabled=True)
    assert rc._assert_valid_tool_sequence(out[1:]) is True  # без SystemMessage


# --- current_turn -----------------------------------------------------------

def test_current_turn_inviolable(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 50)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 0)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 0)
    old = [_human("старый " * 30), _ai("ответ " * 30)]
    current = [_human("ВОТ ТЕКУЩИЙ ВОПРОС"), _ai_tools("z"), _tool("z", "свежий результат")]
    out = build_model_input("SP", old + current, enabled=True)
    contents = [m.content for m in out]
    assert "ВОТ ТЕКУЩИЙ ВОПРОС" in contents  # последний human цел
    assert any(isinstance(m, ToolMessage) and m.content == "свежий результат" for m in out)


def test_current_turn_exceeds_budget_graceful(monkeypatch):
    """Неустранимый пол: current_turn один > бюджета → весь current, БЕЗ исключения."""
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 50)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 0)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 0)
    current = [_human("огромный текущий вопрос " * 50)]  # сам > бюджета
    out = build_model_input("SP", [_human("старое"), _ai("ст")] + current, enabled=True)
    assert any("огромный текущий вопрос" in str(m.content) for m in out)  # current цел, не упали


# --- char-бюджет ------------------------------------------------------------

def test_total_char_budget_enforced(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 400)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 1)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 2)
    msgs = [_ai(f"старое сообщение номер {k} " * 15) for k in range(20)]
    msgs.append(_human("текущий короткий вопрос"))
    out = build_model_input("SP", msgs, enabled=True)
    # CR: считаем ВЕСЬ out, включая out[0] (system + compaction-note)
    size = sum(rc._content_len(m) for m in out)
    assert size <= rc.TOTAL_BUDGET_CHARS  # note учтён в бюджете → итог укладывается
    assert len(out) < len(msgs) + 1  # реально что-то выброшено


def test_note_within_budget(monkeypatch):
    """CR: бюджет резервирует COMPACTION_NOTE — итоговый prompt (sp+note+проекция) ≤ бюджета."""
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 500)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 0)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 1)
    msgs = [_ai(f"старое {k} " * 20) for k in range(20)] + [_human("текущий")]
    out = build_model_input("SP_БАЗА", msgs, enabled=True)
    assert "свёрнут" in out[0].content  # компакция была, note добавлен
    assert sum(rc._content_len(m) for m in out) <= rc.TOTAL_BUDGET_CHARS


def test_ai_tool_chain_segmentation():
    """CR: цепочка AI+Tool → AI+Tool без Human между → две ОТДЕЛЬНЫЕ tool-группы, не склеены."""
    msgs = [_ai_tools("a"), _tool("a", "r1"), _ai_tools("b"), _tool("b", "r2")]
    blocks = rc.segment_messages(msgs)
    assert len(blocks) == 2
    assert all(b.is_tool_group for b in blocks)
    assert {m.tool_call_id for m in blocks[0].messages if isinstance(m, ToolMessage)} == {"a"}
    assert {m.tool_call_id for m in blocks[1].messages if isinstance(m, ToolMessage)} == {"b"}


def test_duplicate_toolmessage_rejected():
    """CR: дубль ToolMessage на один tool_call_id → последовательность невалидна (ровно один результат)."""
    dup = [_ai_tools("a"), _tool("a", "r1"), _tool("a", "r1-dup")]
    assert rc._assert_valid_tool_sequence(dup) is False


# --- превью -----------------------------------------------------------------

def test_budget_structured_preview_keeps_refs(monkeypatch):
    # бюджет между «после превью» (старый блок влезает) и «сырым» объёмом (>триггер компакции)
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 600)
    monkeypatch.setattr(rc, "PREVIEW_CHARS", 50)
    monkeypatch.setattr(rc, "KEEP_FRESH_TOOLS", 1)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 5)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 5)
    big_list = '{"items": [{"id": 101, "title": "молоко"}, {"id": 102, "title": "хлеб"}]}'
    msgs = [
        _ai_tools("old"), _tool("old", big_list + " " * 200),   # старый большой список
        _human("текущий"), _ai_tools("fresh"), _tool("fresh", "СВЕЖИЙ " * 50),  # свежий — целиком
    ]
    out = build_model_input("SP", msgs, enabled=True)
    old_tool = next(m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "old")
    assert "101" in old_tool.content and "молоко" in old_tool.content  # id/название сохранены
    assert "свёрнуто" in old_tool.content
    fresh_tool = next(m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "fresh")
    assert fresh_tool.content.startswith("СВЕЖИЙ")  # свежий не тронут


# --- copy-on-write ----------------------------------------------------------

def test_copy_on_write_no_mutation(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 100)
    monkeypatch.setattr(rc, "PREVIEW_CHARS", 30)
    monkeypatch.setattr(rc, "KEEP_FRESH_TOOLS", 0)
    msgs = [_ai_tools("a"), _tool("a", "очень длинный результат " * 20), _human("вопрос")]
    before = [(m.content if isinstance(m.content, str) else copy.deepcopy(m.content),
               getattr(m, "tool_call_id", None)) for m in msgs]
    build_model_input("SP", msgs, enabled=True)
    after = [(m.content if isinstance(m.content, str) else copy.deepcopy(m.content),
              getattr(m, "tool_call_id", None)) for m in msgs]
    assert before == after  # КАЖДОЕ входное сообщение не изменено (проекция = клон)


def test_non_string_content_safe(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 100)
    monkeypatch.setattr(rc, "PREVIEW_CHARS", 20)
    monkeypatch.setattr(rc, "KEEP_FRESH_TOOLS", 0)
    multipart = [{"type": "text", "text": "часть " * 30}]
    msgs = [_ai_tools("a"), _tool("a", multipart), _human("вопрос")]
    out = build_model_input("SP", msgs, enabled=True)  # не падает
    assert rc._assert_valid_tool_sequence(out[1:]) is True


# --- флаг / passthrough -----------------------------------------------------

def test_flag_off_byte_identical():
    msgs = [_human("привет"), _ai("ответ")]
    out = build_model_input("SP", msgs, enabled=False)
    assert isinstance(out[0], SystemMessage) and out[0].content == "SP"
    assert out[1:] == msgs  # те же объекты, без изменений


def test_short_dialogue_passthrough_on():
    msgs = [_human("коротко"), _ai("ок")]
    out = build_model_input("SP", msgs, enabled=True)
    assert out[1:] == msgs  # мало текста → ничего не свёрнуто
    assert out[0].content == "SP"  # без compaction-note


# --- идемпотентность / note -------------------------------------------------

def test_repeat_projection_idempotent(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 200)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 1)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 1)
    msgs = [_ai(f"старое {k} " * 20) for k in range(15)] + [_human("текущий")]
    out1 = build_model_input("SP", msgs, enabled=True)
    out2 = build_model_input("SP", msgs, enabled=True)
    # детерминизм + note не накапливается (sp каждый раз из исходного "SP")
    assert [m.content for m in out1] == [m.content for m in out2]
    assert out1[0].content.count("свёрнута") <= 1


def test_note_added_only_when_compacted(monkeypatch):
    monkeypatch.setattr(rc, "TOTAL_BUDGET_CHARS", 200)
    monkeypatch.setattr(rc, "KEEP_HEAD_BLOCKS", 0)
    monkeypatch.setattr(rc, "KEEP_TAIL_BLOCKS", 0)
    msgs = [_ai(f"старое {k} " * 20) for k in range(15)] + [_human("текущий")]
    out = build_model_input("SP", msgs, enabled=True)
    assert out[0].content.startswith("SP")
    assert "свёрнута" in out[0].content  # компакция была → note есть


# --- fail-closed ------------------------------------------------------------

def test_fail_closed_on_invalid_projection(monkeypatch):
    """Если проекция вышла невалидной (симулируем багом) → откат на полную историю БЕЗ note."""
    msgs = [_human("привет"), _ai_tools("a", "b"), _tool("a", "r1"), _tool("b", "r2"), _human("ещё")]
    # подменяем _project на возвращающий битую последовательность (потерян результат b)
    monkeypatch.setattr(rc, "_project", lambda m, s: ([_ai_tools("a", "b"), _tool("a", "r1")], True))
    out = build_model_input("SP", msgs, enabled=True)
    assert out == [SystemMessage("SP"), *msgs]  # fail-closed: полная история, sp без note
