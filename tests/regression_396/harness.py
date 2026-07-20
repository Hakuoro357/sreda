"""Прогон фикстуры через РЕАЛЬНЫЙ ``react_loop.handle_turn`` на свежей sqlite.

Детерминированно (без сети/модели/стоимости) — драйвер модели ``StubLLM``
скриптует ходы (паттерн ``_StubLLM`` из ``tests/unit/test_post_tool_reply_393.py``).
Захватывает по каждому ходу: реплику, паузу-подтверждение, дифф состояния БД
(before/after, паттерн ``db_counts`` из qwen_cycle_eval), tool-вызовы модели и
квитанции инструментов. Возвращает ``DialogOutcome`` — по нему судят инварианты.
"""
from __future__ import annotations

import itertools
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import handle_turn
from sreda.runtime.react_result_report import collect_successful_writes, reply_grounds_result
from sreda.services.tool_schemas.families import TOOL_OP_CLASS
from sreda.services.tool_schemas.tool_ok_codec import is_okv2

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
        self.invokes = 0            # сколько раз модель вызвана за ход
        self.overrun = 0            # проходов СВЕРХ скрипта (скрипт исчерпан) — скрытая петля
        # ToolMessage-квитанции, увиденные за ход (union по tool_call_id всех проходов).
        # Копим по всем invoke: финальный chat-проход инжектит служебный HumanMessage
        # ПОСЛЕ ToolMessage → «окно после последнего Human» теряет квитанцию; union устойчив.
        # tool_call_id уникален на весь диалог (см. ai_tool) → коллизий между ходами нет.
        self.tool_msgs: dict[str, ToolMessage] = {}

    def bind_tools(self, tools, *a, **k):  # noqa: ANN001, ARG002
        return self

    def invoke(self, messages, *a, **k):  # noqa: ANN001, ARG002
        self.captured = list(messages)
        self.invokes += 1
        for m in messages:
            if isinstance(m, ToolMessage):
                tid = getattr(m, "tool_call_id", None)
                if tid is not None:
                    self.tool_msgs.setdefault(tid, m)
        if self._i >= len(self._scripted):
            self.overrun += 1       # скрипт исчерпан, а модель зовут снова — фиксируем
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
    # R2 (idempotency, ревью sol+terra): пин inbound_message_id → тот же turn_key в
    # handle_turn → РЕАЛЬНЫЙ replay одного idempotency-ключа (не просто дедуп по имени).
    inbound_id: str | None = None


# ─────────────────────────── снимки состояния БД ───────────────────────────
# R2 (ревью sol+terra): ЛОГИЧЕСКИЙ снимок {таблица: {id: (status, title)}}, а не просто
# count. Ловит in-place мутации (archive/complete/rename), которые count не видит, и
# скоупит по tenant_id И user_id (иначе запись другому юзеру того же тенанта проходила бы
# по count). ChecklistItem без user_id → скоуп через checklist_id родителя юзера.


def state_snapshot(session: Any, tenant_id: str, user_id: str) -> dict[str, dict]:
    from sreda.db.models import (
        AssistantMemory, Checklist, ChecklistItem, FamilyReminder, ShoppingListItem, Task,
    )
    session.expire_all()

    def proj(model) -> dict[str, tuple]:
        q = session.query(model).filter_by(tenant_id=tenant_id)
        if hasattr(model, "user_id"):
            q = q.filter_by(user_id=user_id)
        return {r.id: (getattr(r, "status", None), getattr(r, "title", None)) for r in q.all()}

    checklists = proj(Checklist)
    cl_ids = set(checklists)
    if cl_ids:
        items = {r.id: (r.status, r.title) for r in session.query(ChecklistItem)
                 .filter(ChecklistItem.checklist_id.in_(cl_ids)).all()}
    else:
        items = {}
    return {"shopping": proj(ShoppingListItem), "reminders": proj(FamilyReminder),
            "tasks": proj(Task), "checklists": checklists, "checklist_items": items,
            "memories": {r.id: (None, None) for r in session.query(AssistantMemory)
                         .filter_by(tenant_id=tenant_id, user_id=user_id).all()}}


def counts_from_state(snap: dict[str, dict]) -> dict[str, int]:
    return {k: len(v) for k, v in snap.items()}


# ─────────────────────────── квитанции из истории ───────────────────────────


def _clean_history(user_text: str, ai: list[AIMessage], toolmsgs: list[ToolMessage]) -> list[Any]:
    """Чистая история одного хода для collect_successful_writes: единственный ведущий
    HumanMessage (юзер) + AI-проходы + квитанции. collect_* матчит ToolMessage по
    tool_call_id, порядок в окне не важен — важно, что и вызов, и квитанция в окне."""
    return [HumanMessage(content=user_text), *ai, *toolmsgs]


_REFUSAL_PREFIXES = ("Хорошо, не", "error:", "empty:", "not_found:", "ambiguous:")


def _okv2_added(content: str) -> tuple[int, int]:
    """(added_count, duplicate_count) из okv2-конверта; (-1,-1) если не okv2/битый."""
    if not is_okv2(content):
        return -1, -1
    try:
        import json
        payload = json.loads(content.split(":", 2)[2])
    except (IndexError, ValueError):
        return -1, -1
    if not isinstance(payload, dict):
        return -1, -1
    return int(payload.get("added_count", 0)), int(payload.get("duplicate_count", 0))


# R3 (ревью sol+terra R2): нулевой/no-op контракт — `ok:*:0` (added/cleared/updated 0),
# ok:noop, *unchanged*, *not_found* → эффекта НЕТ, не считать applied (иначе ложное «готово»
# после no-op остаётся зелёным). Кросс-проверка правды — снимок состояния (`mutated`).
_ZERO_EFFECT_RE = re.compile(r"(?::0\b)|(?:\bnoop\b)|unchanged|not_found", re.IGNORECASE)


def _classify_receipt(name: str, kind: str | None, content: str) -> str:
    """Статус квитанции per-receipt (R2, ревью sol+terra: НЕ по set имён — не путать
    успех одного вызова с упавшим одноимённым). applied / already_applied / failed /
    noop / read."""
    if TOOL_OP_CLASS.get(name) != "write":
        return "read"
    if kind not in (None, "ok") or content.startswith(_REFUSAL_PREFIXES):
        return "failed"
    if kind != "ok":
        return "noop"
    added, dup = _okv2_added(content)
    if added >= 0:                       # add-инструмент с okv2-контрактом
        if added > 0:
            return "applied"             # новые строки
        if dup > 0:
            return "already_applied"     # всё уже было (цель удовлетворена, не новая строка)
        return "noop"                    # ни добавлено, ни дубли → эффекта нет
    if _ZERO_EFFECT_RE.search(content):  # ok:updated:0 / ok:cleared:0 / not_found → no-op
        return "noop"
    # target-инструменты (archive/schedule…): success-префикс ok:archived / ok:scheduled …
    if content.startswith("ok:") and not content.startswith("ok:noop"):
        return "applied"
    return "noop"


def _build_receipts(toolmsgs: list[ToolMessage]) -> list[ToolReceipt]:
    out: list[ToolReceipt] = []
    for m in toolmsgs:
        name = getattr(m, "name", "") or ""
        art = getattr(m, "artifact", None)
        kind = art.get("result_kind") if isinstance(art, dict) else None
        content = str(getattr(m, "content", "") or "")
        out.append(ToolReceipt(name=name, result_kind=kind, content=content,
                               status=_classify_receipt(name, kind, content)))
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
    max_tool_calls: int | None = None   # потолок tool-вызовов за диалог (петли/лишние действия)
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
    overrun = 0
    for i, st in enumerate(fx.turns):
        state_before = state_snapshot(session, user.tenant_id, user.user_id)
        stub = StubLLM(st.ai)
        try:
            reply = await handle_turn(
                session=session, tenant_id=user.tenant_id, user_id=user.user_id,
                thread_id=thread, llm=stub, user_text=st.user_text,
                inbound_message_id=st.inbound_id or f"m{i}", channel=fx.channel)
        except Exception as e:  # noqa: BLE001 — handle_turn не должен кидать; страхуемся
            aborted = f"turn-crash: {type(e).__name__}: {e}"
            break
        state_after = state_snapshot(session, user.tenant_id, user.user_id)
        overrun += stub.overrun
        new_ids = [tid for tid in stub.tool_msgs if tid not in seen_tool_ids]
        seen_tool_ids.update(new_ids)
        new_toolmsgs = [stub.tool_msgs[tid] for tid in new_ids]
        clean = _clean_history(st.user_text, st.ai, new_toolmsgs)
        turns_out.append(TurnOutcome(
            user_text=st.user_text, is_auto=st.is_auto, reply=str(reply),
            awaiting_confirm=bool(getattr(reply, "awaiting_confirm", False)),
            db_before=counts_from_state(state_before), db_after=counts_from_state(state_after),
            state_before=state_before, state_after=state_after,
            tool_calls=list(stub.emitted_tool_calls),
            receipts=_build_receipts(new_toolmsgs), messages=clean))

    final_db: dict[str, Any] = {}
    if fx.expect_final is None:
        final_db = counts_from_state(state_snapshot(session, user.tenant_id, user.user_id))
    return DialogOutcome(fixture_id=fx.id, turns=turns_out, final_db=final_db,
                         aborted=aborted, overrun=overrun)


# ─────────────────────────── удобные конструкторы AIMessage ───────────────────────────


_CALL_SEQ = itertools.count()


def ai_tool(name: str, args: dict, cid: str | None = None) -> AIMessage:
    """AIMessage-проход с вызовом инструмента. ``cid`` УНИКАЛЕН по умолчанию (счётчик на
    def-time фикстур) — иначе повтор ``c1`` между ходами ронял бы квитанции поздних ходов
    (ревью sol+terra R1). Явный cid — только для мультиtool-хода, где нужен контроль."""
    if cid is None:
        cid = f"call_{next(_CALL_SEQ)}"
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
    if fx.max_tool_calls is not None and dialog.total_tool_calls > fx.max_tool_calls:
        problems.append(f"tool-вызовов {dialog.total_tool_calls} > потолка {fx.max_tool_calls}")
    if dialog.overrun:
        problems.append(f"модель вызвана {dialog.overrun}× СВЕРХ скрипта (петля/лишние проходы?)")

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
