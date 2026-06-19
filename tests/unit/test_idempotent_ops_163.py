"""#163 Фаза 1в — exact-replay helper на инъецированной no-op мутации.

Контракт фазы (чеклист named-X): повтор того же operation_id+args → ровно одна мутация + тот же
payload; иной args_hmac на committed → внутренняя ошибка (не тихий replay).
"""

from __future__ import annotations

import pytest

from sreda.db.models.tool_operations import ToolOperationResult
from sreda.services.idempotent_ops import (
    IdempotencyArgsMismatch,
    compute_args_hmac,
    execute_idempotent_durable_op,
)


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


def test_args_hmac_mismatch_internal_error_163(db_session):
    """Тот же operation_id, иной args_hmac (committed) → IdempotencyArgsMismatch, НЕ тихий replay."""
    execute_idempotent_durable_op(
        db_session, operation_id="op-2", tenant_id="t1", user_id="u1",
        operation_family="reminders", args_hmac="hA", mutate_fn=lambda: "ok")
    with pytest.raises(IdempotencyArgsMismatch):
        execute_idempotent_durable_op(
            db_session, operation_id="op-2", tenant_id="t1", user_id="u1",
            operation_family="reminders", args_hmac="hB", mutate_fn=lambda: "ok2")


def test_compute_args_hmac_stable_and_sensitive_163():
    """hmac стабилен к порядку ключей, но реагирует на значения."""
    a = compute_args_hmac({"x": 1, "y": 2}, secret="s")
    b = compute_args_hmac({"y": 2, "x": 1}, secret="s")  # тот же набор, иной порядок
    c = compute_args_hmac({"x": 1, "y": 3}, secret="s")  # иное значение
    assert a == b, "hmac должен быть стабилен к порядку ключей"
    assert a != c, "hmac должен реагировать на значения"
