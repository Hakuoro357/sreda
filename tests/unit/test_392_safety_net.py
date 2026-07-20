"""#392 страховка наблюдаемости autoexec: «крик-в-лог» на запись-при-чтении.

autoexec убрал confirm-сеть для аддитивных write. Если такой инструмент ИСПОЛНИЛСЯ на
ходе, чей запрос — ЧТЕНИЕ (read-cue есть, явной write-команды нет), это ПРОМАХ модели
(«читать→сделал запись»). Тогда: громкий warning-лог + admin-alert (P2, DB-dedup+burst,
дуал TG+MAX #395 — reuse send_admin_alert, НЕ новая подсистема). Цель: (а) видим промах
сразу, (б) измеряем частоту для решения «оставлять ли autoexec».

Сигнал «чтение» детерминирован: read_cue_domains непусто И НЕ write_command_signal
(континуация «Ещё добавь X» несёт императив → НЕ флаг; «покажи список» + молчаливый add →
флаг). Всё best-effort/PII-safe.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import (
    _executed_autoexec_writes,
    _maybe_alert_write_on_read,
)
from sreda.services.checklists import ChecklistService
from tests.unit.conftest import seed_telegram_user


def _ai(name: str, cid: str = "c1", args: dict | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid}])


def _tm(content: str, cid: str = "c1", *, kind: str = "ok") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=cid, artifact={"result_kind": kind})


def _turn(name: str, *, kind: str = "ok", human: str = "покажи список дел") -> list:
    return [HumanMessage(content=human), _ai(name), _tm(f"ok:{name}", kind=kind)]


# ─────────── детектор исполненных autoexec-write ───────────

def test_detect_executed_autoexec_write():
    """add_checklist_items (autoexec) успешно исполнен → в наборе."""
    assert _executed_autoexec_writes(_turn("add_checklist_items")) == frozenset({"add_checklist_items"})


def test_detect_ignores_non_autoexec_write():
    """delete_checklist_item (НЕ autoexec) исполнен → НЕ в наборе (детектор только по реестру)."""
    assert _executed_autoexec_writes(_turn("delete_checklist_item")) == frozenset()


def test_detect_ignores_failed_write():
    """autoexec-write с НЕ-ok результатом (отказ/ошибка) → не считается исполненным."""
    assert _executed_autoexec_writes(_turn("add_checklist_items", kind="error")) == frozenset()


def test_detect_scopes_to_current_turn():
    """Окно — после последнего HumanMessage: прошлый ход не учитывается."""
    msgs = [
        HumanMessage(content="старый"), _ai("add_checklist_items", "old"),
        _tm("ok", "old"),
        HumanMessage(content="покажи список дел"),  # новый ход — autoexec НЕ звался
    ]
    assert _executed_autoexec_writes(msgs) == frozenset()


# ─────────── алерт: запись-на-чтении ───────────

class _AlertSink:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


def _fire(monkeypatch, *, human: str, tool: str, kind: str = "ok"):
    sink = _AlertSink()
    import sreda.services.admin_alerts as aa
    monkeypatch.setattr(aa, "send_admin_alert", sink)
    _maybe_alert_write_on_read(
        tenant_id="t1", user_id="u1", channel="max", turn_key="tk1",
        user_text=human, messages=_turn(tool, kind=kind, human=human))
    return sink


def test_alert_fires_on_write_during_read(monkeypatch):
    """Ход-ЧТЕНИЕ «покажи список дел» + молчаливый autoexec add_checklist_items → алерт."""
    sink = _fire(monkeypatch, human="покажи список дел", tool="add_checklist_items")
    assert len(sink.calls) == 1
    c = sink.calls[0]
    assert c["severity"] == "P2"
    assert "add_checklist_items" in c["body"]
    assert c["dedupe_key"] == "autoexec_write_on_read:t1"
    # PII-safe: алерт в приватный админ-чат, но не сырой дамп — обрезка есть
    assert "t1" in c["body"]


def test_no_alert_when_write_command_present(monkeypatch):
    """Явная команда-мутация «добавь молоко в список покупок» → НЕ чтение → нет алерта
    (даже если read-cue сработал на слове «список»: write_command_signal главнее)."""
    sink = _fire(monkeypatch, human="добавь молоко в список покупок", tool="add_shopping_items")
    assert sink.calls == []


def test_no_alert_on_continuation_dictation(monkeypatch):
    """«Ещё добавь уголь» — императив без read-cue → легитимная диктовка, нет алерта."""
    sink = _fire(monkeypatch, human="Ещё добавь уголь", tool="add_checklist_items")
    assert sink.calls == []


def test_no_alert_when_no_autoexec_write(monkeypatch):
    """Ход-чтение, но исполнен деструктив (НЕ autoexec, был под confirm) → нет алерта."""
    sink = _fire(monkeypatch, human="покажи список дел", tool="delete_checklist_item")
    assert sink.calls == []


def test_no_alert_when_no_read_cue(monkeypatch):
    """Нет read-cue (декларатив/смолток «меня зовут Аня») → даже autoexec-запись НЕ флагается:
    рассогласование «читать→запись» требует ЯВНОГО read-запроса, не любого хода без write-команды."""
    sink = _fire(monkeypatch, human="меня зовут Аня", tool="add_task")
    assert sink.calls == []


def test_alert_best_effort_swallows_failure(monkeypatch):
    """Сбой send_admin_alert НЕ пробивается в ход (best-effort)."""
    def _boom(**k):
        raise RuntimeError("boom")
    import sreda.services.admin_alerts as aa
    monkeypatch.setattr(aa, "send_admin_alert", _boom)
    # не бросает
    _maybe_alert_write_on_read(
        tenant_id="t1", user_id="u1", channel="max", turn_key="tk1",
        user_text="покажи список дел", messages=_turn("add_checklist_items"))


# ─────────── e2e: проводка страховки через реальный handle_turn ───────────

class _StubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


class _TraceOn:
    """Стаб трейса с trace_enabled=True → finalization-блок handle_turn (где вызывается
    _maybe_alert_write_on_read) выполняется; прочие методы — no-op."""

    def trace_enabled(self):
        return True

    def collect_tool_calls(self, *a, **k):
        return []

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.mark.asyncio
async def test_e2e_alert_fires_through_handle_turn(monkeypatch, db_session):
    """Проводка «крик-в-лог»: РЕАЛЬНЫЙ handle_turn на ходе-ЧТЕНИИ «покажи список дел», где модель
    молча зовёт autoexec add_checklist_items (aw=∅) → add исполнен ПРЯМО (без confirm) И admin-alert
    СРАБОТАЛ ровно раз. Пинит, что страховка живёт на реальном пути, не только в юните функции."""
    import sreda.runtime.react_loop as rl
    import sreda.services.admin_alerts as aa
    sink = _AlertSink()
    monkeypatch.setattr(aa, "send_admin_alert", sink)
    monkeypatch.setattr(rl, "_trace", _TraceOn())
    u = seed_telegram_user(db_session)
    ChecklistService(db_session).create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Дача")
    db_session.commit()
    llm = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["уголь"]}, "id": "c1"}]),
        AIMessage(content="Готово."),
    ])
    await rl.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=llm, user_text="покажи список дел",
        inbound_message_id="m1", channel="max")
    assert len(sink.calls) == 1, sink.calls
    assert sink.calls[0]["dedupe_key"] == "autoexec_write_on_read:" + u.tenant_id
    assert "add_checklist_items" in sink.calls[0]["body"]
