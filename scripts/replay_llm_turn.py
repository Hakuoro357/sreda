#!/usr/bin/env python3
"""Issue #68 — replay captured LLM envelope в произвольный provider.

Reads phase=request envelope из day-folder JSONL files, reconstructs the
exact ainvoke call в same или different provider, prints side-by-side diff
с captured response.

**Read-only guarantee**: this script NEVER imports ``sreda.db.session`` или
production tool factories (``build_memory_tools`` / ``build_housewife_tools``).
Tool schemas reconstructed from captured envelope.request.tool_schemas (NOT
re-built). Replay invokes LLM, prints output, не touches DB.

**Cross-provider warning**: default replay uses captured provider/model
(sanity check). If --provider/--model differ from captured →
``--allow-cross-provider`` flag required + interactive confirm + log
destination (PII may travel к new data processor).

Usage:
    python replay_llm_turn.py --trace-id trace_abc \\
        [--date YYYY-MM-DD]
        [--root /var/lib/sreda/private/llm-traces]
        [--iter 0]
        [--attempt primary|fallback]
        [--provider mimo-v2.5]
        [--model mimo-v2.5-pro]
        [--allow-cross-provider]
        [--diff]
        [--output /path/log.txt]   # mode 0600 при создании
        [--show-content]           # explicit opt-in для PII в stdout

Plan: plans/mellow-discovering-conway-final.md, Section 13.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("/var/lib/sreda/private/llm-traces")
logger = logging.getLogger("sreda.llm_trace.replay")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _find_trace_files(root: Path, trace_id: str, date_hint: str | None) -> list[Path]:
    """Locate trace JSONL files. With --date — single folder. Without —
    scan all day-folders."""
    if date_hint:
        candidate = root / date_hint / f"{trace_id}.jsonl"
        return [candidate] if candidate.is_file() else []
    matches: list[Path] = []
    if not root.is_dir():
        return matches
    for day_dir in root.iterdir():
        if not day_dir.is_dir():
            continue
        candidate = day_dir / f"{trace_id}.jsonl"
        if candidate.is_file():
            matches.append(candidate)
    return matches


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            logger.warning("skipping malformed line in %s: %s", path, e)
    return rows


def _open_output_file_0600(path: Path) -> int:
    """Same idempotent helper as llm_trace._open_trace_file."""
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return fd


# ---------------------------------------------------------------------------
# Envelope filtering
# ---------------------------------------------------------------------------


def _select_request(rows: list[dict], iter_n: int, attempt: str) -> dict | None:
    """Pick phase=request row for given iter+attempt. Returns None if missing."""
    for r in rows:
        if (r.get("phase") == "request"
                and r.get("iter") == iter_n
                and r.get("attempt") == attempt):
            return r
    return None


def _select_response(rows: list[dict], iter_n: int, attempt: str) -> dict | None:
    """Pick phase=response (preferred) или phase=error row."""
    for r in rows:
        if (r.get("phase") in ("response", "error")
                and r.get("iter") == iter_n
                and r.get("attempt") == attempt):
            return r
    return None


# ---------------------------------------------------------------------------
# Message deserialization (uses llm_trace serialization helpers)
# ---------------------------------------------------------------------------


def _messages_from_envelope(request: dict) -> list:
    from sreda.services.llm_trace import _msg_from_jsonable  # type: ignore
    return [_msg_from_jsonable(d) for d in request["messages"]]


def _merge_call_kwargs(envelope_request: dict) -> dict:
    """R7 (M-R6-2): replay precedence order. Verified test in
    test_envelope_builders.test_invocation_kwargs_override_bind."""
    merged: dict[str, Any] = {}
    ck = envelope_request.get("client_kwargs") or {}
    merged.update({k: v for k, v in ck.items() if v is not None})
    bk = envelope_request.get("bound_kwargs") or {}
    merged.update(bk)
    ik = envelope_request.get("invocation_kwargs") or {}
    merged.update(ik)
    return merged


# ---------------------------------------------------------------------------
# Diff output
# ---------------------------------------------------------------------------


def _print_token_delta(orig_usage: dict, replay_usage: dict) -> None:
    print("")
    print("=== Token usage delta ===")
    print(f"  Original: in={orig_usage.get('input_tokens', '?')} "
          f"out={orig_usage.get('output_tokens', '?')} "
          f"cached={orig_usage.get('cache_read', 0)}")
    print(f"  Replay:   in={replay_usage.get('input_tokens', '?')} "
          f"out={replay_usage.get('output_tokens', '?')} "
          f"cached={replay_usage.get('cache_read', 0)}")


def _print_diff(captured_response: dict, replay_response: dict,
                show_content: bool) -> None:
    print("")
    print("=== Tool calls diff ===")
    orig_tcs = list(captured_response.get("tool_calls") or [])
    replay_tcs = list(replay_response.get("tool_calls") or [])
    # PII gating: tool_call args contain decrypted user text + memories
    # (the same surface as content). Hide them behind --show-content.
    # See Codex review on PR #48 (MAJOR — replay_llm_turn.py:164-176).
    if show_content:
        orig_tools = [(tc.get("name"), tc.get("args")) for tc in orig_tcs]
        replay_tools = [(tc.get("name"), tc.get("args")) for tc in replay_tcs]
        print(f"  Original tool_calls: {orig_tools}")
        print(f"  Replay tool_calls:   {replay_tools}")
    else:
        orig_names = [tc.get("name") for tc in orig_tcs]
        replay_names = [tc.get("name") for tc in replay_tcs]
        print(f"  Original tool_calls: count={len(orig_tcs)} names={orig_names}")
        print(f"  Replay tool_calls:   count={len(replay_tcs)} names={replay_names}")
        print("  (tool args suppressed — use --show-content to see PII)")
    if not show_content:
        print("")
        print("(content diff suppressed — use --show-content to see PII)")
        return
    print("")
    print("=== Content diff (unified) ===")
    orig = str(captured_response.get("content", "") or "")
    repl = str(replay_response.get("content", "") or "")
    diff_lines = list(difflib.unified_diff(
        orig.splitlines(keepends=True),
        repl.splitlines(keepends=True),
        fromfile="captured", tofile="replay", n=3,
    ))
    if not diff_lines:
        print("  (identical content)")
    else:
        print("".join(diff_lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _interactive_confirm(prompt: str) -> bool:
    try:
        ans = input(prompt + " [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--date", help="YYYY-MM-DD; default: scan all day folders")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iter", type=int, default=0, dest="iter_n")
    parser.add_argument("--attempt", default="primary",
                        choices=["primary", "fallback"])
    parser.add_argument("--provider", default=None,
                        help="Provider key (mimo / mimo-v2.5 / openrouter / ...)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--allow-cross-provider", action="store_true",
                        help="Required если provider/model отличается от captured")
    parser.add_argument("--diff", action="store_true",
                        help="Show side-by-side diff с captured response")
    parser.add_argument("--output", type=Path,
                        help="Path для diff output (mode 0600 на create)")
    parser.add_argument("--show-content", action="store_true",
                        help="Explicit opt-in для PII content в stdout")
    parser.add_argument("--verbose", "-v", action="store_true")
    # NB: there is intentionally NO `--no-confirm` flag here. Cross-provider
    # PII replay must always prompt operators in production. Tests bypass
    # the prompt by monkeypatching ``_interactive_confirm`` — that path
    # cannot be triggered from an env var an attacker / automation might set.
    # See Codex review on PR #48 R2 (MAJOR — replay_llm_turn.py --no-confirm
    # env gate is spoofable).
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    files = _find_trace_files(args.root, args.trace_id, args.date)
    if not files:
        logger.error("trace not found: trace_id=%s date=%s root=%s",
                     args.trace_id, args.date, args.root)
        return 2
    if len(files) > 1:
        logger.warning("multiple matches found, using first: %s", files[0])
    rows = _load_jsonl(files[0])
    request_row = _select_request(rows, args.iter_n, args.attempt)
    if request_row is None:
        logger.error("phase=request row not found для iter=%d attempt=%s",
                     args.iter_n, args.attempt)
        return 3
    captured_request = request_row["request"]
    captured_provider = captured_request.get("provider")
    captured_model = captured_request.get("model")

    target_provider = args.provider or captured_provider
    target_model = args.model or captured_model
    cross_provider = (
        (args.provider is not None and args.provider != captured_provider)
        or (args.model is not None and args.model != captured_model)
    )
    if cross_provider:
        if not args.allow_cross_provider:
            logger.error(
                "cross-provider replay requires --allow-cross-provider. "
                "captured: provider=%s model=%s; requested: provider=%s model=%s",
                captured_provider, captured_model, args.provider, args.model,
            )
            return 4
        print(f"⚠ Cross-provider replay: "
              f"{captured_provider}/{captured_model} → "
              f"{target_provider}/{target_model}")
        print(f"⚠ Decrypted PII включая memories будет отправлен NEW provider.")
        print(f"⚠ Если new provider в другой юрисдикции — может нарушить data residency.")
        # Always go through _interactive_confirm. Tests monkeypatch it to
        # return True; production operators see the actual TTY prompt. No
        # bypass flag — env-gated bypass is spoofable. See Codex review on
        # PR #48 R2 (MAJOR — --no-confirm env gate is not a real guard).
        if not _interactive_confirm("Continue?"):
            logger.error("user declined cross-provider replay")
            return 5

    # Now run replay invoke (read-only, no DB)
    messages = _messages_from_envelope(captured_request)
    merge_kwargs = _merge_call_kwargs(captured_request)
    # tool_schemas — bind as inert dicts (НЕ production callables)
    tool_schemas = captured_request.get("tool_schemas") or []

    # Build standalone LLM. Pass ALL reproducible client kwargs — иначе
    # replay output может отличаться по причинам не связанным с моделью
    # (other top_p, missing extra_body, default timeout) и CLI становится
    # unreliable для debug. See Codex review on PR #48 (MAJOR —
    # replay_llm_turn.py: replay is not exact replay).
    from sreda.services.llm import get_chat_llm  # lazy import after no-DB guarantee
    # Whitelist of constructor-level kwargs accepted by ChatOpenAI /
    # MimoChatOpenAI. Anything not in this set is per-call (invocation_kwargs).
    #
    # ``timeout_seconds`` intentionally NOT included: the envelope captures
    # it under that key from ``cur.request_timeout``, but the runtime
    # timeout is enforced by the local wrapper ``ainvoke_with_streaming_timeout``
    # (asyncio.wait_for), NOT by the LangChain client. Forwarding it would
    # land in **kwargs of ChatOpenAI which has no such parameter — best-case
    # an obvious TypeError, worst-case silent provider-side error. Replay
    # is "best-effort" for this field; if needed in future, wrap the local
    # ``llm.invoke`` with a separate asyncio.wait_for. See Codex review on
    # PR #48 R2 (MAJOR — timeout_seconds replayed as constructor + invoke).
    _CONSTRUCTOR_KWARGS = {
        "temperature", "top_p", "seed", "stop", "max_tokens",
        "tool_choice", "parallel_tool_calls", "response_format",
        "extra_body", "base_url",
    }
    chat_llm_kwargs: dict[str, Any] = {}
    for k, v in merge_kwargs.items():
        if v is None:
            continue
        if k in _CONSTRUCTOR_KWARGS:
            chat_llm_kwargs[k] = v
    # Ensure temperature defined даже если captured envelope не имел его.
    chat_llm_kwargs.setdefault("temperature",
                               merge_kwargs.get("temperature", 0.3))

    try:
        llm = get_chat_llm(
            provider=target_provider,
            model=target_model,
            **chat_llm_kwargs,
        )
    except TypeError as exc:
        # Unknown kwarg для current LangChain — fallback с минимумом,
        # warn пользователя что replay best-effort.
        logger.warning(
            "get_chat_llm rejected captured kwargs (%s) — falling back to "
            "temperature-only (best-effort replay); error: %s",
            sorted(chat_llm_kwargs.keys()), exc,
        )
        llm = get_chat_llm(
            provider=target_provider,
            model=target_model,
            temperature=chat_llm_kwargs.get("temperature", 0.3),
        )
    if llm is None:
        logger.error("get_chat_llm returned None для provider=%s model=%s — "
                     "check API key configured", target_provider, target_model)
        return 6
    if tool_schemas:
        llm = llm.bind_tools(tool_schemas)

    # Per-call invocation kwargs (LangChain semantics: ainvoke(messages, **kw)
    # overrides .bind() and constructor kwargs). Replay must reproduce this,
    # BUT some recorded "invocation kwargs" are wrapper-only metadata, not
    # arguments that the underlying LangChain.invoke() understands:
    #
    # * ``timeout_seconds`` — handlers.py stores it in invocation_kwargs for
    #   trace audit, but at runtime it's consumed by the local
    #   ``ainvoke_with_streaming_timeout`` wrapper (asyncio.wait_for). It is
    #   NOT forwarded to ChatOpenAI / MimoChatOpenAI ``invoke``. Passing it
    #   here would land in **kwargs of the provider client and likely raise.
    #
    # Strip wrapper-only keys before invocation. See Codex review on PR #48 R2
    # (MAJOR — timeout_seconds replayed as both constructor and invoke kwarg).
    raw_invocation_kwargs = dict(captured_request.get("invocation_kwargs") or {})
    _WRAPPER_ONLY_KEYS = {"timeout_seconds"}
    invocation_kwargs = {
        k: v for k, v in raw_invocation_kwargs.items()
        if k not in _WRAPPER_ONLY_KEYS
    }
    if raw_invocation_kwargs.keys() & _WRAPPER_ONLY_KEYS:
        logger.debug(
            "stripped wrapper-only keys from invocation_kwargs before replay: %s",
            sorted(raw_invocation_kwargs.keys() & _WRAPPER_ONLY_KEYS),
        )

    # Invoke (sync — debug context)
    try:
        if invocation_kwargs:
            replay_ai_msg = llm.invoke(messages, **invocation_kwargs)
        else:
            replay_ai_msg = llm.invoke(messages)
    except Exception as e:
        logger.exception("replay invoke failed: %s", e)
        return 7

    replay_response = {
        "content": str(getattr(replay_ai_msg, "content", "") or ""),
        "tool_calls": list(getattr(replay_ai_msg, "tool_calls", None) or []),
        "additional_kwargs": dict(getattr(replay_ai_msg, "additional_kwargs", {}) or {}),
    }
    usage_meta = getattr(replay_ai_msg, "usage_metadata", None) or {}
    replay_usage = {
        "input_tokens": int(usage_meta.get("input_tokens") or 0),
        "output_tokens": int(usage_meta.get("output_tokens") or 0),
    }
    cache_read = (usage_meta.get("input_token_details") or {}).get("cache_read")
    if cache_read:
        replay_usage["cache_read"] = int(cache_read)

    # Compose output
    output_lines = [
        "=== Replay summary ===",
        f"  trace_id:           {args.trace_id}",
        f"  iter:               {args.iter_n}",
        f"  attempt:            {args.attempt}",
        f"  captured provider:  {captured_provider} / {captured_model}",
        f"  replay provider:    {target_provider} / {target_model}",
        "",
    ]
    text = "\n".join(output_lines)
    print(text)

    if args.diff:
        response_row = _select_response(rows, args.iter_n, args.attempt)
        if response_row is None:
            logger.warning("no captured response — skipping diff")
        else:
            captured_response = response_row.get("response") or {}
            captured_usage = response_row.get("usage") or {}
            _print_diff(captured_response, replay_response, args.show_content)
            _print_token_delta(captured_usage, replay_usage)

    if args.output:
        try:
            fd = _open_output_file_0600(args.output)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(text + "\n")
                if args.diff and args.show_content:
                    f.write(json.dumps(replay_response, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("output write failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
