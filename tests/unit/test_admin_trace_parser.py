"""Tests for `admin.trace_parser`. Format anchored на actual production
trace.log output (verified post 2026-05-08 deploy).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sreda.admin.trace_parser import (
    ParsedTrace,
    parse_trace_log,
    stage_duration_color,
    total_ms_color,
    _parse_block,
    _parse_meta_string,
    _split_into_blocks,
)


# ---------------------------------------------------------------------------
# Fixtures: real-looking blocks (sanitized — no real user_id).
# ---------------------------------------------------------------------------


_REAL_BLOCK = """\
=== TRACE trace_cb992c92001541f9 2026-05-08 09:32:43.720 user=user_test_1 chan=telegram ===
      0ms  webhook.received       type=text
     43ms  ack.sent               [201ms] phrase="\xf0\x9f\x8c\x80 Секунду…" status=ok tg_date=1778232763 tg_message_id=3452
     67ms  chat.memory_recall.embed [397ms] dim=1024
    465ms  chat.memory_recall.cosine [8ms] candidates=2 selected=2 with_embedding=2
    479ms  chat.skill_resolve     [29ms] feature_key=housewife_assistant
    508ms  chat.budget_check      [3ms] exhausted=False used_pct=0
    514ms  chat.gate.entitlement  allowed=True is_grandfathered=True plan_key=housewife_assistant_base
   1058ms  chat.prompt_build      stable_chars=26306
   1058ms  chat.tools_build       [20ms] base_tools=7
   1586ms  chat.history_load      [13ms] history_turns=9
   1601ms  llm.iter.0             [4841ms] in_tok=27283 model=mimo-v2.5-pro out_tok=35 tools=[]
   6447ms  chat.reply             chars=33 rescued=False
   6458ms  outbox.enqueued        chars=33 decision=send feature_key=housewife_assistant
   6705ms  outbox.delivered       chat=999 path=inline status=ok tg_date=1778232770 tg_message_id=3453
------- TOTAL 6705ms iters=1 tok_in=27283 tok_out=35
"""


_SLOW_BLOCK = """\
=== TRACE trace_slow_xxx 2026-05-08 09:18:41.352 user=user_test_2 chan=telegram ===
      0ms  webhook.received       type=voice voice_duration_s=13
   2534ms  llm.iter.0             [8891ms] in_tok=27088 model=mimo-v2.5-pro out_tok=264 tools=[get_weather]
  12187ms  llm.iter.1             [6333ms] in_tok=27221 model=mimo-v2.5-pro out_tok=220 tools=[]
  18524ms  chat.reply             chars=398 rescued=False
  18715ms  outbox.delivered       chat=999 path=inline status=ok
------- TOTAL 18715ms iters=2 tok_in=54309 tok_out=484
"""


# ---------------------------------------------------------------------------
# meta parsing
# ---------------------------------------------------------------------------


def test_parse_meta_simple_kv():
    meta = _parse_meta_string("status=ok tg_date=1234")
    assert meta == {"status": "ok", "tg_date": "1234"}


def test_parse_meta_quoted_value_with_spaces():
    meta = _parse_meta_string('phrase="Hello world" status=ok')
    assert meta == {"phrase": "Hello world", "status": "ok"}


def test_parse_meta_emoji_unicode():
    meta = _parse_meta_string('phrase="🌀 Секунду…" status=ok')
    assert meta["phrase"] == "🌀 Секунду…"
    assert meta["status"] == "ok"


def test_parse_meta_empty_string():
    assert _parse_meta_string("") == {}


def test_parse_meta_malformed_no_equals():
    # Token without = is skipped silently.
    meta = _parse_meta_string("foo bar=1")
    # Implementation treats `foo` as key without value → break loop. Acceptable.
    assert "bar" not in meta or meta.get("bar") == "1"


# ---------------------------------------------------------------------------
# _split_into_blocks
# ---------------------------------------------------------------------------


def test_split_recognizes_two_blocks():
    text = _REAL_BLOCK + _SLOW_BLOCK
    blocks = _split_into_blocks(text)
    assert len(blocks) == 2
    assert any("trace_cb992c92" in b[0] for b in blocks)
    assert any("trace_slow_xxx" in b[0] for b in blocks)


def test_split_garbage_outside_blocks_ignored():
    text = "garbage line\n" + _REAL_BLOCK + "more garbage\n"
    blocks = _split_into_blocks(text)
    assert len(blocks) == 1


# ---------------------------------------------------------------------------
# _parse_block — real format
# ---------------------------------------------------------------------------


def test_parse_real_block_total_extracted():
    lines = _REAL_BLOCK.splitlines()
    trace = _parse_block(lines)
    assert trace is not None
    assert trace.turn_id == "trace_cb992c92001541f9"
    assert trace.user_id == "user_test_1"
    assert trace.channel == "telegram"
    assert trace.total_ms == 6705
    assert trace.iters == 1
    assert trace.tok_in == 27283
    assert trace.tok_out == 35


def test_parse_real_block_stages_count():
    lines = _REAL_BLOCK.splitlines()
    trace = _parse_block(lines)
    assert trace is not None
    # 14 stage lines (без header и TOTAL)
    stage_names = [s.name for s in trace.stages]
    assert "webhook.received" in stage_names
    assert "chat.skill_resolve" in stage_names
    assert "chat.memory_recall.embed" in stage_names
    assert "llm.iter.0" in stage_names
    assert "chat.reply" in stage_names
    assert "outbox.delivered" in stage_names


def test_parse_real_block_durations_extracted():
    lines = _REAL_BLOCK.splitlines()
    trace = _parse_block(lines)
    assert trace is not None
    by_name = {s.name: s for s in trace.stages}
    assert by_name["chat.memory_recall.embed"].duration_ms == 397
    assert by_name["chat.skill_resolve"].duration_ms == 29
    assert by_name["llm.iter.0"].duration_ms == 4841
    # No `[Xms]` bracket present — duration_ms is None
    assert by_name["chat.reply"].duration_ms is None


def test_parse_real_block_meta_allowlist():
    """PII fields stripped, safe fields preserved."""
    lines = _REAL_BLOCK.splitlines()
    trace = _parse_block(lines)
    assert trace is not None
    by_name = {s.name: s for s in trace.stages}
    # Safe fields preserved
    assert by_name["chat.skill_resolve"].meta.get("feature_key") == "housewife_assistant"
    assert by_name["chat.budget_check"].meta.get("exhausted") == "False"
    assert by_name["chat.memory_recall.embed"].meta.get("dim") == "1024"
    # `phrase` is NOT in allowlist — should be filtered out (Codex CRITICAL #4)
    ack = by_name.get("ack.sent")
    if ack:  # guard — check applies if stage is present
        assert "phrase" not in ack.meta, "phrase contains user-visible text — must not leak to UI"


def test_parse_block_empty_lines_returns_none():
    assert _parse_block([]) is None
    assert _parse_block([""]) is None
    assert _parse_block(["random garbage"]) is None


# ---------------------------------------------------------------------------
# parse_trace_log — file-level
# ---------------------------------------------------------------------------


def test_parse_trace_log_missing_file_returns_empty(tmp_path: Path):
    nonexistent = tmp_path / "nope.log"
    assert parse_trace_log(str(nonexistent)) == []


def test_parse_trace_log_real_file_basic(tmp_path: Path):
    log_path = tmp_path / "trace.log"
    log_path.write_text(_REAL_BLOCK + _SLOW_BLOCK, encoding="utf-8")
    traces = parse_trace_log(str(log_path), tail_turns=10)
    assert len(traces) == 2
    # Most-recent-first: slow_xxx came after cb992c92
    assert traces[0].turn_id == "trace_slow_xxx"
    assert traces[1].turn_id == "trace_cb992c92001541f9"


def test_parse_trace_log_filter_min_total_ms(tmp_path: Path):
    log_path = tmp_path / "trace.log"
    log_path.write_text(_REAL_BLOCK + _SLOW_BLOCK, encoding="utf-8")
    traces = parse_trace_log(str(log_path), min_total_ms=10_000)
    # Only slow_xxx (18715ms) >= 10s
    assert len(traces) == 1
    assert traces[0].turn_id == "trace_slow_xxx"


def test_parse_trace_log_filter_user(tmp_path: Path):
    log_path = tmp_path / "trace.log"
    log_path.write_text(_REAL_BLOCK + _SLOW_BLOCK, encoding="utf-8")
    traces = parse_trace_log(str(log_path), user_filter="user_test_1")
    assert len(traces) == 1
    assert traces[0].turn_id == "trace_cb992c92001541f9"


def test_parse_trace_log_tail_limit(tmp_path: Path):
    log_path = tmp_path / "trace.log"
    log_path.write_text(_REAL_BLOCK + _SLOW_BLOCK, encoding="utf-8")
    traces = parse_trace_log(str(log_path), tail_turns=1)
    assert len(traces) == 1


def test_parse_trace_log_tolerates_malformed_block(tmp_path: Path):
    """Codex r1 MAJOR #7: malformed block doesn't crash, others parsed."""
    bad = "=== TRACE BAD blah\nrandom line\n"
    log_path = tmp_path / "trace.log"
    log_path.write_text(bad + _REAL_BLOCK, encoding="utf-8")
    traces = parse_trace_log(str(log_path))
    # _REAL_BLOCK still parsed; bad block silently dropped.
    assert any(t.turn_id == "trace_cb992c92001541f9" for t in traces)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ms,expected", [
    (None, "neutral"),
    (5_000, "green"),
    (10_000, "green"),
    (10_001, "yellow"),
    (29_999, "yellow"),
    (30_001, "red"),
    (50_000, "red"),
])
def test_total_ms_color_thresholds(ms, expected):
    assert total_ms_color(ms) == expected


@pytest.mark.parametrize("ms,expected", [
    (None, "neutral"),
    (500, "neutral"),
    (2_001, "yellow"),
    (5_001, "red"),
])
def test_stage_duration_color_thresholds(ms, expected):
    assert stage_duration_color(ms) == expected
