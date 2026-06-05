"""Unit tests for scripts/replay/reconstruct.py (Phase B, issue #87).

Tests cover:
- ReplayExportRecord round-trip (to_dict / from_dict).
- load_export_records parses JSONL correctly (blank lines, comments skipped).
- build_prompt_args produces a valid PromptBuildArgs with HISTORICAL now
  (NOT datetime.now()).
- reconstruction_fidelity returns per-field status dict with all expected keys.
- is_fidelity_ok: passes on a fully-ok record; fails when a CRITICAL field is
  missing or degraded.
- load_manifest round-trip via synthetic JSONL.
- pair_with_gold matches by turn_id_hash; unmatched records flagged.

All fixtures use synthetic placeholder data (tenant_tg_<SMOKE>, turn_<hash>).
No prod data, no real chat ids, no DB, no network, no LLM.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Add scripts/replay to sys.path so we can import reconstruct + manifest.
_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest  # noqa: E402

from reconstruct import (  # noqa: E402
    ReplayExportRecord,
    build_prompt_args,
    is_fidelity_ok,
    load_export_records,
    load_manifest,
    pair_with_gold,
    reconstruction_fidelity,
)
from manifest import ReplayCase, ReplayExpectations  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixture helpers
# ---------------------------------------------------------------------------

_ALL_STATUS_OK = {
    "user_text": "ok",
    "now_tz_locale": "ok",
    "profile": "ok",
    "memory_provenance": "ok",
    "history_window": "ok",
    "active_turn": "ok",
    "voice_meta": "ok",
    "baseline_alignment": "ok",
    "decrypted_ok": "ok",
}

_HASH_A = "abcdef0123456789"
_HASH_B = "1234567890abcdef"


_SENTINEL = object()  # distinguishes "not passed" from explicitly passed empty


def _make_record(
    *,
    turn_id_hash: str = _HASH_A,
    user_message: str = "Что купить в магазине?",
    now_iso: str = "2026-05-10T14:23:00+03:00",
    timezone_name: str = "Europe/Moscow",
    locale: str = "ru",
    voice: dict | None = None,
    profile: object = _SENTINEL,
    memories: object = _SENTINEL,
    history: object = _SENTINEL,
    baseline: object = _SENTINEL,
    reconstruction_status: object = _SENTINEL,
    tenant_placeholder: str = "tenant_tg_<SMOKE>",
) -> ReplayExportRecord:
    _default_profile = {
        "name": "Борис",
        "gender": "M",
        "address": "ты",
        "timezone": "Europe/Moscow",
        "language": "ru",
    }
    _default_memories = [
        {
            "content": "Любит шахматы",
            "source": "memory:core",
            "score": 0.92,
            "saved_at": "2026-05-01",
        }
    ]
    _default_history = {
        "active_turn": {
            "turn_id": "turn_<SMOKE_ACTIVE>",
            "started_at": "2026-05-10T14:20:00+03:00",
            "summary": None,
            "messages": [
                {
                    "role": "юзер",
                    "text": "Привет",
                    "ts": "2026-05-10T14:20:01+03:00",
                },
                {
                    "role": "среда",
                    "text": "Привет! Чем могу помочь?",
                    "ts": "2026-05-10T14:20:02+03:00",
                },
            ],
            "is_active": True,
        },
        "closed_turns": [],
    }
    _default_baseline = {
        "old_reply": "Не забудь молоко и хлеб.",
        "old_called_tools": ["list_shopping"],
        "old_side_effects": [],
    }
    return ReplayExportRecord(
        turn_id_hash=turn_id_hash,
        tenant_placeholder=tenant_placeholder,
        user_message=user_message,
        now_iso=now_iso,
        timezone_name=timezone_name,
        locale=locale,
        voice=voice,
        profile=_default_profile if profile is _SENTINEL else profile,  # type: ignore[arg-type]
        memories=_default_memories if memories is _SENTINEL else memories,  # type: ignore[arg-type]
        history=_default_history if history is _SENTINEL else history,  # type: ignore[arg-type]
        baseline=_default_baseline if baseline is _SENTINEL else baseline,  # type: ignore[arg-type]
        reconstruction_status=(
            dict(_ALL_STATUS_OK) if reconstruction_status is _SENTINEL else reconstruction_status  # type: ignore[arg-type]
        ),
    )


def _make_expectations(**kwargs) -> ReplayExpectations:
    defaults: dict = {
        "expected_intent": "read",
        "memory_dependence": "independent",
        "current_memory_status": "not_applicable",
        "eligibility_reason": "test fixture",
        "label_source": "manual",
        "labeler": "test",
        "confidence": 1.0,
    }
    defaults.update(kwargs)
    return ReplayExpectations(**defaults)


def _make_gold_case(turn_id_hash: str = _HASH_A, **exp_kwargs) -> ReplayCase:
    return ReplayCase(
        turn_id_hash=turn_id_hash,
        tenant_placeholder="tenant_tg_<SMOKE>",
        expectations=_make_expectations(**exp_kwargs),
    )


# ---------------------------------------------------------------------------
# ReplayExportRecord — round-trip
# ---------------------------------------------------------------------------


class TestReplayExportRecordRoundTrip:
    def test_to_dict_from_dict_identity(self):
        record = _make_record()
        restored = ReplayExportRecord.from_dict(record.to_dict())
        assert restored.turn_id_hash == record.turn_id_hash
        assert restored.user_message == record.user_message
        assert restored.now_iso == record.now_iso
        assert restored.timezone_name == record.timezone_name
        assert restored.locale == record.locale
        assert restored.voice == record.voice
        assert restored.profile == record.profile
        assert restored.memories == record.memories
        assert restored.history == record.history
        assert restored.baseline == record.baseline
        assert restored.reconstruction_status == record.reconstruction_status

    def test_to_dict_is_json_serialisable(self):
        record = _make_record()
        data = record.to_dict()
        serialised = json.dumps(data)
        assert json.loads(serialised)["turn_id_hash"] == _HASH_A

    def test_from_dict_missing_required_keys_raises(self):
        """Strict contract (Codex review): a malformed export missing required
        keys must FAIL LOUDLY (KeyError), not silently default — a missing
        ``baseline``/``profile``/etc. could otherwise pass while the status
        claims "ok"."""
        minimal = {
            "turn_id_hash": _HASH_A,
            "tenant_placeholder": "tenant_tg_<SMOKE>",
            "user_message": "Тест",
            "now_iso": "2026-05-10T14:23:00+03:00",
            "timezone_name": "Europe/Moscow",
            "locale": "ru",
            # profile/memories/history/baseline/reconstruction_status MISSING
        }
        with pytest.raises(KeyError) as exc:
            ReplayExportRecord.from_dict(minimal)
        for k in ("profile", "memories", "history", "baseline", "reconstruction_status"):
            assert k in str(exc.value)

    def test_from_dict_voice_is_optional(self):
        """``voice`` is the ONLY optional contract field (absent for text turns)."""
        d = _make_record().to_dict()
        d.pop("voice", None)
        record = ReplayExportRecord.from_dict(d)
        assert record.voice is None

    def test_voice_field_round_trip(self):
        voice = {"is_voice": True, "confidence": 0.87}
        record = _make_record(voice=voice)
        restored = ReplayExportRecord.from_dict(record.to_dict())
        assert restored.voice == voice

    def test_no_real_chat_id_in_fixtures(self):
        """PII guard: no real Telegram chat ids allowed in synthetic fixtures."""
        record = _make_record()
        serialised = json.dumps(record.to_dict())
        # Verify placeholder pattern, and that NO real-id-shaped tenant slug
        # (tenant_tg_<6+ digits>) leaked — without writing a real id literal.
        assert "tenant_tg_<SMOKE>" in serialised
        assert not re.search(r"tenant_tg_\d{6,}", serialised)


# ---------------------------------------------------------------------------
# load_export_records
# ---------------------------------------------------------------------------


class TestLoadExportRecords:
    def _write_jsonl(self, lines: list[str]) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        return Path(tmp.name)

    def test_single_record_parsed(self):
        record = _make_record()
        path = self._write_jsonl([json.dumps(record.to_dict())])
        loaded = load_export_records(path)
        assert len(loaded) == 1
        assert loaded[0].turn_id_hash == _HASH_A

    def test_multiple_records(self):
        r1 = _make_record(turn_id_hash=_HASH_A)
        r2 = _make_record(turn_id_hash=_HASH_B)
        path = self._write_jsonl(
            [json.dumps(r1.to_dict()), json.dumps(r2.to_dict())]
        )
        loaded = load_export_records(path)
        assert len(loaded) == 2
        assert loaded[0].turn_id_hash == _HASH_A
        assert loaded[1].turn_id_hash == _HASH_B

    def test_blank_lines_skipped(self):
        record = _make_record()
        path = self._write_jsonl(
            ["", json.dumps(record.to_dict()), "", ""]
        )
        loaded = load_export_records(path)
        assert len(loaded) == 1

    def test_comment_lines_skipped(self):
        record = _make_record()
        path = self._write_jsonl(
            ["# This is a comment", json.dumps(record.to_dict())]
        )
        loaded = load_export_records(path)
        assert len(loaded) == 1

    def test_empty_file_returns_empty_list(self):
        path = self._write_jsonl([])
        loaded = load_export_records(path)
        assert loaded == []

    def test_malformed_json_raises(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        tmp.write("{bad json}\n")
        tmp.close()
        with pytest.raises(Exception):  # json.JSONDecodeError
            load_export_records(tmp.name)


# ---------------------------------------------------------------------------
# build_prompt_args — historical now, profile, memories, history, voice
# ---------------------------------------------------------------------------


class TestBuildPromptArgs:
    """build_prompt_args must produce a PromptBuildArgs with historical now."""

    def _call(self, record: ReplayExportRecord) -> object:
        return build_prompt_args(
            record,
            tool_specs=(),
            composer_template_ids=(),
            composer_llm_prompt_keys=(),
            few_shot_block="",
        )

    def test_returns_prompt_build_args(self):
        from sreda.runtime.planner.prompt_builder import PromptBuildArgs

        record = _make_record()
        args = self._call(record)
        assert isinstance(args, PromptBuildArgs)

    def test_now_is_historical_not_current(self):
        """now must come from now_iso, not datetime.now()."""
        record = _make_record(now_iso="2026-05-10T14:23:00+03:00")
        args = self._call(record)
        # The historical datetime (UTC+3 offset → naive or aware)
        historical = datetime.fromisoformat("2026-05-10T14:23:00+03:00")
        # Compare without timezone for robustness
        assert args.now.now.replace(tzinfo=None) == historical.replace(tzinfo=None)

    def test_now_timezone_name(self):
        record = _make_record(timezone_name="Asia/Yekaterinburg")
        args = self._call(record)
        assert args.now.timezone_name == "Asia/Yekaterinburg"

    def test_user_message_passed_through(self):
        record = _make_record(user_message="Напомни купить молоко")
        args = self._call(record)
        assert args.user_message == "Напомни купить молоко"

    def test_profile_mapped_correctly(self):
        record = _make_record(
            profile={
                "name": "Тест",
                "gender": "F",
                "address": "вы",
                "timezone": "Europe/Moscow",
                "language": "ru",
            }
        )
        args = self._call(record)
        assert args.profile.name == "Тест"
        assert args.profile.gender == "F"
        assert args.profile.address == "вы"

    def test_partial_profile_uses_defaults(self):
        record = _make_record(profile={"name": "Борис"})
        args = self._call(record)
        assert args.profile.name == "Борис"
        assert args.profile.gender == ""
        assert args.profile.timezone == "Europe/Moscow"
        assert args.profile.language == "ru"

    def test_empty_profile_uses_all_defaults(self):
        record = _make_record(profile={})
        args = self._call(record)
        assert args.profile.name == ""
        assert args.profile.timezone == "Europe/Moscow"

    def test_memories_mapped_correctly(self):
        record = _make_record(
            memories=[
                {
                    "content": "Любит шахматы",
                    "source": "memory:core",
                    "score": 0.92,
                    "saved_at": "2026-05-01",
                },
                {
                    "content": "Аллергия на орехи",
                    "source": "memory:episodic",
                    "score": 0.85,
                    "saved_at": "2026-04-15",
                },
            ]
        )
        args = self._call(record)
        assert len(args.memories) == 2
        assert args.memories[0].content == "Любит шахматы"
        assert args.memories[0].score == 0.92
        assert args.memories[1].source == "memory:episodic"

    def test_empty_memories(self):
        record = _make_record(memories=[])
        args = self._call(record)
        assert args.memories == ()

    def test_active_turn_mapped(self):
        record = _make_record()
        args = self._call(record)
        assert args.active_turn is not None
        assert args.active_turn.turn_id == "turn_<SMOKE_ACTIVE>"
        assert args.active_turn.is_active is True
        assert len(args.active_turn.messages) == 2
        assert args.active_turn.messages[0].role == "юзер"

    def test_no_active_turn(self):
        record = _make_record(
            history={"active_turn": None, "closed_turns": []}
        )
        args = self._call(record)
        assert args.active_turn is None

    def test_closed_turns_mapped(self):
        record = _make_record(
            history={
                "active_turn": None,
                "closed_turns": [
                    {
                        "turn_id": "turn_<SMOKE_CLOSED>",
                        "started_at": "2026-05-09T10:00:00+03:00",
                        "summary": "Обсудили меню",
                        "messages": [],
                        "is_active": False,
                    }
                ],
            }
        )
        args = self._call(record)
        assert len(args.closed_turns) == 1
        assert args.closed_turns[0].turn_id == "turn_<SMOKE_CLOSED>"
        assert args.closed_turns[0].summary == "Обсудили меню"

    def test_voice_meta_present_when_voice(self):
        record = _make_record(voice={"is_voice": True, "confidence": 0.87})
        args = self._call(record)
        assert args.voice_meta is not None
        assert args.voice_meta.is_voice is True
        assert abs(args.voice_meta.confidence - 0.87) < 1e-6

    def test_voice_meta_none_when_no_voice(self):
        record = _make_record(voice=None)
        args = self._call(record)
        assert args.voice_meta is None

    def test_tool_specs_passed_through(self):
        """The current registry is passed in, not taken from the record."""
        record = _make_record()
        # Use a sentinel tuple — build_prompt_args must pass it through unchanged.
        sentinel = ("tool_a", "tool_b")  # not real ToolSpec objects, just for contract test
        # We don't call build_prompt (which validates them); just check the field.
        args = build_prompt_args(
            record,
            tool_specs=sentinel,
            composer_template_ids=("tpl_1",),
            composer_llm_prompt_keys=("key_1",),
            few_shot_block="## Few-shot",
        )
        assert args.tool_specs == sentinel
        assert args.composer_template_ids == ("tpl_1",)
        assert args.few_shot_block == "## Few-shot"

    def test_now_is_not_current_time(self):
        """Critical: historical now must not be close to current system time."""
        record = _make_record(now_iso="2025-01-15T08:00:00+03:00")
        args = self._call(record)
        # The result must be far in the past, not near now.
        historical = datetime(2025, 1, 15, 8, 0, 0)
        assert args.now.now.replace(tzinfo=None) == historical


# ---------------------------------------------------------------------------
# reconstruction_fidelity + is_fidelity_ok (§4.3 gate)
# ---------------------------------------------------------------------------


class TestReconstructionFidelity:
    def test_all_ok_returns_ok_for_all_keys(self):
        record = _make_record(reconstruction_status=dict(_ALL_STATUS_OK))
        fidelity = reconstruction_fidelity(record)
        assert set(fidelity.keys()) == set(_ALL_STATUS_OK.keys())
        assert all(v == "ok" for v in fidelity.values())

    def test_missing_key_defaults_to_missing(self):
        status = {k: "ok" for k in _ALL_STATUS_OK}
        del status["baseline_alignment"]
        record = _make_record(reconstruction_status=status)
        fidelity = reconstruction_fidelity(record)
        assert fidelity["baseline_alignment"] == "missing"

    def test_degraded_value_preserved(self):
        status = dict(_ALL_STATUS_OK)
        status["profile"] = "degraded"
        record = _make_record(reconstruction_status=status)
        fidelity = reconstruction_fidelity(record)
        assert fidelity["profile"] == "degraded"

    def test_empty_reconstruction_status_all_missing(self):
        record = _make_record(reconstruction_status={})
        fidelity = reconstruction_fidelity(record)
        assert all(v == "missing" for v in fidelity.values())

    def test_fidelity_has_exactly_nine_keys(self):
        record = _make_record()
        fidelity = reconstruction_fidelity(record)
        expected_keys = {
            "user_text",
            "now_tz_locale",
            "profile",
            "memory_provenance",
            "history_window",
            "active_turn",
            "voice_meta",
            "baseline_alignment",
            "decrypted_ok",
        }
        assert set(fidelity.keys()) == expected_keys


class TestIsFidelityOk:
    def test_all_ok_passes(self):
        record = _make_record(reconstruction_status=dict(_ALL_STATUS_OK))
        assert is_fidelity_ok(record) is True

    def test_critical_user_text_missing_fails(self):
        status = dict(_ALL_STATUS_OK)
        status["user_text"] = "missing"
        record = _make_record(reconstruction_status=status)
        assert is_fidelity_ok(record) is False

    def test_critical_now_tz_locale_missing_fails(self):
        status = dict(_ALL_STATUS_OK)
        status["now_tz_locale"] = "missing"
        record = _make_record(reconstruction_status=status)
        assert is_fidelity_ok(record) is False

    def test_critical_baseline_alignment_missing_fails(self):
        status = dict(_ALL_STATUS_OK)
        status["baseline_alignment"] = "missing"
        record = _make_record(reconstruction_status=status)
        assert is_fidelity_ok(record) is False

    def test_critical_decrypted_ok_missing_fails(self):
        status = dict(_ALL_STATUS_OK)
        status["decrypted_ok"] = "missing"
        record = _make_record(reconstruction_status=status)
        assert is_fidelity_ok(record) is False

    def test_critical_field_degraded_fails(self):
        status = dict(_ALL_STATUS_OK)
        status["user_text"] = "degraded"
        record = _make_record(reconstruction_status=status)
        assert is_fidelity_ok(record) is False

    def test_non_critical_missing_still_passes(self):
        """Non-critical fields (profile, voice_meta, …) don't block the gate."""
        status = dict(_ALL_STATUS_OK)
        status["profile"] = "missing"
        status["voice_meta"] = "missing"
        status["memory_provenance"] = "degraded"
        record = _make_record(reconstruction_status=status)
        assert is_fidelity_ok(record) is True

    def test_empty_reconstruction_status_fails(self):
        record = _make_record(reconstruction_status={})
        assert is_fidelity_ok(record) is False

    def test_custom_critical_fields(self):
        """Callers can override the critical field set."""
        status = dict(_ALL_STATUS_OK)
        status["profile"] = "missing"  # normally non-critical
        record = _make_record(reconstruction_status=status)
        # With default critical fields: passes
        assert is_fidelity_ok(record) is True
        # With profile added to critical set: fails
        assert is_fidelity_ok(record, critical_fields=("profile",)) is False


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------


class TestLoadManifest:
    def _write_manifest(self, cases: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        for case in cases:
            tmp.write(json.dumps(case) + "\n")
        tmp.close()
        return Path(tmp.name)

    def _case_dict(
        self,
        turn_id_hash: str = _HASH_A,
        expected_intent: str = "read",
        **exp_overrides,
    ) -> dict:
        expectations = {
            "expected_intent": expected_intent,
            "memory_dependence": "independent",
            "current_memory_status": "not_applicable",
            "eligibility_reason": "test",
            "label_source": "manual",
            "labeler": "test",
            "confidence": 1.0,
        }
        expectations.update(exp_overrides)
        return {
            "turn_id_hash": turn_id_hash,
            "tenant_placeholder": "tenant_tg_<SMOKE>",
            "expectations": expectations,
        }

    def test_single_case_loaded(self):
        path = self._write_manifest([self._case_dict()])
        manifest = load_manifest(path)
        assert _HASH_A in manifest
        case = manifest[_HASH_A]
        assert case.turn_id_hash == _HASH_A
        assert case.expectations.expected_intent == "read"

    def test_multiple_cases_indexed_by_hash(self):
        path = self._write_manifest([
            self._case_dict(turn_id_hash=_HASH_A),
            self._case_dict(turn_id_hash=_HASH_B, expected_intent="write"),
        ])
        manifest = load_manifest(path)
        assert len(manifest) == 2
        assert manifest[_HASH_A].expectations.expected_intent == "read"
        assert manifest[_HASH_B].expectations.expected_intent == "write"

    def test_reconstruction_status_is_none(self):
        """Manifest does not carry reconstruction_status (populated by Phase B)."""
        path = self._write_manifest([self._case_dict()])
        manifest = load_manifest(path)
        assert manifest[_HASH_A].reconstruction_status is None

    def test_must_cover_loaded(self):
        path = self._write_manifest([
            self._case_dict(must_cover=True, incident_id="#59")
        ])
        manifest = load_manifest(path)
        exp = manifest[_HASH_A].expectations
        assert exp.must_cover is True
        assert exp.incident_id == "#59"

    def test_empty_manifest_returns_empty_dict(self):
        path = self._write_manifest([])
        manifest = load_manifest(path)
        assert manifest == {}

    def test_comment_and_blank_lines_skipped(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        tmp.write("# comment\n")
        tmp.write("\n")
        tmp.write(json.dumps(self._case_dict()) + "\n")
        tmp.close()
        manifest = load_manifest(tmp.name)
        assert len(manifest) == 1

    def test_memory_eligibility_loaded(self):
        path = self._write_manifest([
            self._case_dict(
                memory_dependence="dependent",
                current_memory_status="valid",
            )
        ])
        manifest = load_manifest(path)
        exp = manifest[_HASH_A].expectations
        assert exp.memory_dependence == "dependent"
        assert exp.current_memory_status == "valid"
        assert exp.memory_eligible is True

    def test_optional_expectation_fields_default(self):
        minimal = {
            "turn_id_hash": _HASH_A,
            "tenant_placeholder": "tenant_tg_<SMOKE>",
            "expectations": {"expected_intent": "smalltalk"},
        }
        path = self._write_manifest([minimal])
        manifest = load_manifest(path)
        exp = manifest[_HASH_A].expectations
        assert exp.required_outcomes == ()
        assert exp.acceptable_action_sets == ()
        assert exp.requires_clarification is False
        assert exp.must_cover is False
        assert exp.forbidden_actions == ()


# ---------------------------------------------------------------------------
# pair_with_gold
# ---------------------------------------------------------------------------


class TestPairWithGold:
    def test_matched_record_has_gold(self):
        record = _make_record(turn_id_hash=_HASH_A)
        gold = _make_gold_case(turn_id_hash=_HASH_A)
        manifest = {_HASH_A: gold}

        paired = pair_with_gold([record], manifest).cases
        assert len(paired) == 1
        assert paired[0].gold is gold
        assert paired[0].unmatched is False

    def test_unmatched_record_has_none_gold(self):
        record = _make_record(turn_id_hash=_HASH_A)
        manifest = {}  # no matching entry

        paired = pair_with_gold([record], manifest).cases
        assert len(paired) == 1
        assert paired[0].gold is None
        assert paired[0].unmatched is True

    def test_partial_match(self):
        r_a = _make_record(turn_id_hash=_HASH_A)
        r_b = _make_record(turn_id_hash=_HASH_B)
        gold_a = _make_gold_case(turn_id_hash=_HASH_A)
        manifest = {_HASH_A: gold_a}

        paired = pair_with_gold([r_a, r_b], manifest).cases
        assert paired[0].gold is gold_a
        assert paired[0].unmatched is False
        assert paired[1].gold is None
        assert paired[1].unmatched is True

    def test_order_preserved(self):
        r1 = _make_record(turn_id_hash=_HASH_A)
        r2 = _make_record(turn_id_hash=_HASH_B)
        g1 = _make_gold_case(turn_id_hash=_HASH_A)
        g2 = _make_gold_case(turn_id_hash=_HASH_B)
        manifest = {_HASH_A: g1, _HASH_B: g2}

        paired = pair_with_gold([r1, r2], manifest).cases
        assert paired[0].record is r1
        assert paired[1].record is r2

    def test_fidelity_populated(self):
        record = _make_record(reconstruction_status=dict(_ALL_STATUS_OK))
        paired = pair_with_gold([record], {}).cases
        assert paired[0].fidelity["user_text"] == "ok"

    def test_fidelity_ok_property(self):
        record = _make_record(reconstruction_status=dict(_ALL_STATUS_OK))
        paired = pair_with_gold([record], {}).cases
        assert paired[0].fidelity_ok is True

    def test_fidelity_ok_false_when_critical_missing(self):
        status = dict(_ALL_STATUS_OK)
        status["user_text"] = "missing"
        record = _make_record(reconstruction_status=status)
        paired = pair_with_gold([record], {}).cases
        assert paired[0].fidelity_ok is False

    def test_empty_inputs(self):
        paired = pair_with_gold([], {}).cases
        assert paired == []

    def test_reconstructed_case_is_not_frozen(self):
        """ReconstructedCase is a regular dataclass (mutable), not frozen."""
        record = _make_record()
        paired = pair_with_gold([record], {}).cases
        case = paired[0]
        # unmatched is set in __post_init__; verify it reflects gold=None
        assert case.unmatched is True


# ---------------------------------------------------------------------------
# Integration: end-to-end JSONL → load → pair → fidelity gate
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_pipeline_ok_record(self):
        """A complete ok record: load → pair → gate passes → build_prompt_args works."""
        record = _make_record()
        gold = _make_gold_case(turn_id_hash=_HASH_A)
        manifest = {_HASH_A: gold}

        paired = pair_with_gold([record], manifest).cases
        assert len(paired) == 1
        rc = paired[0]
        assert rc.fidelity_ok is True
        assert rc.gold is not None

        # build_prompt_args should succeed without error
        from sreda.runtime.planner.prompt_builder import PromptBuildArgs
        args = build_prompt_args(
            rc.record,
            tool_specs=(),
            composer_template_ids=(),
            composer_llm_prompt_keys=(),
            few_shot_block="",
        )
        assert isinstance(args, PromptBuildArgs)

    def test_full_pipeline_critical_field_missing_excluded(self):
        """Record with missing critical field: gate fails, must be excluded."""
        status = dict(_ALL_STATUS_OK)
        status["decrypted_ok"] = "missing"
        record = _make_record(reconstruction_status=status)
        paired = pair_with_gold([record], {}).cases
        rc = paired[0]
        assert rc.fidelity_ok is False
        # Caller should exclude this record from headline scoring.


# ---------------------------------------------------------------------------
# Codex review fixes: unmatched_gold / must_cover, locale fallback, naive now
# ---------------------------------------------------------------------------


class TestPairingResultUnmatchedGold:
    def test_unmatched_gold_reported(self):
        """A gold case absent from the export records is surfaced in unmatched_gold."""
        record = _make_record(turn_id_hash=_HASH_A)
        manifest = {
            _HASH_A: _make_gold_case(turn_id_hash=_HASH_A),
            _HASH_B: _make_gold_case(turn_id_hash=_HASH_B),  # NOT in records
        }
        result = pair_with_gold([record], manifest)
        assert len(result.cases) == 1
        assert [g.turn_id_hash for g in result.unmatched_gold] == [_HASH_B]
        assert result.missing_must_cover == []

    def test_missing_must_cover_reported(self):
        """A must_cover=True gold case absent from the export → missing_must_cover
        non-empty (runner must return Stage-0 NO DECISION)."""
        manifest = {
            _HASH_B: _make_gold_case(turn_id_hash=_HASH_B, must_cover=True),
        }
        result = pair_with_gold([], manifest)  # no records at all
        assert [g.turn_id_hash for g in result.missing_must_cover] == [_HASH_B]


class TestLocaleTzFallback:
    def test_empty_profile_uses_record_locale_and_tz(self):
        """Partial profile → ProfileSnapshot language/timezone fall back to the
        record-level locale/timezone_name, NOT hardcoded ru/Europe-Moscow."""
        record = _make_record(
            profile={},  # no language/timezone in profile
            locale="en",
            timezone_name="America/New_York",
        )
        args = build_prompt_args(
            record, tool_specs=(), composer_template_ids=(),
            composer_llm_prompt_keys=(), few_shot_block="",
        )
        assert args.profile.language == "en"
        assert args.profile.timezone == "America/New_York"


class TestNaiveNowRejected:
    def test_build_prompt_args_raises_on_naive_now(self):
        """A naive now_iso (no tz offset) must fail-closed in build_prompt_args."""
        record = _make_record(now_iso="2026-05-10T14:23:00")  # NO offset
        with pytest.raises(ValueError):
            build_prompt_args(
                record, tool_specs=(), composer_template_ids=(),
                composer_llm_prompt_keys=(), few_shot_block="",
            )

    def test_naive_now_downgrades_fidelity_even_if_exporter_says_ok(self):
        """Consumer-side validation: a naive now_iso downgrades now_tz_locale to
        'degraded', so the CRITICAL gate excludes the record regardless of the
        exporter's claimed 'ok'."""
        status = dict(_ALL_STATUS_OK)  # exporter (wrongly) claims everything ok
        record = _make_record(now_iso="2026-05-10T14:23:00", reconstruction_status=status)
        fidelity = reconstruction_fidelity(record)
        assert fidelity["now_tz_locale"] == "degraded"
        assert is_fidelity_ok(record) is False

    def test_blank_timezone_name_downgrades_fidelity(self):
        """R2: blank timezone_name → now_tz_locale degraded even if exporter says ok."""
        record = _make_record(timezone_name="", reconstruction_status=dict(_ALL_STATUS_OK))
        assert reconstruction_fidelity(record)["now_tz_locale"] == "degraded"
        assert is_fidelity_ok(record) is False

    def test_invalid_iana_timezone_downgrades_fidelity(self):
        """R2: an unknown IANA zone → now_tz_locale degraded."""
        record = _make_record(
            timezone_name="Mars/Olympus_Mons", reconstruction_status=dict(_ALL_STATUS_OK)
        )
        assert reconstruction_fidelity(record)["now_tz_locale"] == "degraded"
        assert is_fidelity_ok(record) is False

    def test_blank_locale_downgrades_fidelity(self):
        """R2: blank locale → now_tz_locale degraded."""
        record = _make_record(locale="", reconstruction_status=dict(_ALL_STATUS_OK))
        assert reconstruction_fidelity(record)["now_tz_locale"] == "degraded"
        assert is_fidelity_ok(record) is False

    def test_malformed_locale_downgrades_fidelity(self):
        """R3: a non-blank but INVALID locale ("not-a-locale" / "??") must still
        downgrade now_tz_locale — non-blank alone is insufficient."""
        for bad in ("not-a-locale", "??", "r"):
            record = _make_record(locale=bad, reconstruction_status=dict(_ALL_STATUS_OK))
            assert reconstruction_fidelity(record)["now_tz_locale"] == "degraded", bad
            assert is_fidelity_ok(record) is False

    def test_valid_bcp47_locales_stay_ok(self):
        """Control: well-formed locales (ru, ru-RU, en-US) keep now_tz_locale ok."""
        for good in ("ru", "ru-RU", "en-US"):
            record = _make_record(locale=good, reconstruction_status=dict(_ALL_STATUS_OK))
            assert reconstruction_fidelity(record)["now_tz_locale"] == "ok", good

    def test_valid_tz_and_locale_stays_ok(self):
        """Control: a complete, valid record keeps now_tz_locale ok."""
        record = _make_record(
            timezone_name="America/New_York", locale="en",
            reconstruction_status=dict(_ALL_STATUS_OK),
        )
        assert reconstruction_fidelity(record)["now_tz_locale"] == "ok"
