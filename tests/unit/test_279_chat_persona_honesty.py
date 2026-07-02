# -*- coding: utf-8 -*-
"""#279 (B2, кейс user_tg_1404009027): web-персона chat/fact НЕ должна отрицать
способности Среды (напоминания/задачи/списки/память).

Инцидент: новый юзер попросил напоминание, ход ушёл на chat/fact (web-only), и
`chat_fact_system_prompt` строкой «Других инструментов сейчас нет — не обещай
напоминаний/задач/списков» заставил модель ЗАЯВИТЬ о постоянной неспособности +
извиниться за «ложное обещание». Голосовой паритет: агент не отрицает способность,
которая есть в task-пути; вместо отказа — переспрашивает намерение.
"""
from __future__ import annotations

from sreda.runtime.react_preflight import chat_fact_system_prompt


def test_chat_fact_prompt_does_not_deny_capability_279():
    sp = chat_fact_system_prompt("2026-07-01")

    # СТАРЫЙ текст, породивший конфабуляцию, — не должен присутствовать
    assert "Других инструментов сейчас нет" not in sp
    assert "не обещай напоминаний" not in sp

    # honesty-floor: не отрицать способность (устойчивые маркеры-стемы, не точные фразы)
    assert "способност" in sp            # «способность есть»
    assert "не умеешь" in sp             # запрет говорить «не умею»

    # анти-over-promise (контракт из ревью R1, оба Codex MAJOR): не утверждать,
    # что уже/сейчас сделала — сиблинг-регресс к исходной конфабуляции
    assert "не утверждай" in sp
    assert "уже сделала" in sp

    # инварианты не сломаны: web-only scope цел, tool-имён productivity в промпте нет
    assert "web_search" in sp
    for tn in ("schedule_reminder", "need_family", "add_task", "list_reminders"):
        assert tn not in sp, tn
