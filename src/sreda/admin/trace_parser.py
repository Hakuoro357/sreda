"""Tolerant parser for `/var/log/sreda/trace.log` block format.

Format reference (services.trace.emit_block 2026-05-08):

    === TRACE <trace_id> <YYYY-MM-DD HH:MM:SS.fff> user=<id> chan=<channel> ===
        43ms  ack.sent               [201ms] phrase="🌀 Секунду…" status=ok tg_date=...
        67ms  chat.memory_recall.embed [397ms] dim=1024
      1601ms  llm.iter.0             [4841ms] in_tok=27283 model=mimo-v2.5-pro out_tok=35
      6447ms  chat.reply             chars=33 rescued=False
    ------- TOTAL 6705ms iters=1 tok_in=27283 tok_out=35

Design (per Codex r1+r2 fixes for plan):
- **Tolerant**: malformed lines don't break parse; collected as
  warnings on the ParsedTrace.parse_warnings.
- **Tail-only bounded read** (5MB cap): trace.log can grow.
  Read tail, find last N `=== TRACE` markers, parse blocks.
- **PII allowlist**: meta keys that may contain user content
  (text, body, content, summary, query, …) are dropped from the
  rendered meta dict. Only safe numeric/enum keys retained.
- **Hard caps**: tail/min_total_ms/user_filter validated by route.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Format regexes — match the actual produced format. Tolerate optional
# fields (Codex r1 MAJOR fix #5+#7).
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(
    r"^=== TRACE\s+(?P<id>\S+)\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?:\s+user=(?P<user>\S+))?"
    r"(?:\s+chan=(?P<chan>\S+))?"
    r"\s+===\s*$"
)

_TOTAL_RE = re.compile(
    r"^-+\s*TOTAL\s+(?P<ms>\d+)ms"
    r"(?:\s+iters=(?P<iters>\d+))?"
    r"(?:\s+tok_in=(?P<tin>\d+))?"
    r"(?:\s+tok_out=(?P<tout>\d+))?"
)

_STAGE_RE = re.compile(
    r"^\s+(?P<at>\d+)ms\s+(?P<name>\S+)"
    r"(?:\s+\[(?P<dur>\d+)ms\])?"
    r"(?P<meta>\s+.*)?$"
)

# Logger prefix that wraps every line emitted via logger.info() —
# trace.log actually has standard logging prefix before the
# block lines. Example:
#   2026-05-08 09:32:50 INFO sreda.trace
#   === TRACE ...
# Followed by indented stage lines and then ------- TOTAL.
# Sometimes everything appears on one line via formatter; we split
# by reading lines AND scanning for === TRACE markers anywhere.

# ---------------------------------------------------------------------------
# PII allowlist — only these meta keys are safe to render in HTML.
# Everything else is dropped (Codex r1 CRITICAL #4).
# ---------------------------------------------------------------------------

_SAFE_META_KEYS: frozenset[str] = frozenset({
    # Stage-specific safe fields (numbers, statuses, IDs already redacted)
    "feature_key", "plan_key", "is_grandfathered", "fallback_used",
    "history_count", "history_turns",
    "memory_candidates", "candidates", "selected_count", "selected",
    "with_embedding", "embedding_cache_hit", "dim",
    "tool_count", "base_tools", "rows_affected",
    "iters", "tok_in", "tok_out", "in_tok", "out_tok",
    "status", "quota_used_pct", "quota_remaining_daily",
    "used_pct", "exhausted", "consumed", "allowed",
    "stable_chars", "chars", "chars_out", "bytes_in", "bytes_out",
    "model", "provider", "channel", "type",
    "rescued", "decision",
    "voice_duration_s", "magic_hex", "format_guess",
    "tg_date", "tg_message_id",  # numeric IDs, not PII
})

_MAX_META_VALUE_LEN = 80
_MAX_BYTES_READ = 5 * 1024 * 1024  # 5MB tail


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedStage:
    at_ms: int
    name: str
    duration_ms: int | None
    meta: dict[str, str]  # only safe keys, values truncated

    @property
    def effective_ms(self) -> int | None:
        """Эффективная длительность стадии для ОТОБРАЖЕНИЯ/АТРИБУЦИИ (раскраска строки в трейс-вьюере +
        выбор «где застряло» в обзор-снапшоте), НЕ для колонки длительности.

        #255-фикс: события ``react.*`` несут длительность вызова как ``latency_ms`` в МЕТЕ, а не в
        ``duration_ms`` (иначе раздули бы TOTAL блока — эмитятся в конце хода, ``at_ms``=конец). Побочно
        это увело react.llm-латентность из ``duration_ms``, на который смотрят ДВА потребителя:
        (1) раскраска строки ``stage_duration_color`` — медленный вызов перестал краснеть; (2)
        ``overview_snapshot._slow_turns_block`` — LLM-ходы стали падать в «между стадиями (не размечено)».
        Оба берут ``effective_ms``: своё ``duration_ms``, а когда его нет — ``latency_ms`` из меты. Так
        восстановлено и «краснеет на глаз», и корректная атрибуция долгого хода к react.llm.

        Колонку длительности в шаблоне (гейт ``duration_ms is not none``) это НЕ трогает: она остаётся
        пустой, чтобы latency не читался как длительность стадии и не «суммировался» глазом сверх TOTAL.

        Агрегатные шаги (``react.tool`` с ``sum_latency_ms``) осознанно возвращают None (нейтральны/не
        атрибутируются): сумма по нескольким вызовам против пер-стадийных порогов (2с/5с для ОДНОЙ стадии)
        красила бы штатные мультитул-ходы ложным красным; узкие места видны в ``top3``. Тянем только
        ``latency_ms`` — react.tool так и был вне slow-turns (у него ``duration_ms``=0), регресса нет."""
        if self.duration_ms is not None:
            return self.duration_ms
        raw = self.meta.get("latency_ms")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


@dataclass(slots=True)
class ParsedTrace:
    turn_id: str
    timestamp: str
    user_id: str | None
    channel: str | None
    total_ms: int | None
    iters: int | None
    tok_in: int | None
    tok_out: int | None
    stages: list[ParsedStage] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Meta-value parser (lightweight, no shlex — values can contain spaces in
# quotes and emoji)
# ---------------------------------------------------------------------------


def _parse_meta_string(s: str) -> dict[str, str]:
    """Parse `key=value key2="value with spaces" k3=42 …` → dict.

    Tolerant: drops malformed pairs silently. Preserves quotes if value
    starts with a quote (matched closing quote).
    """
    out: dict[str, str] = {}
    s = (s or "").strip()
    i = 0
    n = len(s)
    while i < n:
        # Skip whitespace
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        # Read key
        start = i
        while i < n and s[i] != "=" and not s[i].isspace():
            i += 1
        if i >= n or s[i] != "=":
            # Malformed: no `=`, skip rest
            break
        key = s[start:i]
        i += 1  # skip `=`
        # Read value — quoted or until whitespace
        if i < n and s[i] in ('"', "'"):
            quote = s[i]
            i += 1
            val_start = i
            while i < n and s[i] != quote:
                i += 1
            value = s[val_start:i]
            if i < n:
                i += 1  # consume closing quote
        else:
            val_start = i
            while i < n and not s[i].isspace():
                i += 1
            value = s[val_start:i]
        out[key] = value
    return out


def _filter_safe_meta(raw: dict[str, str], step_name: str = "") -> dict[str, str]:
    """Apply allowlist + truncate value length. Codex r1 CRITICAL #4.

    #255: шаги ``react.*`` (наблюдаемость react_loop) — ПД-free by construction (числа/enum/имена
    инструментов, как ``llm_calls_json`` #192), пропускаем ВСЕ их поля (иначе latency_ms/intent/fallback
    отрендерились бы пустыми — ради них весь #255). Префикс БУКВАЛЬНО ``"react."`` (с точкой):
    ``react_loop.replied`` НЕ подпадает (после ``react`` идёт ``_``) → у него прежний строгий allowlist.
    Обрезка длины (_MAX_META_VALUE_LEN) применяется в любом случае — страховка от простыней."""
    loose = step_name.startswith("react.")
    safe: dict[str, str] = {}
    for key, value in raw.items():
        if not loose and key not in _SAFE_META_KEYS:
            continue
        if len(value) > _MAX_META_VALUE_LEN:
            value = value[: _MAX_META_VALUE_LEN - 1] + "…"
        safe[key] = value
    return safe


# ---------------------------------------------------------------------------
# Block parser
# ---------------------------------------------------------------------------


def _parse_block(lines: list[str]) -> ParsedTrace | None:
    """Parse a single trace block (header + stages + total).

    Returns None if header is missing/malformed (caller can drop).
    """
    if not lines:
        return None

    # First line: header
    header_match = _HEADER_RE.match(lines[0].strip())
    if not header_match:
        return None

    trace = ParsedTrace(
        turn_id=header_match.group("id"),
        timestamp=header_match.group("ts"),
        user_id=header_match.group("user"),
        channel=header_match.group("chan"),
        total_ms=None,
        iters=None,
        tok_in=None,
        tok_out=None,
    )

    for line in lines[1:]:
        line_stripped = line.rstrip("\n")
        if not line_stripped.strip():
            continue
        # TOTAL line ends the block
        total_match = _TOTAL_RE.match(line_stripped.strip())
        if total_match:
            try:
                trace.total_ms = int(total_match.group("ms"))
            except (TypeError, ValueError):
                trace.parse_warnings.append(f"bad TOTAL ms: {line_stripped[:80]}")
            iters = total_match.group("iters")
            tin = total_match.group("tin")
            tout = total_match.group("tout")
            if iters:
                try:
                    trace.iters = int(iters)
                except ValueError:
                    pass
            if tin:
                try:
                    trace.tok_in = int(tin)
                except ValueError:
                    pass
            if tout:
                try:
                    trace.tok_out = int(tout)
                except ValueError:
                    pass
            continue
        # Stage line
        stage_match = _STAGE_RE.match(line_stripped)
        if not stage_match:
            trace.parse_warnings.append(f"unparsed: {line_stripped[:80]}")
            continue
        try:
            at_ms = int(stage_match.group("at"))
        except (TypeError, ValueError):
            trace.parse_warnings.append(f"bad at_ms: {line_stripped[:80]}")
            continue
        dur_str = stage_match.group("dur")
        duration_ms: int | None = None
        if dur_str:
            try:
                duration_ms = int(dur_str)
            except ValueError:
                pass
        meta_raw = _parse_meta_string(stage_match.group("meta") or "")
        meta_safe = _filter_safe_meta(meta_raw, stage_match.group("name"))
        trace.stages.append(ParsedStage(
            at_ms=at_ms,
            name=stage_match.group("name"),
            duration_ms=duration_ms,
            meta=meta_safe,
        ))

    return trace


# ---------------------------------------------------------------------------
# Tail reader
# ---------------------------------------------------------------------------


def _read_tail(path: str, max_bytes: int = _MAX_BYTES_READ) -> str:
    """Read the last `max_bytes` bytes of file. Returns decoded text.

    Truncates at first newline boundary so we don't start mid-line.
    Returns empty string if file missing/unreadable.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size == 0:
        return ""
    start = max(0, size - max_bytes)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return ""
    if start > 0:
        # Drop first partial line — start at first \n.
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1:]
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _split_into_blocks(text: str) -> list[list[str]]:
    """Split tail text into trace blocks by `=== TRACE` markers."""
    blocks: list[list[str]] = []
    current: list[str] = []
    in_block = False
    for line in text.splitlines():
        # Header signals new block
        if line.lstrip().startswith("=== TRACE"):
            if current and in_block:
                blocks.append(current)
            current = [line]
            in_block = True
            continue
        if in_block:
            current.append(line)
            # Block ends after TOTAL line
            if line.strip().startswith("------- TOTAL") or line.strip().startswith("---- TOTAL"):
                blocks.append(current)
                current = []
                in_block = False
                continue
    # Trailing block without TOTAL — still include for diagnostic
    if current and in_block:
        blocks.append(current)
    return blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_trace_log(
    path: str,
    *,
    tail_turns: int = 50,
    min_total_ms: int = 0,
    user_filter: str | None = None,
) -> list[ParsedTrace]:
    """Parse the last N trace blocks from `path`.

    Args:
        path: filesystem path to trace.log
        tail_turns: maximum number of recent blocks to return (after filter)
        min_total_ms: drop blocks with total_ms < threshold
        user_filter: if set, keep only blocks where user_id == this value

    Returns:
        List of ParsedTrace, **most-recent-first**. Empty if no blocks
        match or file unreadable.
    """
    text = _read_tail(path)
    if not text:
        return []

    raw_blocks = _split_into_blocks(text)
    parsed: list[ParsedTrace] = []
    for block_lines in raw_blocks:
        trace = _parse_block(block_lines)
        if trace is None:
            continue
        if min_total_ms and (trace.total_ms or 0) < min_total_ms:
            continue
        if user_filter and trace.user_id != user_filter:
            continue
        parsed.append(trace)

    # Most-recent-first (file is append-only, last block in file = newest)
    parsed.reverse()
    if tail_turns > 0:
        parsed = parsed[:tail_turns]
    return parsed


# ---------------------------------------------------------------------------
# UI helpers (used by Jinja template — kept here for testability)
# ---------------------------------------------------------------------------


def total_ms_color(total_ms: int | None) -> str:
    """Return badge color class for total_ms value.

    Aligned with `turn_latency_p95` probe thresholds:
      - <10s   → green
      - 10-30s → yellow
      - >30s   → red
    """
    if total_ms is None:
        return "neutral"
    if total_ms > 30_000:
        return "red"
    if total_ms > 10_000:
        return "yellow"
    return "green"


def stage_duration_color(duration_ms: int | None) -> str:
    """Return badge color for individual stage duration.

    Per-stage thresholds tighter — no single stage should be >5s normally.
      - <2s    → neutral
      - 2-5s   → yellow
      - >5s    → red
    """
    if duration_ms is None:
        return "neutral"
    if duration_ms > 5_000:
        return "red"
    if duration_ms > 2_000:
        return "yellow"
    return "neutral"
