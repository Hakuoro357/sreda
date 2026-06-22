"""#187 Phase 4b-2 — self-delete ReAct tool (owner-only, single-user).

RED-first (TDD) tests for the acceptance-checklist items this phase covers:

- **A7 (self-delete owner-only / single-user / anti-injection / confirm)** —
  a user can ask «удали меня»; the ReAct tool `delete_my_account`:
    * single-user tenant + actor IS that single user + confirmed → flips the
      flag via `soft_delete_tenant` (tenant becomes inactive);
    * multi-user tenant (2+ users) → reject WITHOUT deleting (outcome about
      contacting the admin);
    * `tenant_id` / actor `user_id` come from the TURN CONTEXT (closure-bound
      in `build_slice_tools`), NOT from an LLM argument — a forwarded «удали
      меня» / prompt-injection naming a foreign tenant must not target it;
    * destructive → standard confirm (first call asks, confirmed deletes,
      idempotent on repeat).

- **A11 (audit self-delete)** — the action writes both canonical audit rows:
    * `user.self_delete.requested` (on request / before the actual delete);
    * `user.self_delete.completed` (on confirmed delete, inside
      `soft_delete_tenant` via `audit_action`).

Verified against current code:
- bespoke ReAct tools = closures over (session, tenant_id, user_id) in
  `react_loop.build_slice_tools` (closure-bound tenant/user — runtime/react_loop.py:746);
- destructive confirm = `interrupt({"confirm": ..., "key": ...})` then
  `_is_yes(str(decision))` (e.g. cancel_reminder, react_loop.py:833);
- `soft_delete_tenant(session, tid, *, actor_type, actor_id, source,
  reason, audit_action)` → tenant_lifecycle.py:258; idempotent on
  `deleted_at`, writes the `audit_action` row inside the same tx.
- canonical audit keys: plan line 77 (`user.self_delete.requested/completed`);
  valid actor_type "user" → services/audit.py:41.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from sreda.db.models.audit import AuditLog
from sreda.db.models.core import User
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.runtime.react_loop import build_slice_tools, handle_turn
from sreda.services.tenant_lifecycle import is_tenant_active, restore_tenant
from tests.unit.conftest import seed_telegram_user


_TOOL = "delete_my_account"


class _StubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


def _delete_script(args: dict | None = None) -> _StubLLM:
    """LLM scripts a single delete_my_account call, then a final ack."""
    return _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": _TOOL, "args": args or {}, "id": "call_1"}]),
        AIMessage(content="Хорошо."),
    ])


def _ctx(u, tool: str = _TOOL) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        operation_id="o", execution_id="e", step_id="s",
        tool_name=tool, tenant_id=u.tenant_id, user_id=u.user_id, turn_key="tk")


def _tools(db_session, u):
    return {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}


def _audit_rows(db_session, action: str) -> list[AuditLog]:
    return (db_session.query(AuditLog)
            .filter(AuditLog.action == action)
            .order_by(AuditLog.created_at).all())


# ---------------------------------------------------------------------------
# Tool is registered & bound (core, always available)
# ---------------------------------------------------------------------------


def test_self_delete_tool_registered(db_session):
    """The self-delete tool is part of the ReAct tool set (always bound)."""
    u = seed_telegram_user(db_session); db_session.commit()
    assert _TOOL in _tools(db_session, u), "delete_my_account must be registered"


# ---------------------------------------------------------------------------
# A7 — single-user owner, confirmed → deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_delete_single_user_confirmed_deletes(db_session):
    """A7: single-user tenant, actor is that user, confirmed → tenant deleted."""
    u = seed_telegram_user(db_session); db_session.commit()
    assert is_tenant_active(db_session, u.tenant_id) is True
    thread = f"react:t:{uuid4().hex}"

    r1 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is True, (
        "до подтверждения тенант не должен удаляться")
    assert getattr(r1, "awaiting_confirm", False) is True, repr(r1)

    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is False, (
        "после «да» тенант должен быть удалён")


@pytest.mark.asyncio
async def test_self_delete_completed_audit_actor_user(db_session):
    """A11 + A7: confirmed self-delete writes the `completed` audit with
    actor_type=user / actor_id=user_id (via soft_delete_tenant audit_action)."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"

    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()

    rows = _audit_rows(db_session, "user.self_delete.completed")
    assert len(rows) == 1, f"ровно одна completed-строка ожидается: {rows}"
    row = rows[0]
    assert row.actor_type == "user"
    assert row.actor_id == u.user_id
    assert row.resource_id == u.tenant_id
    md = json.loads(row.metadata_json or "{}")
    assert md.get("source") == "self_service", md
    # requested-аудит дедуплицирован: узел tools перевыполняется на resume, но строка
    # requested пишется РОВНО ОДИН раз (within-tenant дедуп), не дублируется.
    assert len(_audit_rows(db_session, "user.self_delete.requested")) == 1, (
        "requested-аудит должен записаться ровно один раз (дедуп на resume)")


# ---------------------------------------------------------------------------
# A7 — multi-user tenant → reject, NOT deleted
# ---------------------------------------------------------------------------


def test_self_delete_multi_user_rejects(db_session):
    """A7: tenant with 2+ users → tool rejects (admin message), NOT deleted."""
    u = seed_telegram_user(db_session)
    # second user in the SAME tenant
    db_session.add(User(id="user_2", tenant_id=u.tenant_id, telegram_account_id="222"))
    db_session.commit()

    tool = _tools(db_session, u)[_TOOL]
    with bind_tool_runtime(_ctx(u)):
        out = tool.invoke({})
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is True, (
        "мульти-юзер тенант НЕ должен удаляться")
    assert "админ" in out.lower(), f"исход должен указывать на админа: {out!r}"


def test_self_delete_multi_user_no_audit(db_session):
    """A7: multi-user reject writes NEITHER requested NOR completed audit
    (no real action happened)."""
    u = seed_telegram_user(db_session)
    db_session.add(User(id="user_2", tenant_id=u.tenant_id, telegram_account_id="222"))
    db_session.commit()

    tool = _tools(db_session, u)[_TOOL]
    with bind_tool_runtime(_ctx(u)):
        tool.invoke({})
    db_session.expire_all()
    assert _audit_rows(db_session, "user.self_delete.completed") == []
    assert _audit_rows(db_session, "user.self_delete.requested") == []


# ---------------------------------------------------------------------------
# A7 — anti-injection: tenant/user from context, not from LLM arg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_delete_anti_injection_uses_context_tenant(db_session):
    """A7 anti-injection: even if a foreign tenant_id is injected as an LLM
    argument, at the confirm-pause (before «да») the requested-audit names the
    CONTEXT (actor) tenant — never the victim — and the victim stays active."""
    victim = seed_telegram_user(
        db_session, tenant_id="tenant_victim", chat_id="900900",
        user_id="user_victim", profile=False)
    actor = seed_telegram_user(
        db_session, tenant_id="tenant_actor", chat_id="100100",
        user_id="user_actor", profile=False)
    db_session.commit()

    thread = f"react:t:{uuid4().hex}"
    inj = {"tenant_id": victim.tenant_id, "user_id": victim.user_id}
    # ход1: модель инжектит чужой тенант в аргументы → пауза подтверждения для actor
    r1 = await handle_turn(
        session=db_session, tenant_id=actor.tenant_id, user_id=actor.user_id,
        thread_id=thread, llm=_delete_script(inj),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    db_session.expire_all()
    assert getattr(r1, "awaiting_confirm", False) is True, repr(r1)
    # никто не удалён (ещё не подтверждено)
    assert is_tenant_active(db_session, victim.tenant_id) is True
    assert is_tenant_active(db_session, actor.tenant_id) is True
    # requested-аудит ссылается ТОЛЬКО на контекстный (actor) тенант, не на чужой
    rows = _audit_rows(db_session, "user.self_delete.requested")
    assert rows, "requested-аудит должен записаться при запросе"
    for r in rows:
        assert r.resource_id == actor.tenant_id, (
            f"requested-аудит должен ссылаться на контекстный тенант: {r.resource_id}")
        assert r.actor_id == actor.user_id


@pytest.mark.asyncio
async def test_self_delete_anti_injection_e2e_context_tenant_deleted(db_session):
    """A7 anti-injection (e2e): with a foreign tenant injected in args, the
    CONTEXT tenant is the one that gets deleted on «да», foreign stays active."""
    victim = seed_telegram_user(
        db_session, tenant_id="tenant_victim2", chat_id="900901",
        user_id="user_victim2", profile=False)
    actor = seed_telegram_user(
        db_session, tenant_id="tenant_actor2", chat_id="100101",
        user_id="user_actor2", profile=False)
    db_session.commit()

    thread = f"react:t:{uuid4().hex}"
    inj = {"tenant_id": victim.tenant_id, "user_id": victim.user_id}

    await handle_turn(
        session=db_session, tenant_id=actor.tenant_id, user_id=actor.user_id,
        thread_id=thread, llm=_delete_script(inj),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=actor.tenant_id, user_id=actor.user_id,
        thread_id=thread, llm=_delete_script(inj),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()

    assert is_tenant_active(db_session, actor.tenant_id) is False, (
        "контекстный (actor) тенант должен быть удалён")
    assert is_tenant_active(db_session, victim.tenant_id) is True, (
        "чужой (victim) тенант должен остаться активным")


# ---------------------------------------------------------------------------
# confirm — first call asks, confirmed deletes, «нет» keeps, idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_delete_first_call_does_not_delete(db_session):
    """confirm: first call (no «да») → asks confirmation, does NOT delete."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"

    r1 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is True, (
        "первый вызов (без подтверждения) не должен удалять")
    assert getattr(r1, "awaiting_confirm", False) is True, repr(r1)


@pytest.mark.asyncio
async def test_self_delete_no_keeps(db_session):
    """confirm: «нет» → NOT deleted (fail-closed)."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"

    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="нет", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is True, "«нет» не должно удалять"


@pytest.mark.asyncio
async def test_self_delete_confirmed_emits_single_completed_audit(db_session):
    """confirm/idempotency: a completed self-delete writes exactly ONE
    completed-audit row (soft_delete_tenant is no-op-idempotent on deleted_at —
    the tool does not double-emit)."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"

    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert len(_audit_rows(db_session, "user.self_delete.completed")) == 1


# ---------------------------------------------------------------------------
# A11 — requested audit is written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_delete_requested_audit_written(db_session):
    """A11: a self-delete request writes the `user.self_delete.requested`
    audit (actor_type=user, resource=tenant)."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"

    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    db_session.expire_all()

    rows = _audit_rows(db_session, "user.self_delete.requested")
    assert len(rows) >= 1, "requested-аудит должен записаться при запросе"
    row = rows[0]
    assert row.actor_type == "user"
    assert row.actor_id == u.user_id
    assert row.resource_id == u.tenant_id


@pytest.mark.asyncio
async def test_self_delete_both_audits_present_after_completion(db_session):
    """A11: after a completed self-delete BOTH requested + completed rows
    exist."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"

    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_delete_script(),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert len(_audit_rows(db_session, "user.self_delete.requested")) >= 1
    assert len(_audit_rows(db_session, "user.self_delete.completed")) == 1


# ---------------------------------------------------------------------------
# FIX 1 (R1 MAJOR) — authz: actor must BE the sole user, not just count==1
# ---------------------------------------------------------------------------


def test_self_delete_mismatched_closure_actor_rejects(db_session):
    """FIX 1: tenant HAS exactly one user, but the closure-bound actor_id is
    NOT that user (binding error / wrong actor) → reject WITHOUT deleting and
    WITHOUT audit. count==1 alone is insufficient: the sole row must BE actor."""
    u = seed_telegram_user(db_session); db_session.commit()
    # single-user tenant (only `u`), but bind a DIFFERENT actor_id in context
    # → must be rejected (actor is not the sole owner of this tenant).
    wrong_user_id = "user_not_owner"
    assert wrong_user_id != u.user_id

    tools = {t.name: t for t in build_slice_tools(
        db_session, u.tenant_id, wrong_user_id)}
    tool = tools[_TOOL]
    ctx = ToolRuntimeContext(
        operation_id="o", execution_id="e", step_id="s",
        tool_name=_TOOL, tenant_id=u.tenant_id, user_id=wrong_user_id,
        turn_key="tk")
    with bind_tool_runtime(ctx):
        out = tool.invoke({})
    db_session.expire_all()

    assert is_tenant_active(db_session, u.tenant_id) is True, (
        "тенант жертвы НЕ должен удаляться при чужом actor_id")
    assert "админ" in out.lower(), f"исход должен указывать на админа: {out!r}"
    # никакого аудита — реального действия не было
    assert _audit_rows(db_session, "user.self_delete.requested") == []
    assert _audit_rows(db_session, "user.self_delete.completed") == []


# ---------------------------------------------------------------------------
# FIX 2 (R1 MAJOR) — requested-audit is within-turn dedup, NOT forever:
# restore → re-request must yield a NEW requested row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_delete_restore_rerequest_new_requested_audit(db_session):
    """FIX 2: delete → admin-restore → delete again ⇒ requested == 2.

    A forever-dedup (action+resource+actor over all history) would block the
    SECOND request's `requested` row (the old one already exists), breaking the
    requested/completed pairing on A11. Within-turn dedup keys on the per-turn
    operation_id → each real user request (new turn) is audited exactly once."""
    u = seed_telegram_user(db_session); db_session.commit()

    # Each delete cycle uses a SEPARATE thread (a real second request after a
    # restore arrives as a fresh conversation turn — distinct inbound id →
    # distinct turn_key → distinct within-turn operation_id). Same thread would
    # accumulate the prior turn's confirm-pause + tool history and entangle the
    # two cycles (the second request would resume the first pause), which is a
    # checkpoint artifact, not the dedup behavior under test.

    # cycle 1: delete (m1 → confirm m2)
    thread1 = f"react:t:{uuid4().hex}"
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread1, llm=_delete_script(),
        user_text="удали меня", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread1, llm=_delete_script(),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is False
    assert len(_audit_rows(db_session, "user.self_delete.requested")) == 1, (
        "первый цикл: requested ровно один (within-turn дедуп на resume)")

    # admin restores the tenant
    restore_tenant(
        db_session, u.tenant_id, actor_type="admin", actor_id="admin_x")
    db_session.expire_all()
    assert is_tenant_active(db_session, u.tenant_id) is True

    # cycle 2: NEW request → NEW thread/turn (m3 → confirm m4) → NEW requested row
    thread2 = f"react:t:{uuid4().hex}"
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread2, llm=_delete_script(),
        user_text="удали меня снова", inbound_message_id="m3", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread2, llm=_delete_script(),
        user_text="да", inbound_message_id="m4", channel="max")
    db_session.expire_all()

    assert is_tenant_active(db_session, u.tenant_id) is False
    assert len(_audit_rows(db_session, "user.self_delete.requested")) == 2, (
        "повторный запрос после restore должен дать НОВЫЙ requested-аудит")
    assert len(_audit_rows(db_session, "user.self_delete.completed")) == 2


# ---------------------------------------------------------------------------
# FIX 3 — zero-args contract (anti-injection): empty args schema
# ---------------------------------------------------------------------------


def test_self_delete_has_empty_args_schema(db_session):
    """FIX 3: the tool takes NO arguments — pins the zero-args anti-injection
    contract (the model cannot pass a tenant_id/user_id to retarget)."""
    u = seed_telegram_user(db_session); db_session.commit()
    tool = _tools(db_session, u)[_TOOL]

    # langchain StructuredTool exposes the arg schema; for a zero-arg @tool the
    # generated schema has NO properties (nothing the model can fill).
    schema = tool.args_schema.model_json_schema()
    assert schema.get("properties", {}) == {}, (
        f"delete_my_account must accept zero args (anti-injection): {schema}")
    assert tool.args == {}, f"tool.args must be empty: {tool.args}"

