#!/usr/bin/env python3
"""Inventory raw tool outputs from #68 LLM trace files.

Sub-A3 of Plan-Execute Epic (Hakuoro357/vex-assistant#74). The full
tool-output wrapper (Sub-A4) needs to know which raw string patterns
to translate into ``ToolOutput`` discriminator unions. Production
already has ~30 days of LLM traces under
``/var/lib/sreda/private/llm-traces/YYYY-MM-DD/*.jsonl`` (Issue #68).
This script reads those traces, extracts every ``ToolMessage``
content, groups by tool name, classifies by leading prefix, and
emits a markdown coverage matrix.

Output (when run with ``--out matrix.md``):

    ## Tool: add_shopping_items (count=247)

    | Prefix | Count | Sample |
    |---|---|---|
    | ok:added:N items | 198 | ``ok:added:3 items: молоко, хлеб, ...`` |
    | ok:duplicate:N existed | 31 | ``ok:duplicate:2 existed: молоко`` |
    | error:validation_failed | 14 | ``error:validation_failed: too long`` |
    | error:limit_exceeded | 4 | ``error:limit_exceeded: 200 items max`` |

The matrix is a planning input for Sub-A4: each row is one
``Literal[...]`` variant in the tool's output discriminator union.

Usage on VDS:
    sudo /opt/sreda/.venv/bin/python /opt/sreda/scripts/tool_output_inventory.py \\
        --root /var/lib/sreda/private/llm-traces \\
        --out /tmp/tool_outputs.md

Defaults to current directory + stdout for local dev.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("sreda.tool_output_inventory")

DEFAULT_ROOT = Path("/var/lib/sreda/private/llm-traces")


# Heuristic prefix extractor. Tool outputs in current codebase follow
# patterns like ``ok:added:N items: ...`` or ``error:validation_failed: ...``
# — the first two ``:`` segments identify the result class without the
# variable payload. Falls back to the leading 50 chars when no ``:`` is
# present.
_PREFIX_PATTERN = re.compile(r"^([^:\n]{1,40})(?::([^:\n]{1,40}))?")


def classify_prefix(output: str) -> str:
    """Reduce a raw output string to a stable pattern key.

    Examples
    --------
    >>> classify_prefix("ok:added:3 items: молоко, хлеб")
    'ok:added'
    >>> classify_prefix("error:validation_failed: too long")
    'error:validation_failed'
    >>> classify_prefix("skipped:past:reminder at 2020-01-01")
    'skipped:past'
    >>> classify_prefix("This is some plain text reply")
    'This is some plain text reply'
    """
    text = output.strip()
    if not text:
        return "<empty>"
    # Unstructured / plain-text output — no ``:`` separator. Return the
    # first 50 chars as the bucket label so multiple plain replies with
    # the same wording collapse into one row.
    if ":" not in text:
        return text[:50]
    match = _PREFIX_PATTERN.match(text)
    if not match:
        return text[:50]
    primary = match.group(1).strip()
    secondary = (match.group(2) or "").strip()
    if secondary:
        return f"{primary}:{secondary}"
    return primary


@dataclass
class _ToolOutputRecord:
    tool_name: str
    raw_output: str
    prefix: str


def iter_tool_messages(envelope: dict) -> Iterable[_ToolOutputRecord]:
    """Yield one record per ``ToolMessage`` found in a request envelope.

    Request envelopes carry the message history fed into the LLM at
    each iteration. Tool outputs appear as ``ToolMessage`` entries
    with ``name`` (the tool that ran) and ``content`` (the raw output
    string the LLM saw next).
    """
    messages = envelope.get("messages") or []
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "ToolMessage":
            continue
        name = (msg.get("name") or "").strip()
        if not name:
            continue
        content = msg.get("content")
        # ``content`` is usually str but the round-trip preserves
        # list[dict] structures (multi-part). For inventory we only
        # need the textual surface — flatten any list to a single str.
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        if not isinstance(content, str):
            continue
        yield _ToolOutputRecord(
            tool_name=name,
            raw_output=content,
            prefix=classify_prefix(content),
        )


def scan_directory(root: Path) -> list[_ToolOutputRecord]:
    """Walk every ``.jsonl`` under ``root`` and collect tool outputs."""
    if not root.is_dir():
        raise FileNotFoundError(f"Trace root not found: {root}")
    records: list[_ToolOutputRecord] = []
    for path in sorted(root.rglob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("could not read %s: %s", path, exc)
            continue
        for line_num, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "skipping malformed JSON in %s:%d — %s", path, line_num, exc
                )
                continue
            for record in iter_tool_messages(envelope):
                records.append(record)
    return records


def build_matrix(records: list[_ToolOutputRecord]) -> dict[str, list[tuple[str, int, str]]]:
    """Group records by tool name and prefix.

    Returns ``{tool_name: [(prefix, count, sample_raw), ...]}`` sorted
    by count desc within each tool.
    """
    by_tool: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    samples: dict[tuple[str, str], str] = {}
    for record in records:
        by_tool[record.tool_name][record.prefix][record.raw_output] += 1
        # Keep first-seen sample per (tool, prefix) pair — easier to
        # spot-check than picking longest/random.
        key = (record.tool_name, record.prefix)
        if key not in samples:
            samples[key] = record.raw_output

    matrix: dict[str, list[tuple[str, int, str]]] = {}
    for tool_name, prefix_buckets in by_tool.items():
        rows: list[tuple[str, int, str]] = []
        for prefix, raw_counter in prefix_buckets.items():
            total = sum(raw_counter.values())
            sample = samples[(tool_name, prefix)]
            rows.append((prefix, total, sample))
        rows.sort(key=lambda r: r[1], reverse=True)
        matrix[tool_name] = rows
    return matrix


def _escape_md(text: str, max_len: int = 80) -> str:
    """Make text safe + compact for a markdown table cell."""
    text = text.replace("\r", "").replace("\n", " ").replace("|", "\\|")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return f"`{text}`"


def render_matrix(matrix: dict[str, list[tuple[str, int, str]]]) -> str:
    """Render the matrix as markdown for human review.

    Tools are sorted by total record count desc — most-frequent at the
    top, where Sub-A4 wrapper attention should land first.
    """
    if not matrix:
        return "# Tool output inventory\n\nNo tool outputs found.\n"

    totals = {name: sum(c for _, c, _ in rows) for name, rows in matrix.items()}
    by_total = sorted(matrix.items(), key=lambda x: totals[x[0]], reverse=True)
    out = [
        "# Tool output inventory",
        "",
        f"_{len(matrix)} tools, "
        f"{sum(totals.values())} total tool outputs observed._",
        "",
    ]
    for tool_name, rows in by_total:
        total = totals[tool_name]
        out.append(f"## Tool: `{tool_name}` (count={total})")
        out.append("")
        out.append("| Prefix | Count | Sample |")
        out.append("|---|---|---|")
        for prefix, count, sample in rows:
            out.append(
                f"| `{prefix}` | {count} | {_escape_md(sample)} |"
            )
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory raw tool outputs from #68 LLM trace files.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=(
            f"Root directory of trace files (default: {DEFAULT_ROOT}). "
            "Searches recursively for *.jsonl."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write markdown to this file (default: stdout).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        records = scan_directory(args.root)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    matrix = build_matrix(records)
    report = render_matrix(matrix)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        logger.info(
            "wrote %d tools / %d outputs to %s",
            len(matrix), len(records), args.out,
        )
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
