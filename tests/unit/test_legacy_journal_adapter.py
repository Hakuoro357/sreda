from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.agents.contracts import ResultKind
from sreda.agents.journal import ToolJournal
from sreda.agents.legacy_journal_adapter import (
    LegacyToolDispatchRecord,
    append_legacy_dispatch,
    attempted_tool_names_for_audit,
    enforce_legacy_journal_response,
    side_effects_count,
    successful_tool_names_for_audit,
)
from sreda.db.models.core import Tenant, Workspace
from sreda.db.models.r39 import R39RunJournal
from sreda.db.models.runtime import AgentRun, AgentThread
from sreda.runtime.handlers import (
    _persist_legacy_journal_row,
    _serialize_legacy_journal_entry,
    _should_persist_legacy_journal,
)


def test_append_successful_physical_mutation_counts_side_effect() -> None:
    journal = ToolJournal()
    record = LegacyToolDispatchRecord(
        tool_call_id="call_1",
        tool_name="schedule_reminder",
        args={"title": "секретный сырой текст", "trigger_iso": "2026-05-21T10:00:00"},
        result_str="ok:scheduled:rem_123",
        is_physical=True,
        loop_index=0,
        batch_index=0,
        tenant_id="tenant_1",
        user_id="user_1",
        run_id="run_1",
        feature_key="housewife_assistant",
    )

    outcome = append_legacy_dispatch(journal, record)

    assert outcome.added is True
    assert len(journal.entries) == 1
    entry = journal.entries[0]
    assert entry.tool_name == "schedule_reminder"
    assert entry.result_kind is ResultKind.SUCCESS
    assert entry.entity_id == "rem_123"
    assert "секретный сырой текст" not in str(entry.result_data)
    assert successful_tool_names_for_audit(journal) == {"schedule_reminder"}
    assert attempted_tool_names_for_audit(journal) == {"schedule_reminder"}
    assert side_effects_count(journal) == 1


def test_non_physical_duplicate_is_skipped() -> None:
    journal = ToolJournal()
    record = LegacyToolDispatchRecord(
        tool_call_id="call_dup",
        tool_name="add_shopping_items",
        args={"items": [{"title": "milk"}]},
        result_str="ok:added:1",
        is_physical=False,
        loop_index=0,
        batch_index=1,
        tenant_id="tenant_1",
        user_id="user_1",
        run_id="run_1",
        feature_key="housewife_assistant",
    )

    outcome = append_legacy_dispatch(journal, record)

    assert outcome.added is False
    assert outcome.duplicate_skipped is True
    assert journal.entries == []
    assert side_effects_count(journal) == 0


def test_unknown_tool_entry_is_sanitized_and_not_counted_as_side_effect() -> None:
    journal = ToolJournal()
    record = LegacyToolDispatchRecord(
        tool_call_id="call_unknown",
        tool_name="unknown_tool",
        args={"secret": "raw secret should not leak"},
        result_str="ok:raw payload should not leak",
        is_physical=True,
        loop_index=0,
        batch_index=0,
        tenant_id="tenant_1",
        user_id=None,
        run_id="run_1",
        feature_key="housewife_assistant",
    )

    outcome = append_legacy_dispatch(journal, record)

    assert outcome.added is True
    assert len(journal.entries) == 1
    entry = journal.entries[0]
    assert entry.result_kind is ResultKind.SUCCESS
    assert entry.result_data["known_mutating_tool"] is False
    assert "raw secret" not in str(entry.result_data)
    assert "raw payload" not in str(entry.result_data)
    assert successful_tool_names_for_audit(journal) == set()
    assert side_effects_count(journal) == 0


def test_bare_result_is_not_treated_as_success() -> None:
    journal = ToolJournal()
    append_legacy_dispatch(
        journal,
        LegacyToolDispatchRecord(
            tool_call_id="call_1",
            tool_name="schedule_reminder",
            args={},
            result_str="reminder created successfully",
            is_physical=True,
            loop_index=0,
            batch_index=0,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
            feature_key="housewife_assistant",
        ),
    )

    assert journal.entries[0].result_kind is ResultKind.FAILURE
    assert successful_tool_names_for_audit(journal) == set()
    assert side_effects_count(journal) == 0


def test_enforce_replaces_unbacked_text_and_clears_stale_markup() -> None:
    journal = ToolJournal()
    append_legacy_dispatch(
        journal,
        LegacyToolDispatchRecord(
            tool_call_id="call_1",
            tool_name="schedule_reminder",
            args={},
            result_str="ok:scheduled:rem_123",
            is_physical=True,
            loop_index=0,
            batch_index=0,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
            feature_key="housewife_assistant",
        ),
    )

    result = enforce_legacy_journal_response(
        text="Готово, добавила покупки и поставила напоминание",
        reply_markup={"inline_keyboard": [[{"text": "Показать покупки"}]]},
        journal=journal,
        enforce_enabled=True,
        journal_unreliable=False,
        detector=lambda text, tools: "add_shopping" not in tools,
        safe_ack_fn=lambda tools: "Готово: reminder",
    )

    assert result.text == "Готово: reminder"
    assert result.reply_markup is None
    assert result.replaced is True
    assert result.audit_unbacked is True


def test_serialize_legacy_journal_entry_drops_non_scalar_values() -> None:
    journal = ToolJournal()
    append_legacy_dispatch(
        journal,
        LegacyToolDispatchRecord(
            tool_call_id="call_1",
            tool_name="schedule_reminder",
            args={},
            result_str="ok:scheduled:rem_123",
            is_physical=True,
            loop_index=0,
            batch_index=0,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
            feature_key="housewife_assistant",
        ),
    )
    journal.entries[0].result_data["nested"] = {"raw": "drop"}
    journal.entries[0].result_data["items"] = ["drop"]

    serialized = _serialize_legacy_journal_entry(journal.entries[0])

    assert serialized["result_data"]["summary"] == "ok"
    assert "nested" not in serialized["result_data"]
    assert "items" not in serialized["result_data"]


def test_should_not_persist_legacy_journal_during_r39_shadow() -> None:
    assert _should_persist_legacy_journal("shadow") is False
    assert _should_persist_legacy_journal("off") is True
    assert _should_persist_legacy_journal("live") is True


def test_persist_legacy_journal_row_inserts_sanitized_legacy_row() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Tenant.__table__,
        Workspace.__table__,
        AgentThread.__table__,
        AgentRun.__table__,
        R39RunJournal.__table__,
    ):
        table.create(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        session.add(Tenant(id="tenant_1", name="Tenant"))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace"))
        session.add(
            AgentThread(
                id="thread_1",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                channel_type="telegram",
                external_chat_id="chat_1",
            )
        )
        session.add(
            AgentRun(
                id="run_1",
                thread_id="thread_1",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                action_type="conversation.chat",
            )
        )
        session.flush()
        journal = ToolJournal()
        append_legacy_dispatch(
            journal,
            LegacyToolDispatchRecord(
                tool_call_id="call_1",
                tool_name="schedule_reminder",
                args={"title": "raw title should not persist"},
                result_str="ok:scheduled:rem_123",
                is_physical=True,
                loop_index=0,
                batch_index=0,
                tenant_id="tenant_1",
                user_id="user_1",
                run_id="run_1",
                feature_key="housewife_assistant",
            ),
        )

        _persist_legacy_journal_row(
            session=session,
            run_id="run_1",
            tenant_id="tenant_1",
            journal=journal,
            audit_unbacked=True,
        )
        session.commit()

        row = session.get(R39RunJournal, "run_1")
        assert row is not None
        assert row.mode == "legacy"
        assert row.plan_kind == "legacy"
        assert row.audit_unbacked is True
        assert row.side_effects_count == 1
        payload = json.loads(row.journal_json)
        assert payload[0]["entity_id"] == "rem_123"
        assert "raw title should not persist" not in row.journal_json
    finally:
        session.close()


def test_unreliable_journal_with_attempted_tool_gets_neutral_text() -> None:
    journal = ToolJournal()
    append_legacy_dispatch(
        journal,
        LegacyToolDispatchRecord(
            tool_call_id="call_1",
            tool_name="schedule_reminder",
            args={},
            result_str="error:internal",
            is_physical=True,
            loop_index=0,
            batch_index=0,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
            feature_key="housewife_assistant",
        ),
    )

    result = enforce_legacy_journal_response(
        text="Готово, поставила",
        reply_markup={"inline_keyboard": [[{"text": "Ок"}]]},
        journal=journal,
        enforce_enabled=True,
        journal_unreliable=True,
        detector=lambda _text, _tools: False,
        safe_ack_fn=lambda tools: f"safe:{tools}",
    )

    assert result.text == "Не смогла подтвердить действие. Попробуй ещё раз."
    assert result.reply_markup is None
    assert result.replaced is True
