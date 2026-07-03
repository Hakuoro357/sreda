"""#285 Фаза 0: output-guard скрипта базлайна — в выводе НЕТ пользовательских текстов/аргументов.

Скрипт гоняется на проде с ключами расшифровки; гарантия «только агрегаты» закреплена тестом
(R1 фазового ревью, CodexH-9): фейк-строки с текстом-сентинелом → сентинел не появляется в выводе.
Заодно: детекторы v2 соответствуют корпусам (заглавная первая буква деклараций ловится,
near-miss «меня зовут на дачу» и «удалось» — нет).
"""

from __future__ import annotations

import json
from types import SimpleNamespace


def _row(text: str, tools: list[dict], confirm: str = "none"):
    return SimpleNamespace(
        origin_user_text=text,
        tool_calls_json=json.dumps(tools),
        confirm_state=confirm,
        created_at=None,
        tenant_id="tenant_x",
        status="done",
    )


SENTINEL = "СЕКРЕТНЫЙ_ТЕКСТ_ЮЗЕРА_12345"


def test_output_contains_no_user_text():
    from scripts.analysis_285_baseline import compute, render

    rows = [
        _row(f"{SENTINEL} добавь молоко", [{"name": "add_shopping_items", "result_kind": "ok"}]),
        _row(f"как дела? {SENTINEL}", [{"name": "save_core_fact", "result_kind": "ok"}]),
        _row(SENTINEL, [], confirm="pending"),
    ]
    out = render(compute(rows))
    assert SENTINEL not in out
    assert "tenant_x" not in out  # тенанты только счётом
    assert "save_core_fact" in out  # имя инструмента БЕЗсигнального хода — допустимый агрегат


def test_detectors_match_corpora_v2():
    from scripts.analysis_285_baseline import _detectors

    # декларативы: заглавная ПЕРВАЯ буква фразы ловится (R1 MAJOR субагента)
    assert _detectors("Меня зовут Таня")["decl"] is True
    assert _detectors("меня зовут Таня")["decl"] is True
    # near-miss: имя не с заглавной → нет сигнала
    assert _detectors("меня зовут на дачу в выходные")["decl"] is False
    # команды: паразит «удалось» не матчится, «удали» матчится
    assert _detectors("не удалось открыть")["cmd"] is False
    assert _detectors("удали напоминание")["cmd"] is True
    # write-сигнал v0 НЕ включает _section_hint: «как дела?» чист
    d = _detectors("как дела?")
    assert d["v0"] is False and d["old"] is True  # old = интент-сигнал (section_hint), сравнительное


def test_confirm_turns_bucketed_separately():
    from scripts.analysis_285_baseline import compute

    rows = [
        _row("привет", [{"name": "add_task", "result_kind": "ok"}], confirm="confirmed"),
        _row("добавь молоко", [{"name": "add_task", "result_kind": "ok"}]),
    ]
    agg = compute(rows)
    assert agg["confirm_turns"] == 1 and agg["confirm_w_write"] == 1
    assert agg["fresh_w_write"] == 1  # confirm-ход НЕ в основной метрике (#269)
