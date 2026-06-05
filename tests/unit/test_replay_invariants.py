"""Unit tests for scripts/replay/invariants.py (Phase A, issue #87).

Tests cover each of the 6 invariant checkers:
  #1 phantom_save       — pass / fail / unknown
  #2 unsolicited_write  — pass / fail / unknown
  #3 tool_loop          — pass / fail / unknown
  #4 imprecise_following— pass / fail
  #5 atomicity          — pass / fail / unknown
  #6 fabricated_state   — pass / fail / unknown

All fixtures use synthetic data (tenant_tg_<SMOKE>, no real chat ids).
No DB, no network, no LLM calls.

Note: detect_unbacked_claim (invariant #1) is mocked via monkeypatch so
these tests do not require the sreda package on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest

from invariants import (
    ActionRecord,
    InvariantResults,
    TurnRecord,
    check_all_invariants,
    check_atomicity,
    check_fabricated_state,
    check_imprecise_following,
    check_phantom_save,
    check_tool_loop,
    check_unsolicited_write,
)
from manifest import ReplayExpectations
from mock_tools import WriteRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exp(**kwargs) -> ReplayExpectations:
    defaults = {
        "expected_intent": "read",
        "memory_dependence": "independent",
        "current_memory_status": "not_applicable",
        "eligibility_reason": "test",
        "label_source": "manual",
        "labeler": "test",
        "confidence": 1.0,
    }
    defaults.update(kwargs)
    return ReplayExpectations(**defaults)


def _turn(
    *,
    user_message: str = "тест",
    new_reply: str = "",
    old_reply: str | None = None,
    new_actions: list | None = None,
    old_actions: list | None = None,
    tool_results_this_turn: set | None = None,
    plan_clarity: str | None = None,
    grounding_sources: list | None = None,
) -> TurnRecord:
    return TurnRecord(
        turn_id_hash="aabbccdd00112233",
        user_message=user_message,
        new_reply=new_reply,
        old_reply=old_reply,
        new_actions=new_actions or [],
        old_actions=old_actions,
        tool_results_this_turn=tool_results_this_turn or set(),
        plan_clarity=plan_clarity,
        grounding_sources=grounding_sources,
    )


def _action(
    tool_name: str,
    args: dict | None = None,
    raw_output: str | None = None,
    write_target: str | None = None,
) -> ActionRecord:
    return ActionRecord(
        tool_name=tool_name,
        args=args or {},
        raw_output=raw_output,
        write_target=write_target,
    )


def _write_rec(tool_name: str, write_target: str, payload: dict | None = None) -> WriteRecord:
    return WriteRecord(
        tool_name=tool_name,
        write_targets=(write_target,),
        payload=payload or {},
        authorized=True,  # explicit True for fixture convenience (overrides default False)
    )


# ---------------------------------------------------------------------------
# Invariant #1 — Phantom save
# ---------------------------------------------------------------------------


class TestPhantomSave:
    """Invariant #1: phantom memory-save."""

    def _run(
        self,
        reply: str,
        write_records=None,
        called_tools=None,
        detector_result=None,
        allowed_write_targets=(),
    ):
        """Helper that mocks detect_unbacked_claim."""
        if detector_result is None:
            detector_result = False
        new_actions = []
        if called_tools:
            new_actions = [_action(t) for t in called_tools]
        turn = _turn(new_reply=reply, new_actions=new_actions)
        exp = _exp(
            expected_intent="write" if allowed_write_targets else "read",
            allowed_write_targets=allowed_write_targets,
        )

        with patch(
            "invariants._try_detect_unbacked_claim",
            return_value=detector_result,
        ):
            return check_phantom_save(turn, exp, new_write_records=write_records)

    def test_pass_no_claim(self):
        """No save-claim verb → detector not firing → pass."""
        verdict = self._run(
            reply="Вот список покупок.",
            write_records=[],
            detector_result=False,
        )
        assert verdict == "pass"

    def test_ambiguous_verb_without_memory_object_not_a_save_claim(self):
        """R4 fix (overbroad): an ambiguous save/add verb WITHOUT a memory/fact
        object ("учтено, добавила в список") is NOT a memory-save claim — it must
        NOT be routed into the memory gate. Shopping write + detector clean → pass."""
        rec = _write_rec("add_shopping_items", "shopping", {"items": ["молоко"]})
        verdict = self._run(
            reply="Учтено, добавила в список молоко.",
            write_records=[rec],
            detector_result=False,  # detector clean → no-save-claim path → pass
            allowed_write_targets=("shopping",),
        )
        assert verdict == "pass"

    def test_pass_claim_backed_by_memory_write(self):
        """Save-claim + authorized memory write whose PAYLOAD backs the claim → pass."""
        # Payload value must structurally appear in the claim (R2 fix: non-empty
        # alone is insufficient).
        rec = _write_rec("save_core_fact", "memory", {"content": "Борис любит шахматы"})
        verdict = self._run(
            reply="Сохранила в память: Борис любит шахматы.",
            write_records=[rec],
            detector_result=True,  # detector fires (but save-claim drives the check now)
            allowed_write_targets=("memory",),  # memory is authorized per gold
        )
        assert verdict == "pass"

    def test_unknown_save_claim_payload_does_not_back(self):
        """Save-claim + authorized memory write but payload does NOT overlap the
        claim → cannot confirm backing in Phase A → unknown (NOT a false pass)."""
        rec = _write_rec("save_core_fact", "memory", {"content": "молоко 2 литра"})
        verdict = self._run(
            reply="Сохранила в память: Борис любит шахматы.",  # claim about chess, payload about milk
            write_records=[rec],
            detector_result=True,
            allowed_write_targets=("memory",),
        )
        assert verdict == "unknown"

    def test_fail_save_claim_no_backing_write(self):
        """Save-claim + no memory write → fail (phantom save)."""
        verdict = self._run(
            reply="Запомнила, что Борис любит шахматы.",
            write_records=[],
            detector_result=True,
        )
        assert verdict == "fail"

    def test_fail_save_claim_wrong_write_target(self):
        """Save-claim + shopping write (not memory) → fail (not backed by memory write)."""
        rec = _write_rec("add_shopping_items", "shopping")
        verdict = self._run(
            reply="Сохранила факт.",
            write_records=[rec],
            detector_result=True,
            allowed_write_targets=("memory",),  # memory allowed but shopping write happened
        )
        assert verdict == "fail"

    def test_unknown_when_detector_unavailable(self):
        """NON-save-claim reply: if detect_unbacked_claim returns None (import
        error) → unknown. (Save-claims no longer consult the detector — R2 fix —
        so this path is exercised with a non-save-claim reply.)"""
        turn = _turn(new_reply="Готово.")  # no save-claim verb → detector path
        exp = _exp()
        with patch("invariants._try_detect_unbacked_claim", return_value=None):
            assert check_phantom_save(turn, exp, new_write_records=[]) == "unknown"

    def test_unknown_when_no_records_provided(self):
        """save-claim verb found but write_records=None → unknown."""
        turn = _turn(new_reply="Запомнила.")
        exp = _exp()
        with patch("invariants._try_detect_unbacked_claim", return_value=True):
            assert check_phantom_save(turn, exp, new_write_records=None) == "unknown"

    def test_unknown_empty_reply(self):
        turn = _turn(new_reply="")
        exp = _exp()
        with patch("invariants._try_detect_unbacked_claim", return_value=False):
            assert check_phantom_save(turn, exp) == "unknown"


# ---------------------------------------------------------------------------
# Invariant #2 — Unsolicited write
# ---------------------------------------------------------------------------


class TestUnsolicitedWrite:
    """Invariant #2: unsolicited write on read-intent turns."""

    def test_pass_read_intent_no_writes(self):
        turn = _turn()
        exp = _exp(expected_intent="read", forbidden_write_targets=("memory", "shopping"))
        assert check_unsolicited_write(turn, exp, new_write_records=[]) == "pass"

    def test_pass_write_intent_with_write(self):
        """Write intent + memory write = fine (not a read-intent turn)."""
        rec = _write_rec("save_core_fact", "memory")
        turn = _turn()
        exp = _exp(expected_intent="write", forbidden_write_targets=())
        assert check_unsolicited_write(turn, exp, new_write_records=[rec]) == "pass"

    def test_fail_read_intent_writes_to_forbidden_target(self):
        rec = _write_rec("save_core_fact", "memory")
        turn = _turn()
        exp = _exp(expected_intent="read", forbidden_write_targets=("memory",))
        assert check_unsolicited_write(turn, exp, new_write_records=[rec]) == "fail"

    def test_fail_read_intent_writes_to_one_of_many_forbidden(self):
        rec = _write_rec("add_shopping_items", "shopping")
        turn = _turn()
        exp = _exp(
            expected_intent="read",
            forbidden_write_targets=("memory", "shopping", "reminders"),
        )
        assert check_unsolicited_write(turn, exp, new_write_records=[rec]) == "fail"

    def test_unknown_when_records_none(self):
        turn = _turn()
        exp = _exp(expected_intent="read", forbidden_write_targets=("memory",))
        assert check_unsolicited_write(turn, exp, new_write_records=None) == "unknown"

    def test_pass_read_intent_allowed_write_target_only(self):
        """Write to a target NOT in forbidden list = not a violation."""
        rec = _write_rec("save_core_fact", "memory")
        turn = _turn()
        exp = _exp(
            expected_intent="read",
            allowed_write_targets=("memory",),
            forbidden_write_targets=("shopping",),  # memory NOT forbidden
        )
        assert check_unsolicited_write(turn, exp, new_write_records=[rec]) == "pass"

    def test_pass_no_forbidden_targets_declared(self):
        """No forbidden_write_targets → cannot fire."""
        rec = _write_rec("save_core_fact", "memory")
        turn = _turn()
        exp = _exp(expected_intent="read", forbidden_write_targets=())
        assert check_unsolicited_write(turn, exp, new_write_records=[rec]) == "pass"


# ---------------------------------------------------------------------------
# Invariant #3 — Tool loop
# ---------------------------------------------------------------------------


class TestToolLoop:
    """Invariant #3: duplicate step detection."""

    def test_pass_no_actions(self):
        turn = _turn(new_actions=[])
        assert check_tool_loop(turn, _exp()) == "pass"

    def test_pass_distinct_tools(self):
        actions = [
            _action("add_shopping_items", {"items": ["milk"]}, raw_output="ok:added:1"),
            _action("list_shopping", {}, raw_output="1 item"),
        ]
        turn = _turn(new_actions=actions)
        assert check_tool_loop(turn, _exp()) == "pass"

    def test_pass_same_tool_different_args(self):
        """Same tool but different args = not a duplicate."""
        actions = [
            _action("add_shopping_items", {"items": ["milk"]}, raw_output="ok:added:1"),
            _action("add_shopping_items", {"items": ["bread"]}, raw_output="ok:added:1"),
        ]
        turn = _turn(new_actions=actions)
        assert check_tool_loop(turn, _exp()) == "pass"

    def test_fail_exact_duplicate_no_progress(self):
        """Same tool + same args + no intervening progress = loop."""
        step = _action("search_recipes", {"query": "суп"}, raw_output="")
        turn = _turn(new_actions=[step, step])
        assert check_tool_loop(turn, _exp()) == "fail"

    def test_pass_same_signature_with_progress_between(self):
        """Same signature but intervening step produced output = progress, OK."""
        step_a = _action("search_recipes", {"query": "суп"}, raw_output="")
        progress = _action("get_recipe", {"id": "rec_001"}, raw_output="recipe data")
        step_b = _action("search_recipes", {"query": "суп"}, raw_output="")
        turn = _turn(new_actions=[step_a, progress, step_b])
        assert check_tool_loop(turn, _exp()) == "pass"

    def test_fail_too_many_steps(self):
        """More than 2× default max_iters = loop."""
        actions = [_action(f"tool_{i}", {}) for i in range(20)]
        turn = _turn(new_actions=actions)
        assert check_tool_loop(turn, _exp()) == "fail"


# ---------------------------------------------------------------------------
# Invariant #4 — Imprecise following
# ---------------------------------------------------------------------------


class TestImpreciseFollowing:
    """Invariant #4: outcome-level action grading."""

    def test_pass_no_constraints(self):
        """No acceptable_action_sets, no forbidden → always pass."""
        turn = _turn(new_actions=[_action("list_shopping")])
        exp = _exp(
            expected_intent="read",
            acceptable_action_sets=(),
            forbidden_actions=(),
        )
        assert check_imprecise_following(turn, exp) == "pass"

    def test_pass_domain_action_in_acceptable_set(self):
        """New plan uses shopping:write → matches acceptable set."""
        turn = _turn(new_actions=[_action("add_shopping_items", write_target="shopping")])
        exp = _exp(
            expected_intent="write",
            acceptable_action_sets=(("shopping:write",),),
        )
        assert check_imprecise_following(turn, exp) == "pass"

    def test_fail_no_acceptable_set_satisfied(self):
        """New plan reads shopping, but gold expects write."""
        turn = _turn(new_actions=[_action("list_shopping")])
        exp = _exp(
            expected_intent="write",
            required_outcomes=("item added to list",),
            acceptable_action_sets=(("shopping:write",),),
        )
        assert check_imprecise_following(turn, exp) == "fail"

    def test_fail_forbidden_action_present(self):
        """New plan calls a forbidden domain-action."""
        turn = _turn(new_actions=[_action("save_core_fact", write_target="memory")])
        exp = _exp(
            expected_intent="read",
            forbidden_actions=("memory:write",),
        )
        assert check_imprecise_following(turn, exp) == "fail"

    def test_pass_multiple_acceptable_sets_one_matches(self):
        """At least one acceptable set must be satisfied."""
        turn = _turn(new_actions=[_action("list_shopping")])
        exp = _exp(
            expected_intent="read",
            acceptable_action_sets=(
                ("shopping:write",),   # not satisfied
                ("shopping:read",),    # satisfied
            ),
        )
        assert check_imprecise_following(turn, exp) == "pass"

    def test_pass_empty_required_outcomes_with_no_acceptable_sets(self):
        """No required_outcomes and no acceptable_action_sets → pass."""
        turn = _turn(new_actions=[])
        exp = _exp(
            expected_intent="smalltalk",
            required_outcomes=(),
            acceptable_action_sets=(),
        )
        assert check_imprecise_following(turn, exp) == "pass"


# ---------------------------------------------------------------------------
# Invariant #5 — Atomicity
# ---------------------------------------------------------------------------


class TestAtomicity:
    """Invariant #5: clarification contract."""

    def test_pass_no_clarification_required(self):
        turn = _turn(new_reply="Добавила молоко.", new_actions=[_action("add_shopping_items", write_target="shopping")])
        exp = _exp(requires_clarification=False)
        assert check_atomicity(turn, exp) == "pass"

    def test_pass_clarification_required_and_question_present_no_writes(self):
        turn = _turn(new_reply="Какой тип молока? Сколько?", new_actions=[])
        exp = _exp(requires_clarification=True)
        assert check_atomicity(turn, exp) == "pass"

    def test_fail_clarification_required_but_silent_partial(self):
        """Clarification needed but plan wrote something without asking."""
        turn = _turn(
            new_reply="Добавила молоко.",
            new_actions=[_action("add_shopping_items", write_target="shopping")],
        )
        exp = _exp(requires_clarification=True)
        assert check_atomicity(turn, exp) == "fail"

    def test_unknown_clarification_required_no_question_no_write(self):
        """No question and no write — could be a text refusal → unknown."""
        turn = _turn(new_reply="Я не могу это сделать.", new_actions=[])
        exp = _exp(requires_clarification=True)
        assert check_atomicity(turn, exp) == "unknown"

    def test_pass_clarification_not_required_regardless_of_reply(self):
        turn = _turn(new_reply="Добавила!")
        exp = _exp(requires_clarification=False)
        assert check_atomicity(turn, exp) == "pass"


# ---------------------------------------------------------------------------
# Invariant #6 — Fabricated state
# ---------------------------------------------------------------------------


class TestFabricatedState:
    """Invariant #6: reply must be grounded in user message + tool results."""

    def test_unknown_when_old_actions_none(self):
        """Missing old trace → unknown (cannot grade)."""
        turn = _turn(new_reply="У вас 42 задачи.", old_actions=None)
        assert check_fabricated_state(turn, _exp()) == "unknown"

    def test_unknown_empty_reply(self):
        turn = _turn(new_reply="", old_actions=[])
        assert check_fabricated_state(turn, _exp()) == "unknown"

    def test_pass_number_in_reply_is_in_user_message(self):
        """Number comes from user message → grounded → pass."""
        turn = _turn(
            user_message="Добавь 42 молока",
            new_reply="Добавила 42 молока.",
            old_actions=[],  # trace present but empty
        )
        assert check_fabricated_state(turn, _exp()) == "pass"

    def test_pass_number_in_reply_is_in_tool_output(self):
        """Number comes from tool output → grounded → pass."""
        action = _action("list_shopping", raw_output="ok:42 items")
        turn = _turn(
            user_message="что в списке?",
            new_reply="У вас 42 товара.",
            new_actions=[action],
            old_actions=[],
        )
        assert check_fabricated_state(turn, _exp()) == "pass"

    def test_fail_unsupported_number_in_reply(self):
        """Number in reply not in user message or tool output → fabricated."""
        turn = _turn(
            user_message="что в списке?",
            new_reply="У вас 99 товаров.",
            new_actions=[_action("list_shopping", raw_output="ok:items")],
            old_actions=[],
        )
        assert check_fabricated_state(turn, _exp()) == "fail"


# ---------------------------------------------------------------------------
# check_all_invariants — consolidated runner
# ---------------------------------------------------------------------------


class TestCheckAllInvariants:
    def test_returns_invariant_results_instance(self):
        turn = _turn(new_reply="Готово!", old_actions=[])
        exp = _exp()
        with patch("invariants._try_detect_unbacked_claim", return_value=False):
            result = check_all_invariants(turn, exp, new_write_records=[])
        assert isinstance(result, InvariantResults)

    def test_any_fail_false_when_all_pass(self):
        turn = _turn(new_reply="Готово.", old_actions=[])
        exp = _exp(expected_intent="read", forbidden_write_targets=())
        with patch("invariants._try_detect_unbacked_claim", return_value=False):
            result = check_all_invariants(turn, exp, new_write_records=[])
        assert result.any_fail() is False

    def test_any_fail_true_when_unsolicited_write(self):
        rec = _write_rec("save_core_fact", "memory")
        turn = _turn(
            new_reply="Сохранила.",
            new_actions=[_action("save_core_fact", write_target="memory")],
            old_actions=[],
        )
        exp = _exp(expected_intent="read", forbidden_write_targets=("memory",))
        with patch("invariants._try_detect_unbacked_claim", return_value=True):
            result = check_all_invariants(turn, exp, new_write_records=[rec])
        assert result.any_fail() is True

    def test_as_dict_has_all_six_keys(self):
        turn = _turn()
        exp = _exp()
        with patch("invariants._try_detect_unbacked_claim", return_value=False):
            result = check_all_invariants(turn, exp, new_write_records=[])
        d = result.as_dict()
        assert set(d.keys()) == {
            "phantom_save",
            "unsolicited_write",
            "tool_loop",
            "imprecise_following",
            "atomicity",
            "fabricated_state",
        }


# ---------------------------------------------------------------------------
# Finding 3+4 (MAJOR): Phantom-save authorization from gold + payload backing
# ---------------------------------------------------------------------------


class TestPhantomSaveAuthAndPayload:
    """Authorization is now computed from frozen gold expectations, not hardcoded."""

    def _write_rec_multi(
        self,
        tool_name: str,
        write_targets: tuple,
        payload: dict | None = None,
    ):
        """Build a WriteRecord with write_targets tuple (new API)."""
        from mock_tools import WriteRecord
        rec = WriteRecord.__new__(WriteRecord)
        rec.tool_name = tool_name
        rec.write_targets = write_targets
        rec.write_target = write_targets[0] if write_targets else "unknown"
        rec.payload = dict(payload or {})
        rec.authorized = False  # recorder always sets False; auth from gold
        rec.raw_args = dict(payload or {})
        return rec

    def test_pass_when_target_in_allowed_write_targets_with_payload(self):
        """Write to "memory" in allowed_write_targets + payload that BACKS the claim → pass."""
        rec = self._write_rec_multi(
            "save_core_fact", ("memory",), {"content": "Борис любит шахматы"}
        )
        turn = _turn(new_reply="Сохранила в память: Борис любит шахматы.")
        exp = _exp(
            expected_intent="write",
            allowed_write_targets=("memory",),
        )
        with patch("invariants._try_detect_unbacked_claim", return_value=True):
            result = check_phantom_save(turn, exp, new_write_records=[rec])
        assert result == "pass"

    def test_fail_when_target_not_in_allowed_write_targets(self):
        """Memory write but "memory" not in allowed_write_targets → unauthorized → fail."""
        rec = self._write_rec_multi("save_core_fact", ("memory",), {"content": "chess"})
        turn = _turn(new_reply="Сохранила факт.")
        exp = _exp(
            expected_intent="read",
            allowed_write_targets=(),  # memory not allowed
        )
        with patch("invariants._try_detect_unbacked_claim", return_value=True):
            result = check_phantom_save(turn, exp, new_write_records=[rec])
        assert result == "fail"

    def test_fail_when_backing_write_has_empty_payload(self):
        """Authorized memory write but empty payload → no backing → fail."""
        rec = self._write_rec_multi("save_core_fact", ("memory",), {})
        turn = _turn(new_reply="Сохранила: важный факт.")
        exp = _exp(
            expected_intent="write",
            allowed_write_targets=("memory",),
        )
        with patch("invariants._try_detect_unbacked_claim", return_value=True):
            result = check_phantom_save(turn, exp, new_write_records=[rec])
        assert result == "fail"

    def test_fail_old_also_fail_read_turn_save_claim_no_backing(self):
        """old=FAIL/new=FAIL on read turn: save claim with no authorized write → fail."""
        turn = _turn(new_reply="Запомнила ваш запрос.")
        exp = _exp(
            expected_intent="read",
            allowed_write_targets=(),  # no writes allowed on read turn
        )
        with patch("invariants._try_detect_unbacked_claim", return_value=True):
            result = check_phantom_save(turn, exp, new_write_records=[])
        assert result == "fail"

    def test_passive_save_claim_routes_through_gate_even_if_detector_clean(self):
        """R3 fix: a PASSIVE save-claim ("Сохранено …") must route through the
        auth+payload gate even when detect_unbacked_claim reports CLEAN — it must
        NOT slip down the no-save-claim fast path to a tool-name-only pass.
        Here: unauthorized memory write (memory not in allowed) → fail."""
        rec = self._write_rec_multi("save_core_fact", ("memory",), {"content": "x"})
        turn = _turn(new_reply="Сохранено в память: важный факт.")
        exp = _exp(expected_intent="read", allowed_write_targets=())  # memory NOT allowed
        with patch("invariants._try_detect_unbacked_claim", return_value=False):  # CLEAN
            result = check_phantom_save(turn, exp, new_write_records=[rec])
        assert result == "fail"

    def test_action_arm_unauthorized_memory_write_with_NO_claim_text_fails(self):
        """R5 fix (structural): an UNAUTHORIZED memory write that was RECORDED
        must FAIL even when the reply contains NO save-claim wording at all and
        the detector is clean. The authorization arm is action-driven, so the
        verdict does NOT depend on the text classifier matching every phrasing."""
        rec = self._write_rec_multi("save_core_fact", ("memory",), {"content": "x"})
        turn = _turn(new_reply="Готово.")  # no save-claim wording whatsoever
        exp = _exp(expected_intent="read", allowed_write_targets=())  # memory NOT allowed
        with patch("invariants._try_detect_unbacked_claim", return_value=False):  # CLEAN
            result = check_phantom_save(turn, exp, new_write_records=[rec])
        assert result == "fail"

    def test_negated_memory_denial_is_not_phantom(self):
        """R5 fix (negation): a DENIAL ("Не сохранила в память") with no memory
        write is NOT a phantom save-claim → must not falsely fail."""
        turn = _turn(new_reply="Не сохранила в память — ты не просил.")
        exp = _exp(expected_intent="read", allowed_write_targets=())
        with patch("invariants._try_detect_unbacked_claim", return_value=False):  # clean
            result = check_phantom_save(turn, exp, new_write_records=[])
        assert result == "pass"

    def test_fail_closed_when_records_missing_no_claim_clean_detector(self):
        """R7 fix: with new_write_records=None (recorder data missing/miswired),
        invariant #1 must FAIL CLOSED → "unknown", even for a no-save-claim reply
        with a clean detector (must NOT reach "pass")."""
        turn = _turn(new_reply="Готово.")  # no save-claim
        exp = _exp(expected_intent="read", allowed_write_targets=())
        with patch("invariants._try_detect_unbacked_claim", return_value=False):  # clean
            result = check_phantom_save(turn, exp, new_write_records=None)
        assert result == "unknown"

    def test_future_form_save_claim_unauthorized_fails(self):
        """R5 fix (underbroad): future-stem claim "Запишу в память…" is a memory
        save-claim; unauthorized memory write → fail."""
        rec = self._write_rec_multi("save_core_fact", ("memory",), {"content": "шахматы"})
        turn = _turn(new_reply="Запишу в память: Борис любит шахматы.")
        exp = _exp(expected_intent="read", allowed_write_targets=())  # memory NOT allowed
        with patch("invariants._try_detect_unbacked_claim", return_value=False):
            result = check_phantom_save(turn, exp, new_write_records=[rec])
        assert result == "fail"


# ---------------------------------------------------------------------------
# Finding 6 (MAJOR): Multi-domain writes — invariant #2 checks all targets
# ---------------------------------------------------------------------------


class TestUnsolicitedWriteMultiDomain:
    """Invariant #2 must check ALL write_targets, not just the primary."""

    def _multi_target_rec(self, tool_name: str, targets: tuple) -> "WriteRecord":
        from mock_tools import WriteRecord
        rec = WriteRecord.__new__(WriteRecord)
        rec.tool_name = tool_name
        rec.write_targets = targets
        rec.write_target = targets[0] if targets else "unknown"
        rec.payload = {}
        rec.authorized = False
        rec.raw_args = {}
        return rec

    def test_fail_when_secondary_domain_is_forbidden(self):
        """attach_reminder writes tasks+reminders; if reminders is forbidden, must FAIL."""
        rec = self._multi_target_rec("attach_reminder", ("tasks", "reminders"))
        turn = _turn()
        exp = _exp(
            expected_intent="read",
            forbidden_write_targets=("reminders",),  # secondary domain forbidden
        )
        result = check_unsolicited_write(turn, exp, new_write_records=[rec])
        assert result == "fail"

    def test_fail_when_primary_domain_is_forbidden(self):
        """Primary domain also catches forbidden target."""
        rec = self._multi_target_rec("save_core_fact", ("memory",))
        turn = _turn()
        exp = _exp(expected_intent="read", forbidden_write_targets=("memory",))
        result = check_unsolicited_write(turn, exp, new_write_records=[rec])
        assert result == "fail"

    def test_pass_when_no_domain_is_forbidden(self):
        """Multi-domain tool but none of its domains is forbidden → pass."""
        rec = self._multi_target_rec("attach_reminder", ("tasks", "reminders"))
        turn = _turn()
        exp = _exp(
            expected_intent="read",
            forbidden_write_targets=("memory", "shopping"),  # neither tasks nor reminders
        )
        result = check_unsolicited_write(turn, exp, new_write_records=[rec])
        assert result == "pass"


# ---------------------------------------------------------------------------
# Finding 7 (MAJOR): build_replay_tools_by_name factory / fail-fast guard
# ---------------------------------------------------------------------------


class TestBuildReplayToolsByName:
    """Integrated factory builds complete tool map; any un-mocked write raises."""

    def test_factory_produces_complete_tool_map(self):
        """build_replay_tools_by_name returns a dict covering all write + read tools.
        Uses the DEFAULT full taxonomy so every authoritative write tool is
        recorder-backed (R2 fix: a partial taxonomy is now rejected — see
        test_factory_rejects_stale_taxonomy)."""
        from mock_tools import ReplayWriteRecorder, build_replay_tools_by_name

        recorder = ReplayWriteRecorder()  # default = full authoritative taxonomy
        all_tools = build_replay_tools_by_name(recorder)

        # Write tools are recorder-backed
        assert "save_core_fact" in all_tools
        assert "add_shopping_items" in all_tools
        # Read tools are also present
        assert "list_shopping" in all_tools
        assert "recall_memory" in all_tools

    def test_factory_write_tools_record_calls(self):
        """Write tools returned by the factory record to the recorder."""
        from mock_tools import ReplayWriteRecorder, build_replay_tools_by_name

        recorder = ReplayWriteRecorder()  # default = full authoritative taxonomy
        all_tools = build_replay_tools_by_name(recorder)

        all_tools["save_core_fact"].invoke({"content": "test"})
        assert len(recorder.get_records()) == 1
        assert recorder.get_records()[0].tool_name == "save_core_fact"

    def test_factory_rejects_stale_taxonomy(self):
        """R2 fix: a recorder whose taxonomy MISSES authoritative write tools is
        rejected (AssertionError) — prevents silent unrecorded-write leakage."""
        import pytest
        from mock_tools import ReplayWriteRecorder, build_replay_tools_by_name

        # Partial taxonomy: only one write tool, omitting the rest.
        recorder = ReplayWriteRecorder(write_tool_domains={"save_core_fact": ["memory"]})
        with pytest.raises(AssertionError) as exc:
            build_replay_tools_by_name(recorder)
        assert "NOT recorder-backed" in str(exc.value)

    def test_factory_rejects_extra_read_tool_that_is_write(self):
        """R2 fix: injecting a WRITE tool via extra_read_tools is rejected."""
        import pytest
        from mock_tools import ReplayWriteRecorder, build_replay_tools_by_name

        recorder = ReplayWriteRecorder()
        with pytest.raises(AssertionError) as exc:
            build_replay_tools_by_name(
                recorder, extra_read_tools={"add_shopping_items": object()}
            )
        assert "write tool" in str(exc.value).lower()

    def test_stray_real_write_tool_raises(self):
        """A write tool not covered by the recorder raises ReplayWriteAttempted."""
        import pytest
        from mock_tools import (
            ReplayWriteAttempted,
            ReplayWriteRecorder,
            _FailFastRealWriteTool,
        )

        guard = _FailFastRealWriteTool("some_real_write_tool")
        with pytest.raises(ReplayWriteAttempted) as exc_info:
            guard.invoke({})
        assert "some_real_write_tool" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Finding 8 (MAJOR): Phase-B-ready fields — intent_group, plan_clarity,
#                     grounding_sources
# ---------------------------------------------------------------------------


class TestPhaseB_ReadyFields:
    """Verify intent_group, plan_clarity, grounding_sources wire into checkers."""

    # --- intent_group in check_tool_loop ---

    def test_intent_group_limits_per_class(self):
        """Actions with intent_group="reminder" must not exceed _max_iters_for("reminder")=3."""
        from invariants import _MAX_ITERS
        limit = _MAX_ITERS["reminder"]  # 3
        # Create limit+1 reminder actions with distinct args (no dup-step trigger)
        actions = [
            _action(f"schedule_reminder", args={"text": f"r{i}", "when": "12:00"})
            for i in range(limit + 1)
        ]
        for a in actions:
            a.intent_group = "reminder"
        turn = _turn(new_actions=actions)
        result = check_tool_loop(turn, _exp())
        assert result == "fail"

    def test_intent_group_within_limit_passes(self):
        """Exactly max_iters reminder actions → pass."""
        from invariants import _MAX_ITERS
        limit = _MAX_ITERS["reminder"]  # 3
        actions = [
            _action(f"schedule_reminder", args={"text": f"r{i}", "when": "12:00"})
            for i in range(limit)
        ]
        for a in actions:
            a.intent_group = "reminder"
        turn = _turn(new_actions=actions)
        result = check_tool_loop(turn, _exp())
        assert result == "pass"

    # --- plan_clarity in check_atomicity ---

    def test_plan_clarity_needs_clarification_passes(self):
        """plan_clarity='needs_clarification' passes invariant #5."""
        turn = _turn(
            new_reply="Какой тип напитка?",
            new_actions=[],
            plan_clarity="needs_clarification",
        )
        exp = _exp(requires_clarification=True)
        assert check_atomicity(turn, exp) == "pass"

    def test_plan_clarity_other_value_with_write_fails(self):
        """plan_clarity set but not 'needs_clarification', and wrote → fail."""
        turn = _turn(
            new_reply="Добавила.",
            new_actions=[_action("add_shopping_items", write_target="shopping")],
            plan_clarity="confirmed",  # not needs_clarification
        )
        exp = _exp(requires_clarification=True)
        assert check_atomicity(turn, exp) == "fail"

    # --- grounding_sources in check_fabricated_state ---

    def test_grounding_sources_used_when_provided(self):
        """When grounding_sources is set, it's used instead of rebuilding from actions."""
        turn = _turn(
            user_message="что в списке?",
            new_reply="У вас 42 товара.",
            new_actions=[],  # no actions with raw_output
            old_actions=[],
            grounding_sources=["у вас 42 товара в корзине"],  # 42 is in sources
        )
        assert check_fabricated_state(turn, _exp()) == "pass"

    def test_grounding_sources_fail_when_number_not_in_sources(self):
        """Number in reply not in grounding_sources → fabricated."""
        turn = _turn(
            user_message="что в списке?",
            new_reply="У вас 99 товаров.",
            new_actions=[],
            old_actions=[],
            grounding_sources=["пусто"],  # 99 not in sources
        )
        assert check_fabricated_state(turn, _exp()) == "fail"


# ---------------------------------------------------------------------------
# Finding 10 (MINOR): check_imprecise_following returns unknown for bad labels
# ---------------------------------------------------------------------------


class TestImpreciseFollowingBadLabels:
    def test_unknown_when_required_outcomes_nonempty_but_no_acceptable_sets(self):
        """required_outcomes present but acceptable_action_sets empty → unknown."""
        turn = _turn(new_actions=[_action("list_shopping")])
        exp = _exp(
            expected_intent="write",
            required_outcomes=("item added to list",),
            acceptable_action_sets=(),  # bad gold label: obligation but no grading path
        )
        result = check_imprecise_following(turn, exp)
        assert result == "unknown"

    def test_pass_when_both_empty(self):
        """Both required_outcomes and acceptable_action_sets empty → pass (unconstrained)."""
        turn = _turn(new_actions=[_action("list_shopping")])
        exp = _exp(
            expected_intent="smalltalk",
            required_outcomes=(),
            acceptable_action_sets=(),
        )
        result = check_imprecise_following(turn, exp)
        assert result == "pass"
