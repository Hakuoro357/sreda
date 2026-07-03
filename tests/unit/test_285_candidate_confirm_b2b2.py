"""#285 Фаза B срез B2b-2: ярус (б) — candidate-write + универсальный confirm.

Пилляр 1(б): на едином пути write ВНЕ allowed_write НЕ отказывает, а биндится кандидатом под
generic confirm (превью без БД). Молчаливой мутации нет; unsignaled write = подтверждение.
Проверяем: _apply_unified_policy (обёртка/фильтр) + _generic_confirm_wrap (confirm-поведение).
"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop
from sreda.runtime.react_loop import (
    _apply_unified_policy, _generic_confirm_wrap, _tool_unavailable_reason,
)


def _tool(name):
    def _f(q: str = "", **kw):
        return f"{name}-done"
    return StructuredTool.from_function(func=_f, name=name, description=name)


# ─────────── _apply_unified_policy: ярусы (а)/(б) + read-гейт ───────────
def test_signaled_write_direct_no_wrap():
    """write-инструмент с доменом в allowed_write → прямой (тот же объект, без confirm-обёртки)."""
    t = _tool("add_task")  # write / tasks
    out = _apply_unified_policy([t], allowed_read=["tasks", "web"], allowed_write=["tasks"])
    assert out == [t]  # identity: не обёрнут


def test_unsignaled_write_becomes_candidate():
    """write вне allowed_write → КАНДИДАТ (обёрнут, НЕ отфильтрован). Имя сохранено (LLM зовёт прозрачно)."""
    t = _tool("add_task")
    out = _apply_unified_policy([t], allowed_read=["web"], allowed_write=[])
    assert len(out) == 1
    assert out[0] is not t              # обёрнут
    assert out[0].name == "add_task"    # имя сохранено


def test_read_tool_gated_by_allowed_read():
    """read-инструмент вне allowed_read → отфильтрован (own-data не открывается кандидатом)."""
    lr = _tool("list_reminders")  # read / reminders
    assert _apply_unified_policy([lr], allowed_read=["web"], allowed_write=[]) == []
    assert _apply_unified_policy([lr], allowed_read=["reminders"], allowed_write=[]) == [lr]


def test_web_always_passes_baseline():
    ws = _tool("web_search")  # read / web
    assert _apply_unified_policy([ws], allowed_read=["web"], allowed_write=[]) == [ws]


def test_meta_ask_human_need_family_pass():
    for nm in ("ask_human", "need_family"):
        t = _tool(nm)
        assert _apply_unified_policy([t], allowed_read=["web"], allowed_write=[]) == [t]


def test_delete_my_account_gated_by_signal():
    """B2 CodexH/M CRITICAL: delete_my_account НЕ биндится без сигнала удаления аккаунта (иначе его
    pre-confirm audit-запись достижима на смолтоке). С сигналом → биндится (свой A11-confirm)."""
    d = _tool("delete_my_account")
    # без сигнала → не бинд (недостижим)
    assert _apply_unified_policy([d], allowed_read=["web"], allowed_write=[], allow_account_delete=False) == []
    # с сигналом → бинд
    assert _apply_unified_policy([d], allowed_read=["web"], allowed_write=[], allow_account_delete=True) == [d]


def test_account_deletion_signal_precision():
    from sreda.runtime.react_signals import account_deletion_signal as a
    for t in ("удали мой аккаунт", "снеси мой профиль", "удали мою учётку",
              "уничтожь мой аккаунт", "сотри мой профиль"):
        assert a(t) is True, t
    # негация/вопрос/цитата/мета/чужой/безличн./деliberative (B2 R2-R4) + не-аккаунт; ТОЛЬКО императив+притяж.
    for t in ("удали задачу", "добавь молоко", "как дела?", "удали напоминание про врача",
              "не удаляй мой аккаунт", "как удалить аккаунт?", 'команда "удали мой аккаунт" не работает',
              "он сказал удали свой аккаунт",
              "удали аккаунт в инстаграме", "как удалить аккаунт в телеграме",
              "надо удалить аккаунт в инстаграме", "надо удалить аккаунт",
              "надо удалить свой аккаунт?", "хочу удалить свой аккаунт"):  # R4: вопрос/деliberative
        assert a(t) is False, t


def test_direct_write_requires_read_domain_too():
    """B2 CodexH R2 MAJOR: write-инструмент с read≠write доменом (write=shopping/read=menu) на
    shopping-ходе НЕ прямой (читал бы menu own-data), а кандидат под confirm."""
    t = _tool("generate_shopping_from_menu")  # write=shopping, read=menu (override)
    # write разрешён (shopping), но read (menu) НЕ в allowed_read → кандидат (обёрнут)
    out = _apply_unified_policy([t], allowed_read=["web", "shopping"], allowed_write=["shopping"])
    assert len(out) == 1 and out[0] is not t  # обёрнут (кандидат), не прямой
    # если И read-домен разрешён → прямой
    out2 = _apply_unified_policy([t], allowed_read=["web", "menu", "shopping"], allowed_write=["shopping"])
    assert out2 == [t]  # прямой


def test_unknown_tool_fail_closed():
    x = _tool("totally_unknown_tool_xyz")
    assert _apply_unified_policy([x], allowed_read=["web"], allowed_write=["tasks"]) == []


def test_none_policy_noop():
    t = _tool("add_task")
    assert _apply_unified_policy([t], None, None) == [t]


def test_multiple_writes_each_wrapped_no_blanket():
    """blanket-unlock невозможен: КАЖДЫЙ candidate-write обёрнут отдельно (не один флаг «всё разрешено»)."""
    a, b = _tool("add_task"), _tool("schedule_reminder")
    out = _apply_unified_policy([a, b], allowed_read=["web"], allowed_write=[])
    assert len(out) == 2
    assert all(o is not orig for o, orig in zip(out, (a, b)))  # оба обёрнуты


# ─────────── _generic_confirm_wrap: мутация только после «да», превью без БД ───────────
def test_confirm_yes_executes(monkeypatch):
    calls = {}

    def _inner_f(x=""):
        calls["ran"] = x
        return "исполнено"
    inner = StructuredTool.from_function(func=_inner_f, name="add_task", description="d")
    monkeypatch.setattr(react_loop, "interrupt", lambda payload: "да")
    res = _generic_confirm_wrap(inner).invoke({"x": "молоко"})
    assert res == "исполнено" and calls.get("ran") == "молоко"


def test_confirm_no_does_not_execute(monkeypatch):
    calls = {}

    def _inner_f(x=""):
        calls["ran"] = True
        return "исполнено"
    inner = StructuredTool.from_function(func=_inner_f, name="add_task", description="d")
    monkeypatch.setattr(react_loop, "interrupt", lambda payload: "нет")
    res = _generic_confirm_wrap(inner).invoke({"x": "молоко"})
    assert "не делаю" in res and "ran" not in calls  # мутация НЕ произошла


def test_confirm_preview_no_db_and_has_name(monkeypatch):
    """Превью GENERIC: имя+аргументы, без чтения БД (пилляр — превью не читает own-data до «да»)."""
    seen = {}
    inner = StructuredTool.from_function(func=lambda x="": "ok", name="add_shopping_items", description="d")
    monkeypatch.setattr(react_loop, "interrupt",
                        lambda payload: seen.update(payload) or "нет")
    _generic_confirm_wrap(inner).invoke({"x": "молоко"})
    assert "add_shopping_items" in seen["confirm"]  # имя в превью
    assert "key" in seen  # анти-stale-tap ключ


# ─────────── need_family: единый путь грузит любую семью (кандидат) ───────────
def test_need_family_unified_loads_any_family():
    """На едином пути write-семья вне allowed_write грузится (кандидат), не domain_blocked."""
    # shopping вне allowed_write, но unified → available (per-tool гейт защитит)
    r = _tool_unavailable_reason("need_family", {"family": "shopping"},
                                 allowed_read=["web"], allowed_write=[], unified=True)
    assert r == "available"
    # без unified — domain_blocked (legacy #221)
    r2 = _tool_unavailable_reason("need_family", {"family": "shopping"},
                                  allowed_read=["web"], allowed_write=[], unified=False)
    assert r2 == "domain_blocked"
