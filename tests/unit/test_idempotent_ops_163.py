"""#163 Фаза 1в — exact-replay helper на инъецированной no-op мутации.

Контракт фазы (чеклист named-X): повтор того же operation_id+args → ровно одна мутация + тот же
payload; иной args_hmac на committed → внутренняя ошибка (не тихий replay).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sreda.db.models.tool_operations import ToolOperationResult
from sreda.services.idempotent_ops import (
    IdempotencyArgsMismatch,
    IdempotencyInFlight,
    IdempotencyScopeMismatch,
    compute_args_hmac,
    execute_idempotent_durable_op,
)


def _seed_row(db_session, *, operation_id, tenant_id="t1", user_id="u1", status="committed",
              args_hmac="h", payload=None):
    now = datetime.now(timezone.utc)
    row = ToolOperationResult(
        id=f"tor_{operation_id}", tenant_id=tenant_id, user_id=user_id,
        operation_family="reminders", tool_name=None, operation_id=operation_id,
        args_hmac=args_hmac, status=status, stable_return_payload=payload,
        created_at=now, updated_at=now)
    db_session.add(row)
    db_session.commit()
    return row


def test_exact_replay_injected_noop_163(db_session):
    """Повтор того же operation_id+args_hmac → mutate_fn ОДИН раз, тот же payload, 1 строка."""
    calls = []

    def mutate():
        calls.append(1)
        return {"ref": "rem_1", "ok": True}

    kw = dict(operation_id="op-1", tenant_id="t1", user_id="u1",
              operation_family="reminders", args_hmac="h1")
    r1 = execute_idempotent_durable_op(db_session, mutate_fn=mutate, **kw)
    r2 = execute_idempotent_durable_op(db_session, mutate_fn=mutate, **kw)
    assert r1 == r2 == {"ref": "rem_1", "ok": True}
    assert len(calls) == 1, f"мутация должна выполниться РОВНО один раз: {calls}"
    rows = db_session.query(ToolOperationResult).filter(
        ToolOperationResult.operation_id == "op-1").all()
    assert len(rows) == 1 and rows[0].status == "committed"


def test_exact_replay_through_decrypt_path_163(db_session):
    """Replay через РЕАЛЬНЫЙ decrypt-путь (expire_all → перечитать из шифротекста), а не
    identity-map: payload реконструирован из БД == исходный, мутации нет (субагент R1 MINOR)."""
    calls = []

    def mutate():
        calls.append(1)
        return {"ref": "task_9", "n": 3}

    kw = dict(operation_id="op-dec", tenant_id="t1", user_id="u1",
              operation_family="tasks", args_hmac="hd")
    r1 = execute_idempotent_durable_op(db_session, mutate_fn=mutate, **kw)
    db_session.expire_all()  # сбросить identity-map → 2-й вызов читает payload из шифротекста БД
    r2 = execute_idempotent_durable_op(db_session, mutate_fn=mutate, **kw)
    assert len(calls) == 1, f"мутация один раз: {calls}"
    assert r2 == {"ref": "task_9", "n": 3} == r1, f"decrypt round-trip разошёлся: {r2!r}"


def test_args_hmac_mismatch_internal_error_163(db_session):
    """Тот же operation_id, иной args_hmac (committed) → IdempotencyArgsMismatch, НЕ тихий replay."""
    execute_idempotent_durable_op(
        db_session, operation_id="op-2", tenant_id="t1", user_id="u1",
        operation_family="reminders", args_hmac="hA", mutate_fn=lambda: "ok")
    with pytest.raises(IdempotencyArgsMismatch):
        execute_idempotent_durable_op(
            db_session, operation_id="op-2", tenant_id="t1", user_id="u1",
            operation_family="reminders", args_hmac="hB", mutate_fn=lambda: "ok2")


def test_pending_in_flight_not_rerun_163(db_session):
    """Codex high R1 CRITICAL: существующий pending (исход неизвестен) → IdempotencyInFlight,
    mutate НЕ перезапускается (анти-двойное-применение)."""
    _seed_row(db_session, operation_id="op-pend", status="pending", args_hmac="h")
    calls = []
    with pytest.raises(IdempotencyInFlight):
        execute_idempotent_durable_op(
            db_session, operation_id="op-pend", tenant_id="t1", user_id="u1",
            operation_family="reminders", args_hmac="h",
            mutate_fn=lambda: calls.append(1))
    assert calls == [], "pending не должен перезапускать мутацию"


def test_scope_mismatch_cross_tenant_163(db_session):
    """Codex high R1 MAJOR: строка по operation_id принадлежит ДРУГОМУ tenant → IdempotencyScopeMismatch
    (анти-cross-tenant replay), не отдаём чужой payload."""
    _seed_row(db_session, operation_id="op-x", tenant_id="OTHER", user_id="u1",
              status="committed", args_hmac="h", payload="secret")
    with pytest.raises(IdempotencyScopeMismatch):
        execute_idempotent_durable_op(
            db_session, operation_id="op-x", tenant_id="t1", user_id="u1",
            operation_family="reminders", args_hmac="h", mutate_fn=lambda: "new")


def test_compute_args_hmac_stable_and_sensitive_163():
    """hmac стабилен к порядку ключей, но реагирует на значения."""
    a = compute_args_hmac({"x": 1, "y": 2}, secret="s")
    b = compute_args_hmac({"y": 2, "x": 1}, secret="s")  # тот же набор, иной порядок
    c = compute_args_hmac({"x": 1, "y": 3}, secret="s")  # иное значение
    assert a == b, "hmac должен быть стабилен к порядку ключей"
    assert a != c, "hmac должен реагировать на значения"
