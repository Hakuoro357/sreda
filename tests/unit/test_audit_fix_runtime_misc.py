"""Audit 2026-07-18 — регрессионные тесты воркера runtime-misc.

Покрывает фиксы:
- cross-latency NEW-2 (MAJOR): legacy tool-слой MAX-чата уходит с
  loop-потока через ``asyncio.to_thread`` (handlers.py).
- runtime-core #6 (MINOR): fallback НЕ ре-стримит в ack после частичного
  стрима primary (llm_caller.py).
- runtime-core #7 (MINOR): args-HMAC fail-closed без SREDA_ENCRYPTION_KEY
  (tools.py).
- runtime-core #8 (MINOR): 25-символьный префикс refusal-substitute не
  режет органические ответы (handlers.py).
- runtime-core #9 (MINOR): batch-чтение outbox в _load_chat_history
  (handlers.py).
- runtime-core #10 (MINOR): propose_update рендерит русские имена полей
  (handlers.py).
- llm-core #7 legacy-сторона: rescue-путь проходит strip_reasoning_prefix.

Все тесты — sqlite in-memory / pure-unit: без сети и без PostgreSQL.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, Workspace
from sreda.db.models.runtime import AgentRun, AgentThread
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.handlers import (
    _dispatch_tool_calls_batch,
    _is_synthetic_fallback_reply,
    _load_chat_history,
    _REFUSAL_SUBSTITUTE_MESSAGE,
    _run_legacy_react_loop,
    execute_profile_propose_update,
    finalize_chat_reply,
)


# ---------------------------------------------------------------------------
# cross-latency NEW-2 (MAJOR): tool-диспетчер снят с loop-потока
# ---------------------------------------------------------------------------


def test_dispatch_batch_via_to_thread_runs_off_loop_thread() -> None:
    """Точный call-pattern фикса: ``await asyncio.to_thread(
    _dispatch_tool_calls_batch, ...)`` — инструменты исполняются НЕ на
    потоке event loop, результаты сохраняют порядок tool_calls."""
    calls = [
        {"id": "tc1", "name": "rec", "args": {"x": 1}},
        {"id": "tc2", "name": "rec", "args": {"x": 2}},
    ]
    seen: list[tuple[int, int]] = []
    loop_tid = threading.get_ident()

    def _rec(x: int) -> str:
        seen.append((x, threading.get_ident()))
        return f"ok:{x}"

    tools = {"rec": SimpleNamespace(invoke=lambda args: _rec(args["x"]))}

    async def _main():
        return await asyncio.to_thread(_dispatch_tool_calls_batch, calls, tools)

    results = asyncio.run(_main())

    assert [(r[0], r[2]) for r in results] == [("tc1", "ok:1"), ("tc2", "ok:2")]
    assert seen, "инструмент не был вызван"
    assert all(tid != loop_tid for _, tid in seen), (
        "tool исполнился на потоке event loop — фикс NEW-2 регрессировал"
    )


def test_legacy_loop_dispatches_tools_via_to_thread_source_guard() -> None:
    """Source-guard против отката MAJOR-фикса: вызов batch-диспетча в
    async-цикле обязан идти через ``await asyncio.to_thread``."""
    src = inspect.getsource(_run_legacy_react_loop)
    assert "await asyncio.to_thread(" in src
    assert "_dispatch_tool_calls_batch" in src


# ---------------------------------------------------------------------------
# runtime-core #8 (MINOR): префикс refusal-substitute не режет органику
# ---------------------------------------------------------------------------


def test_synthetic_fallback_exact_substitute_still_detected() -> None:
    assert _is_synthetic_fallback_reply(_REFUSAL_SUBSTITUTE_MESSAGE) is True
    # trailing punctuation / whitespace tolerated
    assert _is_synthetic_fallback_reply(_REFUSAL_SUBSTITUTE_MESSAGE + " ") is True
    assert _is_synthetic_fallback_reply("...") is True
    assert _is_synthetic_fallback_reply("") is True


def test_synthetic_fallback_organic_apology_not_dropped() -> None:
    """Органический ответ модели, начинающийся с «Прости, не получилось
    поня…», но НЕ являющийся нашей substitution-фразой, обязан остаться
    в истории (прежний [:25]-префикс молча выкидывал такие ходы)."""
    organic = (
        "Прости, не получилось понять, что ты имеешь в виду — "
        "уточни, пожалуйста, про какой список речь?"
    )
    assert organic.startswith(_REFUSAL_SUBSTITUTE_MESSAGE[:25])  # регрессия была бы красной
    assert _is_synthetic_fallback_reply(organic) is False


# ---------------------------------------------------------------------------
# runtime-core #10 (MINOR): propose_update — русские имя поля и значение
# ---------------------------------------------------------------------------


def _profile_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    from sreda.db.repositories.seed import SeedRepository

    SeedRepository(session).ensure_tenant_bundle(
        tenant_id="t1", tenant_name="t", workspace_id="w1", workspace_name="w",
        user_id="u1", telegram_account_id="42", assistant_id="a1",
        assistant_name="a",
    )
    session.commit()
    return session


def _envelope(action_type: str, **params) -> ActionEnvelope:
    return ActionEnvelope(
        action_type=action_type,
        tenant_id="t1",
        workspace_id="w1",
        assistant_id="a1",
        user_id="u1",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="system",
        source_value=action_type,
        params=params,
    )


def test_propose_update_renders_ru_field_and_value() -> None:
    session = _profile_session()
    replies = execute_profile_propose_update(
        session,
        _envelope(
            "profile.propose_update",
            field_name="communication_style",
            proposed_value="casual",
        ),
        {},
    )
    assert len(replies) == 1
    text = replies[0].text
    assert "стиль общения" in text
    assert "по-простому" in text
    assert "communication_style" not in text
    assert "casual" not in text


# ---------------------------------------------------------------------------
# runtime-core #9 (MINOR): outbox читается одним IN-запросом
# ---------------------------------------------------------------------------


def _history_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Default"))
    session.add(
        AgentThread(
            id="thread_1",
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            channel_type="telegram",
            external_chat_id="100",
            status="active",
        )
    )
    session.commit()
    return engine, session


def _seed_turn(session, *, run_id, user_text, bot_texts, created_at, status="completed"):
    outbox_ids = []
    for i, bot_text in enumerate(bot_texts):
        oid = f"out_{run_id}_{i}"
        session.add(
            OutboxMessage(
                id=oid,
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                channel_type="telegram",
                status="sent",
                payload_json=json.dumps({"chat_id": "100", "text": bot_text}),
            )
        )
        outbox_ids.append(oid)
    run = AgentRun(
        id=run_id,
        thread_id="thread_1",
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        action_type="conversation.chat",
        status=status,
        input_json=json.dumps({"params": {"text": user_text}}),
        result_json=json.dumps({"outbox_message_ids": outbox_ids}),
    )
    run.created_at = created_at
    session.add(run)
    session.commit()
    return run


def test_load_chat_history_batches_outbox_fetches() -> None:
    """5 ходов × 2 outbox-сообщения: ровно ОДИН SELECT по outbox_messages
    (было: per-id ``session.get`` в цикле — до ~20 PK-lookup'ов на ход)."""
    engine, session = _history_session()
    base_ts = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    for i in range(5):
        _seed_turn(
            session,
            run_id=f"r{i}",
            user_text=f"вопрос {i}",
            bot_texts=[f"ответ {i}a", f"ответ {i}b"],
            created_at=base_ts + timedelta(seconds=i),
        )
    current = _seed_turn(
        session,
        run_id="cur",
        user_text="текущий",
        bot_texts=[],
        created_at=base_ts + timedelta(seconds=99),
        status="pending",
    )

    outbox_selects: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "outbox_messages" in statement:
            outbox_selects.append(statement)

    history = _load_chat_history(session, current.id)

    # поведение не изменилось: обе части склеены, порядок newest-first
    assert len(history) == 5
    assert history[0] == ("вопрос 4", "ответ 4a\nответ 4b")
    assert history[-1] == ("вопрос 0", "ответ 0a\nответ 0b")
    assert len(outbox_selects) == 1, (
        f"N+1 регрессия: {len(outbox_selects)} SELECT по outbox_messages"
    )


# ---------------------------------------------------------------------------
# runtime-core #6 (MINOR): fallback не ре-стримит в ack после частичного
# стрима primary (llm_caller.py)
# ---------------------------------------------------------------------------


def _make_caller(fallback_llm=None) -> "object":
    from sreda.runtime.llm_caller import LlmCaller

    return LlmCaller(
        primary_llm=MagicMock(name="primary_llm"),
        fallback_llm=fallback_llm,
        primary_provider="primary_prov",
        fallback_provider="fallback_prov" if fallback_llm else None,
        per_call_timeout=10.0,
        tenant_id="t1",
        feature_key="feat",
    )


def _call(caller, on_text_update):
    return asyncio.run(
        caller.ainvoke_with_fallback(
            messages=[],
            iter_n=0,
            trace_meta={},
            on_text_update=on_text_update,
            persist_request=AsyncMock(),
            persist_response=AsyncMock(),
            persist_error=AsyncMock(),
        )
    )


def test_fallback_stream_suppressed_after_partial_primary_stream(monkeypatch) -> None:
    """Primary отстримил «часть» в ack и умер по таймауту → fallback
    вызывается с on_text_update=None (никакого видимого «отката» ack)."""
    from sreda.services.llm import LLMCallTimeout

    fallback_cb_seen: list = []

    async def fake_invoke(llm, messages, *, timeout_seconds, on_text_update, provider=None):
        if provider == "primary_prov":
            assert on_text_update is not None
            on_text_update("частичный текст")  # primary успел показать кусок
            raise LLMCallTimeout("primary stream timeout")
        fallback_cb_seen.append(on_text_update)
        return MagicMock(name="fallback_ai_msg")

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout", fake_invoke,
    )
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", MagicMock(), raising=False,
    )

    streamed: list[str] = []
    caller = _make_caller(fallback_llm=MagicMock(name="fallback_llm"))
    _call(caller, on_text_update=streamed.append)

    assert streamed == ["частичный текст"]  # primary-стрим дошёл до ack
    assert fallback_cb_seen == [None], (
        "fallback обязан идти БЕЗ streaming-callback после частичного "
        "стрима primary — иначе ack видимо «откатывается»"
    )


def test_fallback_keeps_stream_callback_when_primary_silent(monkeypatch) -> None:
    """Primary упал БЕЗ стрима → fallback получает исходный callback
    (UX-стриминг fallback'а сохраняется)."""
    from sreda.services.llm import LLMCallTimeout

    fallback_cb_seen: list = []

    async def fake_invoke(llm, messages, *, timeout_seconds, on_text_update, provider=None):
        if provider == "primary_prov":
            raise LLMCallTimeout("primary connect timeout")
        fallback_cb_seen.append(on_text_update)
        return MagicMock(name="fallback_ai_msg")

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout", fake_invoke,
    )
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", MagicMock(), raising=False,
    )

    user_cb = lambda text: None  # noqa: E731
    caller = _make_caller(fallback_llm=MagicMock(name="fallback_llm"))
    _call(caller, on_text_update=user_cb)

    assert fallback_cb_seen == [user_cb]


# ---------------------------------------------------------------------------
# runtime-core #7 (MINOR): args-HMAC fail-closed без encryption_key
# ---------------------------------------------------------------------------


def test_idem_write_fail_closed_without_encryption_key(monkeypatch) -> None:
    """Без SREDA_ENCRYPTION_KEY idempotent memory-write НЕ считает HMAC на
    публичной константе → EncryptionConfigError (эталон: #193 durable key)."""
    from sreda.config.settings import get_settings
    from sreda.db.repositories.seed import SeedRepository
    from sreda.runtime.planner.tool_runtime import (
        ToolRuntimeContext,
        bind_tool_runtime,
    )
    from sreda.runtime.tools import build_memory_tools
    from sreda.services.embeddings import FakeEmbeddingClient
    from sreda.services.encryption import EncryptionConfigError

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    SeedRepository(session).ensure_tenant_bundle(
        tenant_id="t1", tenant_name="t", workspace_id="w", workspace_name="w",
        user_id="u1", telegram_account_id="1", assistant_id="a",
        assistant_name="a",
    )
    session.commit()
    tools = {
        t.name: t
        for t in build_memory_tools(
            session=session, tenant_id="t1", user_id="u1",
            embedding_client=FakeEmbeddingClient(),
        )
    }

    monkeypatch.delenv("SREDA_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    try:
        ctx = ToolRuntimeContext(
            operation_id="op-fail-closed", execution_id="e", step_id="s1",
            tool_name="save_episode", tenant_id="t1", user_id="u1",
            turn_key="tk", channel="react", thread_id="th", origin="react",
        )
        with bind_tool_runtime(ctx):
            with pytest.raises(EncryptionConfigError):
                tools["save_episode"].invoke({"summary": "без ключа"})
    finally:
        get_settings.cache_clear()
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# llm-core #7 (legacy-сторона): rescue-путь тоже проходит
# strip_reasoning_prefix — source-guard против отката
# ---------------------------------------------------------------------------


def test_rescue_path_applies_strip_reasoning_prefix_source_guard() -> None:
    src = inspect.getsource(finalize_chat_reply)
    # основной путь (:3442 эталон) + rescue-ветка — минимум два применения
    assert src.count("strip_reasoning_prefix") >= 2, (
        "rescue-ветка finalize_chat_reply обязана прогонять спасённый "
        "текст через strip_reasoning_prefix (llm-core #7, legacy)"
    )
