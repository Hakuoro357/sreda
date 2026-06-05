"""Unit tests for scripts/replay/probe_replay_export.py (Phase B, issue #87).

Tests cover ONLY the PURE assembly logic (``build_export_record`` + helpers)
with SYNTHETIC fixtures.  NO prod connection, NO real chat ids, NO DB, NO
network, NO decryption keys.

Covered:
- build_export_record produces a dict that round-trips through
  ReplayExportRecord.from_dict (the contract in reconstruct.py).
- turn_id hashing is opaque (no raw id leaks) and stable.
- missing tz/locale → now_tz_locale degraded; missing timestamp → missing.
- decrypt failure → decrypted_ok="missing" (export does NOT crash).
- missing baseline → baseline_alignment missing; heuristic pairing →
  degraded.
- voice vs text turns (voice modality preserved; unknown confidence →
  degraded).
- the safety guards (REPLAY_MODE / write-env / quarantine-inside-repo)
  refuse correctly with synthetic env dicts (no real env mutation).

Mirrors the style of tests/unit/test_replay_reconstruct.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Add scripts/replay to sys.path so we can import the export module.
_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest  # noqa: E402

import probe_replay_export as exp  # noqa: E402
from probe_replay_export import (  # noqa: E402
    ReplaySafetyError,
    _begin_read_only,
    _to_iso_aware,
    assert_no_write_env,
    assert_replay_mode,
    build_export_record,
    hash_turn_id,
    resolve_quarantine_dir,
    safe_output_path,
)
from reconstruct import ReplayExportRecord  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixture helpers — NO real chat ids, NO prod data.
# ---------------------------------------------------------------------------

_FAKE_TURN_ID = "run_synthetic_000000001"
_FAKE_TENANT_REAL = "tg_000000001"  # never a real chat id


def _ok_decrypt(value: str) -> str:
    """Stub decrypt: returns a JSON ``{"text": ...}`` payload as if the v2:
    string decrypted cleanly.  The fixture encodes the plaintext directly
    after the ``v2:fake:`` prefix so no real key is involved."""
    # Synthetic payloads are shaped "v2:fake:<plaintext-json>".
    if value.startswith("v2:fake:"):
        return value[len("v2:fake:"):]
    raise ValueError(f"unexpected fixture payload: {value!r}")


def _failing_decrypt(value: str) -> str:
    raise ValueError("simulated decrypt failure")


def _raw_turn(**overrides) -> dict:
    """A complete synthetic raw-row dict for build_export_record."""
    base = {
        "turn_id": _FAKE_TURN_ID,
        "input_json": {
            "source_type": "telegram_message",
            "source_value": "Что купить в магазине?",
            "params": {"text": "Что купить в магазине?"},
        },
        "created_at": "2026-05-10T14:23:00+03:00",
        "timezone_name": "Europe/Moscow",
        "locale": "ru",
        "profile": {
            "name": "Тест",
            "gender": "",
            "address": "ты",
            "timezone": "Europe/Moscow",
            "language": "ru",
        },
        "memories": [
            {
                "content": "Любит шахматы",
                "source": "memory:core",
                "score": 0.0,
                "saved_at": "2026-05-01",
            }
        ],
        "history": {
            "active_turn": None,
            "closed_turns": [
                {
                    "turn_id": "abc0123456789def",
                    "started_at": "2026-05-10T14:20:00+03:00",
                    "summary": None,
                    "messages": [
                        {"role": "юзер", "text": "Привет", "ts": "2026-05-10T14:20:01+03:00"},
                    ],
                    "is_active": False,
                }
            ],
        },
        "baseline_payloads": ['v2:fake:{"text": "Не забудь молоко и хлеб."}'],
        "old_called_tools": ["list_shopping"],
        "old_side_effects": [],
        "baseline_aligned": True,
        "trace_reliable": True,
    }
    base.update(overrides)
    return base


def _build(**overrides) -> ReplayExportRecord:
    return build_export_record(_raw_turn(**overrides), decrypt=_ok_decrypt)


# ---------------------------------------------------------------------------
# Round-trip through the contract
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_record_round_trips_through_from_dict(self):
        record = _build()
        restored = ReplayExportRecord.from_dict(record.to_dict())
        assert restored.user_message == "Что купить в магазине?"
        assert restored.timezone_name == "Europe/Moscow"
        assert restored.locale == "ru"
        assert restored.baseline["old_reply"] == "Не забудь молоко и хлеб."
        assert restored.baseline["old_called_tools"] == ["list_shopping"]

    def test_record_to_dict_is_json_serialisable(self):
        import json

        record = _build()
        round = json.loads(json.dumps(record.to_dict()))
        assert round["turn_id_hash"] == record.turn_id_hash

    def test_all_required_contract_fields_present(self):
        d = _build().to_dict()
        for key in (
            "turn_id_hash", "tenant_placeholder", "user_message", "now_iso",
            "timezone_name", "locale", "profile", "memories", "history",
            "baseline", "reconstruction_status",
        ):
            assert key in d

    def test_status_keys_match_fidelity_keys(self):
        status = _build().reconstruction_status
        expected = {
            "user_text", "now_tz_locale", "profile", "memory_provenance",
            "history_window", "active_turn", "voice_meta",
            "baseline_alignment", "decrypted_ok",
        }
        assert set(status.keys()) == expected


# ---------------------------------------------------------------------------
# turn_id hashing — opaque + stable
# ---------------------------------------------------------------------------


class TestTurnIdHash:
    def test_hash_is_16_hex_chars(self):
        h = hash_turn_id(_FAKE_TURN_ID)
        assert len(h) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", h)

    def test_hash_is_stable(self):
        assert hash_turn_id(_FAKE_TURN_ID) == hash_turn_id(_FAKE_TURN_ID)

    def test_hash_differs_per_id(self):
        assert hash_turn_id("run_a") != hash_turn_id("run_b")

    def test_raw_turn_id_not_present_in_output(self):
        """PII/opacity: the real turn id must never appear in the record."""
        import json

        record = _build()
        serialised = json.dumps(record.to_dict())
        assert _FAKE_TURN_ID not in serialised
        assert record.turn_id_hash == hash_turn_id(_FAKE_TURN_ID)

    def test_empty_turn_id_yields_empty_hash(self):
        record = _build(turn_id="")
        assert record.turn_id_hash == ""


# ---------------------------------------------------------------------------
# user_message extraction
# ---------------------------------------------------------------------------


class TestUserMessage:
    def test_prefers_source_value(self):
        record = _build(
            input_json={
                "source_value": "из source_value",
                "params": {"text": "из params"},
            }
        )
        assert record.user_message == "из source_value"
        assert record.reconstruction_status["user_text"] == "ok"

    def test_falls_back_to_params_text(self):
        record = _build(
            input_json={"source_type": "telegram_message", "params": {"text": "из params"}}
        )
        assert record.user_message == "из params"
        assert record.reconstruction_status["user_text"] == "ok"

    def test_missing_user_text_marked_missing(self):
        record = _build(input_json={"source_type": "telegram_message", "params": {}})
        assert record.user_message == ""
        assert record.reconstruction_status["user_text"] == "missing"

    def test_input_json_as_json_string(self):
        """input_json arrives as a JSON-encoded STRING (Text column)."""
        record = _build(
            input_json='{"source_value": "строка", "params": {"text": "x"}}'
        )
        assert record.user_message == "строка"


# ---------------------------------------------------------------------------
# now / tz / locale
# ---------------------------------------------------------------------------


class TestNowTzLocale:
    def test_full_tz_locale_is_ok(self):
        record = _build(timezone_name="Europe/Moscow", locale="ru")
        assert record.reconstruction_status["now_tz_locale"] == "ok"
        assert record.now_iso.startswith("2026-05-10T14:23:00")

    def test_missing_tz_degrades_and_falls_back(self):
        """tz NOT sourced (empty) → degraded, even though we fall back to a
        sensible default zone for rendering."""
        record = _build(timezone_name="", locale="ru")
        assert record.reconstruction_status["now_tz_locale"] == "degraded"
        assert record.timezone_name == "Europe/Moscow"  # sensible fallback

    def test_tz_sourced_no_locale_is_ok(self):
        """Russian-only product: tz sourced + defaulted "ru" locale → ok, NOT
        degraded (otherwise the §4.3 CRITICAL gate can never pass — Codex R1)."""
        record = _build(timezone_name="Europe/Moscow", locale="")
        assert record.reconstruction_status["now_tz_locale"] == "ok"
        assert record.locale == "ru"  # defaulted, FAITHFUL invariant

    def test_missing_timestamp_is_missing(self):
        record = _build(created_at=None)
        assert record.reconstruction_status["now_tz_locale"] == "missing"
        assert record.now_iso == ""

    def test_naive_timestamp_gets_offset_attached(self):
        """A naive stored timestamp is rendered tz-aware (offset attached)."""
        record = _build(created_at="2026-05-10T14:23:00", timezone_name="Europe/Moscow")
        # now_iso must carry an offset so the consumer's tz-aware check passes.
        assert re.search(r"[+\-]\d{2}:\d{2}$", record.now_iso)

    def test_now_iso_is_historical(self):
        """now_iso must come from the stored timestamp, NOT current time."""
        record = _build(created_at="2025-01-15T08:00:00+03:00")
        assert record.now_iso.startswith("2025-01-15T08:00:00")


class TestToIsoAware:
    def test_aware_utc_converted_into_user_tz(self):
        """An AWARE UTC datetime is CONVERTED into the user zone (Moscow=+03:00),
        so 11:00Z becomes 14:00+03:00 wall-clock."""
        from datetime import datetime, timezone

        aware_utc = datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc)
        iso = _to_iso_aware(aware_utc, tz_name="Europe/Moscow")
        assert iso is not None
        assert iso.endswith("+03:00")
        assert iso.startswith("2026-05-10T14:00:00")  # 11:00Z shifted +3h

    def test_naive_localized_to_zone(self):
        """A naive datetime is interpreted AS the user zone (offset attached,
        no hour shift)."""
        from datetime import datetime

        naive = datetime(2026, 5, 10, 14, 0, 0)
        iso = _to_iso_aware(naive, tz_name="Europe/Moscow")
        assert iso is not None
        assert iso.startswith("2026-05-10T14:00:00")
        assert iso.endswith("+03:00")

    def test_unparseable_returns_none(self):
        assert _to_iso_aware(None, tz_name="Europe/Moscow") is None
        assert _to_iso_aware("not-a-date", tz_name="Europe/Moscow") is None


# ---------------------------------------------------------------------------
# baseline / decryption
# ---------------------------------------------------------------------------


class TestBaselineDecryption:
    def test_decrypts_old_reply(self):
        record = _build()
        assert record.baseline["old_reply"] == "Не забудь молоко и хлеб."
        assert record.reconstruction_status["decrypted_ok"] == "ok"

    def test_decrypt_failure_marks_missing_and_does_not_crash(self):
        record = build_export_record(
            _raw_turn(baseline_payloads=['v2:realkey:ciphertext']),
            decrypt=_failing_decrypt,
        )
        assert record.reconstruction_status["decrypted_ok"] == "missing"
        assert record.baseline["old_reply"] == ""  # failed payload dropped

    def test_no_baseline_payload_marks_missing(self):
        record = _build(baseline_payloads=[])
        assert record.reconstruction_status["decrypted_ok"] == "missing"
        assert record.reconstruction_status["baseline_alignment"] == "missing"

    def test_heuristic_pairing_degrades_alignment(self):
        record = _build(baseline_aligned=False)
        assert record.reconstruction_status["baseline_alignment"] == "degraded"

    def test_unreliable_trace_does_not_degrade_alignment(self):
        """baseline_alignment is §4.2 reply/run correlation ONLY — an empty /
        unreliable tool trace must NOT poison this CRITICAL field (Codex R1)."""
        record = _build(baseline_aligned=True, trace_reliable=False)
        assert record.reconstruction_status["baseline_alignment"] == "ok"

    def test_aligned_baseline_is_ok(self):
        record = _build(baseline_aligned=True, trace_reliable=True)
        assert record.reconstruction_status["baseline_alignment"] == "ok"

    def test_multiple_payloads_joined(self):
        record = _build(
            baseline_payloads=[
                'v2:fake:{"text": "Часть 1."}',
                'v2:fake:{"text": "Часть 2."}',
            ]
        )
        assert record.baseline["old_reply"] == "Часть 1.\nЧасть 2."

    def test_already_plaintext_dict_payload(self):
        """If a payload arrives as a plaintext dict (ORM auto-decrypt path)."""
        record = _build(baseline_payloads=[{"text": "уже plaintext"}])
        assert record.baseline["old_reply"] == "уже plaintext"
        assert record.reconstruction_status["decrypted_ok"] == "ok"


# ---------------------------------------------------------------------------
# voice vs text
# ---------------------------------------------------------------------------


class TestVoice:
    def test_text_turn_has_no_voice(self):
        record = _build()
        assert record.voice is None
        assert record.reconstruction_status["voice_meta"] == "ok"

    def test_voice_source_type_detected_with_confidence(self):
        record = _build(
            input_json={
                "source_type": "telegram_voice",
                "source_value": "распознанный текст",
                "voice_meta": {"is_voice": True, "confidence": 0.87},
            }
        )
        assert record.voice == {"is_voice": True, "confidence": 0.87}
        assert record.reconstruction_status["voice_meta"] == "ok"

    def test_voice_without_confidence_degrades(self):
        record = _build(
            input_json={
                "source_type": "telegram_voice",
                "source_value": "распознанный текст",
            }
        )
        assert record.voice is not None
        assert record.voice["is_voice"] is True
        assert record.reconstruction_status["voice_meta"] == "degraded"

    def test_voice_meta_block_without_source_type(self):
        record = _build(
            input_json={
                "source_type": "telegram_message",
                "source_value": "текст",
                "voice_meta": {"is_voice": True, "confidence": 0.5},
            }
        )
        assert record.voice == {"is_voice": True, "confidence": 0.5}


# ---------------------------------------------------------------------------
# profile / memories / history status
# ---------------------------------------------------------------------------


class TestPlannerContext:
    def test_profile_mapped_and_ok(self):
        record = _build()
        assert record.profile["name"] == "Тест"
        assert record.reconstruction_status["profile"] == "ok"

    def test_empty_profile_marked_missing(self):
        record = _build(profile={})
        assert record.reconstruction_status["profile"] == "missing"

    def test_memories_passed_through(self):
        record = _build()
        assert len(record.memories) == 1
        assert record.memories[0]["content"] == "Любит шахматы"
        assert record.reconstruction_status["memory_provenance"] == "ok"

    def test_empty_memory_list_is_ok_not_missing(self):
        """Empty current memory is a VALID state — ok, not missing."""
        record = _build(memories=[])
        assert record.memories == []
        assert record.reconstruction_status["memory_provenance"] == "ok"

    def test_history_window_ok_active_turn_missing(self):
        record = _build()
        assert record.reconstruction_status["history_window"] == "ok"
        # No active turn reconstructed → non-critical missing.
        assert record.reconstruction_status["active_turn"] == "missing"

    def test_history_normalised_when_absent(self):
        record = _build(history=None)
        assert record.history == {"active_turn": None, "closed_turns": []}
        assert record.reconstruction_status["history_window"] == "missing"


# ---------------------------------------------------------------------------
# Safety guards (synthetic env — never mutate real os.environ destructively)
# ---------------------------------------------------------------------------


class TestSafetyGuards:
    def test_replay_mode_required(self):
        with pytest.raises(ReplaySafetyError):
            assert_replay_mode(env={})
        with pytest.raises(ReplaySafetyError):
            assert_replay_mode(env={"REPLAY_MODE": "0"})

    def test_replay_mode_ok(self):
        assert_replay_mode(env={"REPLAY_MODE": "1"})  # no raise

    def test_write_env_refused(self):
        with pytest.raises(ReplaySafetyError):
            assert_no_write_env(env={"TELEGRAM_BOT_TOKEN": "123:abc"})

    def test_clean_env_passes_write_guard(self):
        assert_no_write_env(env={"REPLAY_MODE": "1"})  # no raise

    def test_quarantine_inside_repo_refused(self, tmp_path):
        """A quarantine dir inside the git repo must be refused (§15)."""
        # Use the real repo anchor (the scripts/replay dir) and point the
        # quarantine env at a path inside the repo.
        repo_inside = str(_REPLAY_DIR / "leaked-export")
        with pytest.raises(ReplaySafetyError):
            resolve_quarantine_dir(
                env={"REPLAY_QUARANTINE_DIR": repo_inside},
                repo_anchor=_REPLAY_DIR,
            )

    def test_quarantine_outside_repo_ok(self, tmp_path):
        """A quarantine dir outside any repo resolves cleanly."""
        out = resolve_quarantine_dir(
            env={"REPLAY_QUARANTINE_DIR": str(tmp_path / "quarantine")},
            repo_anchor=_REPLAY_DIR,
        )
        assert out == (tmp_path / "quarantine").resolve()

    def test_quarantine_default_is_outside_repo(self, tmp_path):
        """The default quarantine path (system tmp) is accepted."""
        out = resolve_quarantine_dir(env={}, repo_anchor=_REPLAY_DIR)
        assert out == Path(exp._DEFAULT_QUARANTINE_DIR).resolve()


# ---------------------------------------------------------------------------
# safe_output_path — path-traversal guard (§15)
# ---------------------------------------------------------------------------


class TestSafeOutputPath:
    @pytest.mark.parametrize("bad", ["../x", "/abs/x", "a/b", "..", ""])
    def test_rejects_unsafe_names(self, tmp_path, bad):
        with pytest.raises(ReplaySafetyError):
            safe_output_path(tmp_path, bad)

    def test_rejects_backslash_separator(self, tmp_path):
        with pytest.raises(ReplaySafetyError):
            safe_output_path(tmp_path, "a\\b")

    def test_accepts_bare_filename(self, tmp_path):
        out = safe_output_path(tmp_path, "x.jsonl")
        assert out == (tmp_path / "x.jsonl").resolve()


# ---------------------------------------------------------------------------
# _begin_read_only — fail-closed on PG, no-op on sqlite (§2)
# ---------------------------------------------------------------------------


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


class _FakeSession:
    """Minimal session whose SET TRANSACTION READ ONLY raises (as on sqlite)."""

    def __init__(self, name: str) -> None:
        self.bind = _FakeBind(name)

    def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("SET TRANSACTION READ ONLY unsupported")


class TestBeginReadOnly:
    def test_sqlite_is_noop(self):
        """On a non-PG (sqlite) backend, a failed SET TRANSACTION READ ONLY is
        a no-op — we never write anyway."""
        _begin_read_only(_FakeSession("sqlite"))  # no raise

    def test_postgres_failclosed(self):
        """On Postgres, an unsettable read-only transaction HARD-FAILS (§2)."""
        with pytest.raises(ReplaySafetyError):
            _begin_read_only(_FakeSession("postgresql"))


# ---------------------------------------------------------------------------
# Closed-turn history summary line (FIX 6) — prompt_builder renders closed
# turns by `summary` only, so it must be a non-empty transcript line.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None


class _HistorySession:
    """Fake session driving exp._load_history through one prior closed turn.

    First execute() = the prior-runs query (returns one synthetic run);
    subsequent execute() calls = _load_baseline_payloads' outbox lookup.
    """

    def __init__(self):
        self._calls = 0

    def execute(self, _stmt, params=None):  # noqa: ANN001
        self._calls += 1
        if self._calls == 1:
            # (id, created_at, input_json, result_json) for one prior run.
            return _Result([
                (
                    "run_prior_000000002",
                    "2026-05-10T14:20:00+03:00",
                    '{"source_value": "Привет, среда", "user_id": "user_fake_1"}',
                    '{"outbox_message_ids": [1]}',
                )
            ])
        # outbox payload lookup → one row with a fake v2: payload.
        return _Result([('v2:fake:{"text": "Здравствуй!"}',)])


class TestClosedTurnSummary:
    def test_closed_turn_has_nonempty_summary(self):
        history = exp._load_history(
            _HistorySession(),
            tenant_id=_FAKE_TENANT_REAL,
            thread_id="thread_fake_1",
            before_run_id="run_target_1",
            before_created_at="2026-05-10T14:23:00+03:00",
            timezone_name="Europe/Moscow",
            decrypt=_ok_decrypt,
        )
        closed = history["closed_turns"]
        assert len(closed) == 1
        summary = closed[0]["summary"]
        assert summary  # non-empty — survives prompt_builder._render_history
        assert summary.startswith("юзер: Привет, среда")
        assert "среда: Здравствуй!" in summary
        # messages preserved alongside the summary.
        assert closed[0]["messages"][0]["text"] == "Привет, среда"


# ---------------------------------------------------------------------------
# FIX A — timezone provenance through fetch_raw_turns (fake session).
# A NULL/empty profile tz ⟹ now_tz_locale="degraded"; a real sourced tz ⟹ "ok".
# ---------------------------------------------------------------------------


class _FetchSession:
    """Fake session driving exp.fetch_raw_turns end-to-end for ONE run.

    Execute-call order in fetch_raw_turns (per run):
      1. the agent_runs main query (returns one synthetic completed run)
      2. _load_profile           (tenant_user_profiles row)
      3. _load_memories          (assistant_memories rows)
      4. _load_baseline_payloads (outbox_messages lookup for the target run)
      5. _load_history main query (prior runs — none here)
    The profile tz is parametrised so we can exercise sourced vs unsourced tz.
    """

    def __init__(self, profile_tz):
        self._calls = 0
        self._profile_tz = profile_tz

    def execute(self, _stmt, params=None):  # noqa: ANN001
        self._calls += 1
        if self._calls == 1:
            # agent_runs: (id, thread_id, created_at, input_json, result_json)
            return _Result([
                (
                    "run_fetch_000000001",
                    "thread_fetch_1",
                    "2026-05-10T14:23:00+03:00",
                    '{"source_value": "Привет", "user_id": "user_fetch_1"}',
                    '{"outbox_message_ids": [1]}',
                )
            ])
        if self._calls == 2:
            # _load_profile: (display_name, address_form, timezone, comm_style)
            return _Result([("Тест", "ты", self._profile_tz, "friendly")])
        if self._calls == 3:
            # _load_memories: (content, source, tier, created_at, embedding_json)
            return _Result([])
        if self._calls == 4:
            # _load_baseline_payloads: one same-tenant outbox payload.
            return _Result([('v2:fake:{"text": "Здравствуй!"}',)])
        # _load_history main query — no prior runs.
        return _Result([])


class TestFetchRawTurnsTzProvenance:
    def _one(self, profile_tz):
        turns = list(
            exp.fetch_raw_turns(
                _FetchSession(profile_tz),
                tenant_id=_FAKE_TENANT_REAL,
                decrypt=_ok_decrypt,
            )
        )
        assert len(turns) == 1
        return build_export_record(turns[0], decrypt=_ok_decrypt)

    def test_null_profile_tz_degrades(self):
        """Profile tz is NULL ⟹ raw timezone_name stays empty ⟹ degraded."""
        record = self._one(None)
        assert record.reconstruction_status["now_tz_locale"] == "degraded"

    def test_empty_profile_tz_degrades(self):
        """Profile tz is an empty string ⟹ degraded (unsourced)."""
        record = self._one("")
        assert record.reconstruction_status["now_tz_locale"] == "degraded"

    def test_real_profile_tz_is_ok(self):
        """A genuinely sourced profile tz ⟹ now_tz_locale="ok"."""
        record = self._one("Europe/Moscow")
        assert record.reconstruction_status["now_tz_locale"] == "ok"
        assert record.timezone_name == "Europe/Moscow"


# ---------------------------------------------------------------------------
# FIX B — _load_baseline_payloads tenant-scoping + completeness.
# aligned True ONLY when every referenced id resolves same-tenant.
# ---------------------------------------------------------------------------


class _BaselineSession:
    """Fake session for _load_baseline_payloads.

    ``resolvable`` maps outbox id → payload row (or None to simulate a missing
    / cross-tenant row that the tenant WHERE-clause filtered out).  Each
    execute() pops the next id from the query params and returns its row.
    """

    def __init__(self, resolvable):
        self._resolvable = resolvable

    def execute(self, _stmt, params=None):  # noqa: ANN001
        oid = params["id"]
        payload = self._resolvable.get(oid)
        return _Result([(payload,)] if payload is not None else [])


class TestLoadBaselineTenantScoping:
    def test_all_ids_resolve_same_tenant_aligned_true(self):
        session = _BaselineSession({
            1: 'v2:fake:{"text": "a"}',
            2: 'v2:fake:{"text": "b"}',
        })
        payloads, aligned = exp._load_baseline_payloads(
            session, '{"outbox_message_ids": [1, 2]}', _FAKE_TENANT_REAL
        )
        assert aligned is True
        assert len(payloads) == 2

    def test_one_id_missing_aligned_false(self):
        """A referenced id with no row (missing) ⟹ aligned False."""
        session = _BaselineSession({1: 'v2:fake:{"text": "a"}'})  # id 2 absent
        payloads, aligned = exp._load_baseline_payloads(
            session, '{"outbox_message_ids": [1, 2]}', _FAKE_TENANT_REAL
        )
        assert aligned is False
        assert len(payloads) == 1

    def test_cross_tenant_row_filtered_aligned_false(self):
        """A referenced row belonging to another tenant is filtered out by the
        tenant WHERE-clause (no row returned) ⟹ aligned False."""
        # id 2 exists but for a different tenant → our query returns no row.
        session = _BaselineSession({1: 'v2:fake:{"text": "a"}'})
        payloads, aligned = exp._load_baseline_payloads(
            session, '{"outbox_message_ids": [1, 2]}', _FAKE_TENANT_REAL
        )
        assert aligned is False
        assert len(payloads) == 1

    def test_no_ids_aligned_false(self):
        payloads, aligned = exp._load_baseline_payloads(
            _BaselineSession({}), '{"outbox_message_ids": []}', _FAKE_TENANT_REAL
        )
        assert aligned is False
        assert payloads == []


# ---------------------------------------------------------------------------
# FIX C — _load_memories exports stored embeddings (parsed vector or None).
# Still scoped by (tenant_id, user_id).
# ---------------------------------------------------------------------------


class _MemoriesSession:
    """Fake session returning assistant_memories rows for _load_memories.

    Each row is (content, source, tier, created_at, embedding_json).  Captures
    the bound query params so the test can assert (tenant_id, user_id) scoping.
    """

    def __init__(self, rows):
        self._rows = rows
        self.last_params = None

    def execute(self, _stmt, params=None):  # noqa: ANN001
        self.last_params = params
        return _Result(self._rows)


class TestLoadMemoriesEmbeddings:
    def test_valid_embedding_json_parsed_to_list(self):
        session = _MemoriesSession([
            ("Любит шахматы", "memory:core", "core", "2026-05-01",
             "[0.1, 0.2, 0.3]"),
        ])
        out = exp._load_memories(
            session, _FAKE_TENANT_REAL, "user_fake_1", _ok_decrypt
        )
        assert len(out) == 1
        assert out[0]["embedding"] == [0.1, 0.2, 0.3]
        assert out[0]["content"] == "Любит шахматы"

    def test_null_embedding_is_none(self):
        session = _MemoriesSession([
            ("c", "memory:core", "core", "2026-05-01", None),
        ])
        out = exp._load_memories(
            session, _FAKE_TENANT_REAL, "user_fake_1", _ok_decrypt
        )
        assert out[0]["embedding"] is None

    def test_garbage_embedding_is_none(self):
        """A non-JSON / non-list embedding_json ⟹ embedding None (not a crash)."""
        for garbage in ("not-json", '{"x": 1}', '"a string"', "  "):
            session = _MemoriesSession([
                ("c", "memory:core", "core", "2026-05-01", garbage),
            ])
            out = exp._load_memories(
                session, _FAKE_TENANT_REAL, "user_fake_1", _ok_decrypt
            )
            assert out[0]["embedding"] is None, garbage

    def test_scoped_by_tenant_and_user(self):
        session = _MemoriesSession([])
        exp._load_memories(session, _FAKE_TENANT_REAL, "user_fake_1", _ok_decrypt)
        assert session.last_params == {"t": _FAKE_TENANT_REAL, "u": "user_fake_1"}


# ---------------------------------------------------------------------------
# Import safety — importing the module must have NO side effects
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_import_does_not_assert_env(self):
        """Importing the module must NOT raise even without REPLAY_MODE — all
        guards live in main(), so the pure functions are unit-testable."""
        import importlib

        # Re-import is a no-op if already imported; the assertion is that the
        # original import (top of file) already succeeded without REPLAY_MODE.
        importlib.reload(exp)
        assert hasattr(exp, "build_export_record")
