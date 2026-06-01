"""Unit tests for sreda.runtime.planner.tool_runtime (Sub-A12 Phase E)."""

from __future__ import annotations

import asyncio

import pytest

from sreda.runtime.planner.tool_runtime import (
    ToolRuntimeContext,
    allocate_operation_id,
    bind_tool_runtime,
    current_tool_runtime,
    is_retriable,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides) -> ToolRuntimeContext:
    defaults = dict(
        operation_id="op_abc",
        execution_id="exec_1",
        step_id="step_1",
        tool_name="add_item",
        tenant_id="tenant_1",
    )
    defaults.update(overrides)
    return ToolRuntimeContext(**defaults)


# ---------------------------------------------------------------------------
# allocate_operation_id — determinism
# ---------------------------------------------------------------------------


class TestAllocateOperationId:
    def test_same_inputs_same_id(self) -> None:
        a = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
        b = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
        assert a == b

    def test_different_step_id_different_id(self) -> None:
        a = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
        b = allocate_operation_id(turn_key="tk1", step_id="s2", tool_name="tool_a")
        assert a != b

    def test_different_tool_name_different_id(self) -> None:
        a = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
        b = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_b")
        assert a != b

    def test_different_turn_key_different_id(self) -> None:
        a = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
        b = allocate_operation_id(turn_key="tk2", step_id="s1", tool_name="tool_a")
        assert a != b

    def test_none_turn_key_rejected(self) -> None:
        """FAIL-CLOSED: a missing turn_key would collide across executions."""
        with pytest.raises(ValueError, match="turn_key"):
            allocate_operation_id(turn_key=None, step_id="s1", tool_name="tool_a")  # type: ignore[arg-type]

    def test_empty_turn_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="turn_key"):
            allocate_operation_id(turn_key="", step_id="s1", tool_name="tool_a")

    def test_empty_step_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            allocate_operation_id(turn_key="tk1", step_id="", tool_name="tool_a")

    def test_empty_tool_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="")

    def test_exact_sha1_vector(self) -> None:
        """Pin the exact field order + separator + prefix so a future change
        to the hash recipe (which would silently break idempotency across a
        deploy) is caught."""
        import hashlib

        payload = "\x1f".join(["tk1", "s1", "tool_a"])
        expected = "op_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()
        assert (
            allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
            == expected
        )

    def test_prefix_and_max_length(self) -> None:
        op_id = allocate_operation_id(turn_key="tk1", step_id="s1", tool_name="tool_a")
        assert op_id.startswith("op_")
        assert len(op_id) <= 64

    def test_exact_length(self) -> None:
        # SHA-1 hex = 40 chars; prefix "op_" = 3 chars → total 43
        op_id = allocate_operation_id(turn_key="x", step_id="y", tool_name="z")
        assert len(op_id) == 43


# ---------------------------------------------------------------------------
# ContextVar — bind_tool_runtime / current_tool_runtime
# ---------------------------------------------------------------------------


class TestContextVar:
    def test_default_is_none(self) -> None:
        assert current_tool_runtime() is None

    def test_inside_bind_returns_ctx(self) -> None:
        ctx = _make_ctx()
        with bind_tool_runtime(ctx):
            assert current_tool_runtime() is ctx

    def test_after_bind_resets_to_none(self) -> None:
        ctx = _make_ctx()
        with bind_tool_runtime(ctx):
            pass
        assert current_tool_runtime() is None

    def test_reset_after_exception(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(RuntimeError):
            with bind_tool_runtime(ctx):
                raise RuntimeError("boom")
        assert current_tool_runtime() is None

    def test_nested_bind_restores_outer(self) -> None:
        outer = _make_ctx(operation_id="op_outer", step_id="s_outer")
        inner = _make_ctx(operation_id="op_inner", step_id="s_inner")
        with bind_tool_runtime(outer):
            assert current_tool_runtime() is outer
            with bind_tool_runtime(inner):
                assert current_tool_runtime() is inner
            assert current_tool_runtime() is outer
        assert current_tool_runtime() is None

    def test_propagates_into_asyncio_to_thread(self) -> None:
        """A sync tool wrapped in asyncio.to_thread must observe the bound
        context (to_thread copies the context). This is the mechanism the
        executor relies on for SYNC durable-write tools."""
        ctx = _make_ctx(operation_id="op_thread")

        def _sync_tool_reads_ctx() -> ToolRuntimeContext | None:
            return current_tool_runtime()

        async def _run() -> ToolRuntimeContext | None:
            with bind_tool_runtime(ctx):
                return await asyncio.to_thread(_sync_tool_reads_ctx)

        seen = asyncio.run(_run())
        assert seen is ctx

    def test_isolated_across_gather(self) -> None:
        """Concurrent gathered coros each see only their OWN bound context —
        the executor binds per-step inside _execute_one_step, and gather runs
        each coro in its own copied context, so no cross-step leak."""
        seen: dict[str, str | None] = {}

        async def _step(name: str, delay: float) -> None:
            ctx = _make_ctx(operation_id=f"op_{name}", step_id=name)
            with bind_tool_runtime(ctx):
                await asyncio.sleep(delay)
                cur = current_tool_runtime()
                seen[name] = cur.operation_id if cur else None

        async def _run() -> None:
            await asyncio.gather(
                _step("a", 0.03), _step("b", 0.01), _step("c", 0.02)
            )

        asyncio.run(_run())
        assert seen == {"a": "op_a", "b": "op_b", "c": "op_c"}


# ---------------------------------------------------------------------------
# is_retriable — fail-closed classifier
# ---------------------------------------------------------------------------


class TestIsRetriable:
    # --- Durable writes: NEVER blind-retriable (ambiguous post-invoke). ---
    def test_durable_write_timeout_not_retriable(self) -> None:
        """A durable-write timeout may have committed late → must NOT retry."""
        assert is_retriable(asyncio.TimeoutError(), is_durable_write=True) is False

    def test_durable_write_connection_error_not_retriable(self) -> None:
        assert is_retriable(ConnectionError(), is_durable_write=True) is False

    def test_durable_write_invalidated_db_error_not_retriable(self) -> None:
        import sqlalchemy.exc

        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("inner"))
        exc.connection_invalidated = True  # type: ignore[attr-defined]
        assert is_retriable(exc, is_durable_write=True) is False

    # --- Read / non-durable: transient infra is safe to retry (no side effect). ---
    def test_asyncio_timeout_retriable_for_read(self) -> None:
        assert is_retriable(asyncio.TimeoutError(), is_durable_write=False) is True

    def test_builtin_timeout_retriable_for_read(self) -> None:
        assert is_retriable(TimeoutError(), is_durable_write=False) is True

    def test_connection_error_retriable_for_read(self) -> None:
        assert is_retriable(ConnectionError(), is_durable_write=False) is True

    def test_value_error_not_retriable(self) -> None:
        assert is_retriable(ValueError("bad input"), is_durable_write=False) is False

    def test_generic_exception_not_retriable(self) -> None:
        assert is_retriable(Exception("something"), is_durable_write=False) is False

    def test_type_error_not_retriable(self) -> None:
        assert is_retriable(TypeError("wrong type"), is_durable_write=False) is False

    def test_runtime_error_not_retriable(self) -> None:
        assert is_retriable(RuntimeError("uh oh"), is_durable_write=False) is False

    def test_sqlalchemy_operational_error_without_invalidation(self) -> None:
        import sqlalchemy.exc

        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("inner"))
        # connection_invalidated defaults to False on a freshly constructed exc
        assert is_retriable(exc, is_durable_write=False) is False

    def test_sqlalchemy_operational_error_with_invalidation_read(self) -> None:
        import sqlalchemy.exc

        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("inner"))
        exc.connection_invalidated = True  # type: ignore[attr-defined]
        assert is_retriable(exc, is_durable_write=False) is True

    def test_is_durable_write_is_required_keyword(self) -> None:
        """No silently-unsafe default — every caller must classify the tool."""
        with pytest.raises(TypeError):
            is_retriable(asyncio.TimeoutError())  # type: ignore[call-arg]
