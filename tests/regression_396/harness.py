"""Прогон фикстуры через РЕАЛЬНЫЙ ``react_loop.handle_turn`` на свежей sqlite.

Детерминированно (без сети/модели/стоимости) — драйвер модели ``StubLLM``
скриптует ходы (паттерн ``_StubLLM`` из ``tests/unit/test_post_tool_reply_393.py``).
Захватывает по каждому ходу: реплику, паузу-подтверждение, дифф состояния БД
(before/after, паттерн ``db_counts`` из qwen_cycle_eval), tool-вызовы модели и
квитанции инструментов. Возвращает ``DialogOutcome`` — по нему судят инварианты.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import handle_turn
from sreda.runtime.react_result_report import collect_successful_writes, reply_grounds_result
from sreda.services.tool_schemas.families import TOOL_OP_CLASS

from tests.unit.conftest import SeededTelegramUser

from .invariants import inv_ambiguous_cancel_zero_mutations, inv_idempotency, run_universal
from .model import DialogOutcome, ToolReceipt, TurnOutcome

# ─────────────────────────── детерминированный драйвер модели ───────────────────────────


class StubLLM:
    """Скриптованная модель: отдаёт заранее заданные ``AIMessage`` по порядку.

    Дакт-тайпинг под планировщик: ``bind_tools(tools)→self``, ``invoke(messages)→next``.
    Записывает ВСЕ вызванные tool_calls (для потолка) и захватывает входящие
    сообщения последнего прохода (там аккумулированы ToolMessage-квитанции)."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = scripted
        self._i = 0
        self.emitted_tool_calls: list[str] = []
        self.captured: list[Any] = []
        # ToolMessage-квитанции, увиденные за ход (union по tool_call_id всех проходов).
        # Копим по всем invoke: финальный chat-проход инжектит служебный HumanMessage
        # ПОСЛЕ ToolMessage → «окно после последнего Human» теряет квитанцию; union устойчив.
        self.tool_msgs: dict[str, ToolMessage] = {}

    def bind_tools(self, tools, *a, **k):  # noqa: ANN001, ARG002
        return self

    def invoke(self, messages, *a, **k):  # noqa: ANN001, ARG002
        self.captured = list(messages)
        for m in messages:
            if isinstance(m, ToolMessage):
                tid = getattr(m, "tool_call_id", None)
                if tid is not None:
                    self.tool_msgs.setdefault(tid, m)
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        for tc in (getattr(msg, "tool_calls", None) or []):
            self.emitted_tool_calls.append(tc.get("name"))
        return msg

    async def ainvoke(self, messages, *a, **k):  # noqa: ANN001
        return self.invoke(messages, *a, **k)


@dataclass
class ScriptedTurn:
    """Один ход диалога: текст юзера + что модель эмитит (проходы tool-call + финал)."""

    user_text: str
    ai: list[AIMessage]
    is_auto: bool = False   # авто-ответ на confirm («да»/«нет») — для учёта


# ─────────────────────────── снимки состояния БД ───────────────────────────

# Таблица -> ORM-модель. Паттерн db_counts из qwen_cycle_eval; фильтр по тенанту.
def _count_models() -> dict[str, Any]:
    from sreda.db.models import (
        AssistantMemory, Checklist, ChecklistItem, FamilyReminder, ShoppingListItem, Task,
    )
    return {"shopping": ShoppingListItem, "reminders": FamilyReminder, "tasks": Task,
            "checklists": Checklist, "checklist_items": ChecklistItem, "memories": AssistantMemory}


def db_counts(session: Any, tenant_id: str) -> dict[str, int]:
    session.expire_all()
    out: dict[str, int] = {}
    for name, model in _count_models().items():
        q = session.query(model)
        if hasattr(model, "tenant_id"):
            q = q.filter_by(tenant_id=tenant_id)
        out[name] = int(q.count())
    return out


# ─────────────────────────── квитанции из истории ───────────────────────────


def _clean_history(user_text: str, ai: list[AIMessage], toolmsgs: list[ToolMessage]) -> list[Any]:
    """Чистая история одного хода для collect_successful_writes: единственный ведущий
    HumanMessage (юзер) + AI-проходы + квитанции. collect_* матчит ToolMessage по
    tool_call_id, порядок в окне не важен — важно, что и вызов, и квитанция в окне."""
    return [HumanMessage(content=user_text), *ai, *toolmsgs]


def _build_receipts(toolmsgs: list[ToolMessage], applied_tools: set[str]) -> list[ToolReceipt]:
    """Квитанции хода. ``applied`` — доказанный успешный write (collect_successful_writes —
    авторитетная логика #393) ИЛИ ok-квитанция write-инструмента без отказ/ошибка-префикса."""
    out: list[ToolReceipt] = []
    for m in toolmsgs:
        name = getattr(m, "name", "") or ""
        art = getattr(m, "artifact", None)
        kind = art.get("result_kind") if isinstance(art, dict) else None
        content = str(getattr(m, "content", "") or "")
        applied = (name in applied_tools) or (
            kind == "ok" and TOOL_OP_CLASS.get(name) == "write"
            and not content.startswith(("error:", "Хорошо, не")))
        out.append(ToolReceipt(name=name, result_kind=kind, content=content, applied=applied))
    return out


# ─────────────────────────── прогон фикстуры ───────────────────────────


@dataclass
class Fixture:
    """Фикстура одного бага: начальное состояние БД + скрипт диалога + ожидания."""

    id: str
    bug: str
    summary: str
    turns: list[ScriptedTurn]
    seed: Callable[[Any, SeededTelegramUser], None] = lambda s, u: None
    channel: str = "max"
    # декларативные ожидания (все опциональны) — проверяются в тесте фикстур:
    expect_mutations: dict[str, int] = field(default_factory=dict)   # чистая дельта за диалог (точно)
    forbid_mutations: list[str] = field(default_factory=list)         # эти таблицы: чистая дельта = 0
    ground_result_turns: list[int] = field(default_factory=list)      # #393: реплика называет результат
    forbid_phrases: list[str] = field(default_factory=list)           # ни в одной реплике (ci-substr)
    require_phrases: list[tuple[int, str]] = field(default_factory=list)  # (ход, substr) в реплике
    max_confirms: int | None = None
    idempotent_group: tuple[list[int], str] | None = None
    ambiguous_cancel_turns: list[int] = field(default_factory=list)
    # #390: сырой ВЫВОД инструмента (квитанция) — источник техвыдачи, НЕЗАВИСИМ от скрипта
    # реплики. Красный на текущем main (англ. pending/done/total), зелёный когда инструмент
    # чинят на русский формат → честная режущая способность по РЕАЛЬНОМУ коду, не по скрипту.
    require_clean_receipts: bool = False
    expect_final: Callable[[Any, SeededTelegramUser], list[str]] | None = None


async def run_fixture(fx: Fixture, session: Any, user: SeededTelegramUser) -> DialogOutcome:
    """Засеять начальное состояние, проиграть скрипт через handle_turn, собрать исход."""
    fx.seed(session, user)
    session.commit()

    thread = f"react:t:{uuid.uuid4().hex}"
    turns_out: list[TurnOutcome] = []
    seen_tool_ids: set[str] = set()   # квитанция принадлежит ходу первого её появления
    aborted = ""
    for st in fx.turns:
        before = db_counts(session, user.tenant_id)
        stub = StubLLM(st.ai)
        try:
            reply = await handle_turn(
                session=session, tenant_id=user.tenant_id, user_id=user.user_id,
                thread_id=thread, llm=stub, user_text=st.user_text,
                inbound_message_id=f"m{len(turns_out)}", channel=fx.channel)
        except Exception as e:  # noqa: BLE001 — handle_turn не должен кидать; страхуемся
            aborted = f"turn-crash: {type(e).__name__}: {e}"
            break
        after = db_counts(session, user.tenant_id)
        new_ids = [tid for tid in stub.tool_msgs if tid not in seen_tool_ids]
        seen_tool_ids.update(new_ids)
        new_toolmsgs = [stub.tool_msgs[tid] for tid in new_ids]
        clean = _clean_history(st.user_text, st.ai, new_toolmsgs)
        applied_tools = {a.tool for a in collect_successful_writes(clean)}
        turns_out.append(TurnOutcome(
            user_text=st.user_text, is_auto=st.is_auto, reply=str(reply),
            awaiting_confirm=bool(getattr(reply, "awaiting_confirm", False)),
            db_before=before, db_after=after,
            tool_calls=list(stub.emitted_tool_calls),
            receipts=_build_receipts(new_toolmsgs, applied_tools), messages=clean))

    final_db: dict[str, Any] = {}
    if fx.expect_final is None:
        final_db = db_counts(session, user.tenant_id)
    return DialogOutcome(fixture_id=fx.id, turns=turns_out, final_db=final_db, aborted=aborted)


# ─────────────────────────── удобные конструкторы AIMessage ───────────────────────────


def ai_tool(name: str, args: dict, cid: str = "c1") -> AIMessage:
    """AIMessage-проход с вызовом инструмента."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def ai_text(text: str) -> AIMessage:
    """AIMessage-проход с финальным текстом (без инструментов)."""
    return AIMessage(content=text)


# ─────────────────────────── проверка ожиданий фикстуры ───────────────────────────


def _net_diff(dialog: DialogOutcome) -> dict[str, int]:
    """Чистая дельта состояния БД за весь диалог (сумма ходовых db_diff)."""
    net: dict[str, int] = {}
    for t in dialog.turns:
        for k, v in t.db_diff.items():
            net[k] = net.get(k, 0) + v
    return {k: v for k, v in net.items() if v != 0}


def check_fixture(dialog: DialogOutcome, fx: Fixture, session: Any,
                  user: SeededTelegramUser) -> list[str]:
    """Универсальные инварианты + декларативные ожидания фикстуры. Пусто = зелёная."""
    problems: list[str] = []
    if dialog.aborted:
        problems.append(f"aborted: {dialog.aborted}")
    problems += [str(v) for v in run_universal(dialog)]

    net = _net_diff(dialog)
    for tbl, exp in fx.expect_mutations.items():
        if net.get(tbl, 0) != exp:
            problems.append(f"expect_mutations[{tbl}]={exp}, факт {net.get(tbl, 0)} (net={net})")
    for tbl in fx.forbid_mutations:
        if net.get(tbl, 0) != 0:
            problems.append(f"forbid_mutations[{tbl}] изменилась на {net.get(tbl, 0)}")

    for i in fx.ground_result_turns:
        acts = collect_successful_writes(dialog.turns[i].messages)
        if not reply_grounds_result(dialog.turns[i].reply, acts):
            problems.append(f"ход#{i}: реплика не называет результат (#393): {dialog.turns[i].reply!r}")

    for ph in fx.forbid_phrases:
        for i, t in enumerate(dialog.turns):
            if ph.lower() in (t.reply or "").lower():
                problems.append(f"ход#{i}: запрещённая фраза {ph!r}: {t.reply!r}")
    for i, ph in fx.require_phrases:
        if ph.lower() not in (dialog.turns[i].reply or "").lower():
            problems.append(f"ход#{i}: нет обязательной подстроки {ph!r}: {dialog.turns[i].reply!r}")

    if fx.max_confirms is not None and dialog.confirm_count > fx.max_confirms:
        problems.append(f"confirm-пауз {dialog.confirm_count} > потолка {fx.max_confirms}")

    if fx.require_clean_receipts:
        from .invariants import tech_tokens
        for i, t in enumerate(dialog.turns):
            for r in t.receipts:
                toks = tech_tokens(r.content)
                if toks:
                    problems.append(
                        f"ход#{i}: сырой вывод инструмента «{r.name}» несёт техвыдачу {toks} "
                        f"(источник #390) — модель пересказывает как есть")

    if fx.idempotent_group:
        turns, tbl = fx.idempotent_group
        problems += [str(v) for v in inv_idempotency(dialog, turns=turns, table=tbl)]
    if fx.ambiguous_cancel_turns:
        problems += [str(v) for v in
                     inv_ambiguous_cancel_zero_mutations(dialog, turns=fx.ambiguous_cancel_turns)]

    if fx.expect_final is not None:
        problems += fx.expect_final(session, user)
    return problems
