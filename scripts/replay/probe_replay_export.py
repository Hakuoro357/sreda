"""VDS read-only EXPORT for the offline-replay harness (#87, Phase B).

PURE READ-ONLY prod EXPORT.  The ONLY job of this script: read archived
turns from the prod DB (read-only role/transaction) and write a JSONL file
of :class:`reconstruct.ReplayExportRecord` objects to a local quarantine
directory OUTSIDE the git repo.

It does NOT replay, score, run invariants, plan, or compare old-vs-new —
those live in other files (``run_replay.py`` / ``invariants.py`` /
``scoring.py``).  This script only EXPORTS raw reconstructed turns.

Structure (testability — §"Structure for testability"):
    * :func:`build_export_record` — PURE assembly.  Takes an already-fetched
      raw-row dict and returns a ``ReplayExportRecord`` + the per-field
      ``reconstruction_status``.  No DB, no network — unit-tested with
      synthetic fixtures.
    * :func:`fetch_raw_turns` / :func:`main` — thin read-only DB layer that
      runs the queries and feeds rows into ``build_export_record``.

Source mapping (§4.1; verified against models + the export_archive.py /
diag_replies.py prototypes + probe runbook):
    * user_message   ← ``agent_runs.input_json.source_value`` (plaintext;
                       falls back to ``input_json.params.text``)
    * voice          ← ``agent_runs.input_json`` voice meta / source_type
    * now_iso        ← ``agent_runs.created_at`` (tz-aware UTC) converted to
                       the user tz, rendered tz-aware ISO-8601.  NOTE:
                       ``conversation_turns`` does NOT exist on prod — history
                       is reconstructed from ``agent_runs`` only.
    * timezone_name  ← ``tenant_user_profiles.timezone`` (IANA)
    * locale         ← stored BCP-47 locale (column absent on prod → "ru")
    * profile        ← current ``tenant_user_profiles`` row
    * memories       ← Raw current-memory dump WITH stored embeddings.
                       Recall-ranking equivalence (cosine top_k=10,
                       min_score=0.1 by the turn query) is DEFERRED to
                       run_replay.py / reconstruct, which selects the same
                       top-k OFFLINE from these embeddings — the exporter does
                       NOT rank (stays read-only/dumb).
    * history        ← prior completed runs of the same thread, chronological
    * baseline       ← ``outbox_messages.payload_json`` (decrypt v2:) +
                       archived tool calls (trace availability drives §8
                       tool invariants, NOT the CRITICAL baseline_alignment)

Safety / PII (§2, §15):
    * Refuses to start unless ``REPLAY_MODE=1``.
    * Reads with a READ-ONLY transaction (``SET TRANSACTION READ ONLY``);
      never writes to prod.
    * Refuses if a write-capable env (real bot tokens) appears loaded.
    * Output JSONL goes to ``REPLAY_QUARANTINE_DIR`` — refuses to write
      anywhere inside the git repo.  The file is PII and must NEVER be
      committed.
    * ``turn_id_hash`` = SHA-256 hex prefix (16 chars) of the real turn id.
    * The real tenant id lives ONLY in the quarantine JSONL; the
      ``tenant_placeholder`` field exposed to the repo is redacted.

Run ON the VDS via ``run_probe.sh`` (loads ``/etc/sreda/.env`` first).
This module is import-safe: importing it has NO side effects (no DB, no env
assertion).  All guards live in :func:`main`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import the contract (ReplayExportRecord) + the app's own modules.
# sys.path is set up so the module imports cleanly both on the VDS
# (scripts/ context) and from local tests.
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from reconstruct import ReplayExportRecord  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-field reconstruction status vocabulary (must match reconstruct.py).
_OK = "ok"
_MISSING = "missing"
_DEGRADED = "degraded"

# §15 — redacted tenant placeholder exposed to the repo-side manifest.
TENANT_PLACEHOLDER = "tenant_tg_<SMOKE>"

# §4.1 fallback defaults when tz/locale are not stored.
_DEFAULT_TZ = "Europe/Moscow"
_DEFAULT_LOCALE = "ru"

# Default quarantine dir — clearly OUTSIDE any repo (system tmp).
_DEFAULT_QUARANTINE_DIR = "/tmp/sreda-replay-quarantine"

# Voice source_type discriminators (Telegram / MAX).
_VOICE_SOURCE_TYPES = frozenset({"telegram_voice", "max_voice"})


# ---------------------------------------------------------------------------
# Small helpers (pure)
# ---------------------------------------------------------------------------


def hash_turn_id(turn_id: str) -> str:
    """Opaque correlation id: first 16 hex chars of SHA-256 of the turn id.

    Stable (same input → same output) and one-way — no PII leaks via the
    hash.  Used as ``turn_id_hash`` so the repo-side manifest can reference a
    turn without exposing the real id.
    """
    return hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:16]


def _as_dict(val: Any) -> dict[str, Any]:
    """Coerce a JSON column value (dict from psycopg JSONB, or a JSON string)
    to a plain dict.  Returns ``{}`` on None / parse failure.

    Mirrors ``export_archive.py._as_dict`` — agent_runs.input_json is a Text
    column storing a JSON string, while some JSONB columns arrive as dicts.
    """
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_iso_aware(value: Any, *, tz_name: str) -> str | None:
    """Render a stored timestamp as tz-AWARE ISO-8601 with offset.

    ``value`` may be a ``datetime`` (psycopg returns aware datetimes for
    ``timestamptz`` columns) or an ISO string.  Returns ``None`` when the
    value cannot be parsed.

    AWARE datetimes (prod stores ``created_at`` as tz-aware UTC) are CONVERTED
    into ``tz_name`` so the rendered wall-clock matches the user's zone; NAIVE
    ones are localized AS ``tz_name``.  The consumer-side validator in
    reconstruct.py re-checks tz/locale validity.
    """
    dt: datetime | None = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — tz DB missing / unknown zone
        zone = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)        # naive: interpret AS the user zone
    else:
        dt = dt.astimezone(zone)            # aware (UTC): convert INTO user zone
    return dt.isoformat()


def _decrypt_safe(
    raw: Any,
    decrypt: Callable[[str], str],
) -> tuple[str, bool]:
    """Decrypt an outbox ``payload_json`` value → ``(reply_text, ok)``.

    ``payload_json`` is a ``v2:``-encrypted STRING whose plaintext is a JSON
    object ``{"text": ...}`` (export_archive.py).  Decrypt the WHOLE string
    first, THEN parse JSON, THEN read ``text``.

    Returns ``ok=False`` on any decrypt/parse failure so the caller can set
    ``decrypted_ok="missing"`` and continue (never crash the whole export).
    Already-plaintext dicts/strings are handled too (ORM TypeDecorator may
    have decrypted transparently depending on read path).
    """
    if not raw:
        return "", True  # nothing to decrypt — not a failure
    obj: Any
    if isinstance(raw, str) and raw.startswith(("v2:", "v1:")):
        try:
            plain = decrypt(raw)
            obj = json.loads(plain)
        except Exception:  # noqa: BLE001 — any decrypt/parse error
            return "", False
    else:
        obj = _as_dict(raw)
    if not isinstance(obj, dict):
        return "", False
    return (obj.get("text") or "").strip(), True


def _extract_user_message(input_data: dict[str, Any]) -> tuple[str, str]:
    """Return ``(user_message, status)`` from a decoded ``input_json`` dict.

    Prefers ``source_value`` (§4.1 / runbook); falls back to
    ``params.text`` (what the prod history loader + export_archive use).
    Status is ``ok`` when a non-empty message is found, else ``missing``.
    """
    source_value = input_data.get("source_value")
    if isinstance(source_value, str) and source_value.strip():
        return source_value.strip(), _OK
    params = input_data.get("params")
    if isinstance(params, dict):
        text = params.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip(), _OK
    return "", _MISSING


def _extract_voice(input_data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Return ``(voice_dict_or_None, status)``.

    Voice modality is identified by ``source_type`` (``telegram_voice`` /
    ``max_voice``) and/or an explicit ``voice_meta`` block.  When a
    confidence value is present → status ``ok``; when the turn is voice but
    confidence is unknown → ``degraded`` (we still flag the modality, but
    fidelity is partial — §4.1 Codex MAJOR: don't lose voice modality).
    Text turns → ``(None, "ok")``.
    """
    voice_meta = input_data.get("voice_meta")
    source_type = str(input_data.get("source_type") or "")
    is_voice = source_type in _VOICE_SOURCE_TYPES or bool(voice_meta)

    if not is_voice:
        return None, _OK

    confidence: float | None = None
    if isinstance(voice_meta, dict):
        raw_conf = voice_meta.get("confidence")
        if isinstance(raw_conf, (int, float)):
            confidence = float(raw_conf)

    if confidence is None:
        # Voice modality known, confidence not stored → flag, mark degraded.
        return {"is_voice": True, "confidence": 1.0}, _DEGRADED
    return {"is_voice": True, "confidence": confidence}, _OK


# ---------------------------------------------------------------------------
# PURE assembly: raw row dict → ReplayExportRecord
# ---------------------------------------------------------------------------


def build_export_record(
    raw_turn: dict[str, Any],
    *,
    tenant_placeholder: str = TENANT_PLACEHOLDER,
    decrypt: Callable[[str], str],
    default_timezone: str = _DEFAULT_TZ,
    default_locale: str = _DEFAULT_LOCALE,
) -> ReplayExportRecord:
    """Assemble a :class:`ReplayExportRecord` from an already-fetched raw row.

    PURE — NO DB, NO network.  This is the unit-tested core.

    ``raw_turn`` keys (all produced by :func:`fetch_raw_turns`):
        ``turn_id``           real turn/run id (hashed here; never stored raw)
        ``input_json``        raw ``agent_runs.input_json`` (str or dict)
        ``created_at``        historical timestamp (datetime or ISO str)
        ``timezone_name``     stored IANA tz (may be empty)
        ``locale``            stored BCP-47 locale (may be empty)
        ``profile``           {name,gender,address,timezone,language} dict
        ``memories``          [{content,source,score,saved_at,embedding}, ...]
                              (``embedding`` = parsed vector or None; deferred
                              offline recall ranks from it — reconstruct ignores it)
        ``history``           {active_turn, closed_turns}
        ``baseline_payloads`` list of raw outbox payload_json values (v2:)
        ``old_called_tools``  [str] archived tool calls (may be empty)
        ``old_side_effects``  [...] archived side effects (may be empty)
        ``baseline_aligned``  bool — was the baseline reply paired by a
                              stable correlation id (run_id/turn_id)?  False
                              ⟹ paired heuristically ⟹ baseline degraded.
        ``trace_reliable``    bool — were tool calls recovered from trace?
                              Tracked for §8 tool-dependent invariants only;
                              does NOT affect the CRITICAL baseline_alignment.

    The per-field ``reconstruction_status`` is computed here so the exporter
    is the single source of truth; reconstruct.py re-validates the CRITICAL
    ``now_tz_locale`` field consumer-side regardless of what we claim.
    """
    status: dict[str, str] = {}

    turn_id = str(raw_turn.get("turn_id") or "")
    turn_id_hash = hash_turn_id(turn_id) if turn_id else ""

    # --- user message -----------------------------------------------------
    input_data = _as_dict(raw_turn.get("input_json"))
    user_message, status["user_text"] = _extract_user_message(input_data)

    # --- voice modality ---------------------------------------------------
    voice, status["voice_meta"] = _extract_voice(input_data)

    # --- now / tz / locale ------------------------------------------------
    timezone_sourced = bool((raw_turn.get("timezone_name") or "").strip())
    timezone_name = (raw_turn.get("timezone_name") or "").strip() or default_timezone
    # Russian-only product: defaulted "ru" locale is the documented invariant,
    # not degraded (Codex R1: otherwise §4.3 CRITICAL gate can never pass).
    locale = (raw_turn.get("locale") or "").strip() or default_locale
    now_iso = _to_iso_aware(raw_turn.get("created_at"), tz_name=timezone_name)

    if now_iso is None:
        now_iso = ""
        status["now_tz_locale"] = _MISSING
    elif timezone_sourced:
        status["now_tz_locale"] = _OK
    else:
        status["now_tz_locale"] = _DEGRADED

    # --- profile (current-state, Q2) --------------------------------------
    profile_raw = raw_turn.get("profile")
    profile: dict[str, Any] = profile_raw if isinstance(profile_raw, dict) else {}
    status["profile"] = _OK if profile else _MISSING

    # --- memories (current re-recall, Q2) ---------------------------------
    memories_raw = raw_turn.get("memories")
    memories: list[dict[str, Any]] = (
        list(memories_raw) if isinstance(memories_raw, list) else []
    )
    # Provenance is "ok" once we have the list (incl. empty — empty memory is
    # a valid current state); "missing" only if the field was absent entirely.
    status["memory_provenance"] = _OK if isinstance(memories_raw, list) else _MISSING

    # --- history window ---------------------------------------------------
    history_raw = raw_turn.get("history")
    history: dict[str, Any] = (
        history_raw
        if isinstance(history_raw, dict)
        else {"active_turn": None, "closed_turns": []}
    )
    history.setdefault("active_turn", None)
    history.setdefault("closed_turns", [])
    status["history_window"] = _OK if isinstance(history_raw, dict) else _MISSING
    status["active_turn"] = _OK if history.get("active_turn") else _MISSING

    # --- baseline (old reply + tools + side effects) ----------------------
    reply_parts: list[str] = []
    decrypted_ok = True
    payloads = raw_turn.get("baseline_payloads") or []
    for payload in payloads:
        text, ok = _decrypt_safe(payload, decrypt)
        if not ok:
            decrypted_ok = False
            continue
        if text:
            reply_parts.append(text)
    old_reply = "\n".join(reply_parts)

    old_called_tools = list(raw_turn.get("old_called_tools") or [])
    old_side_effects = list(raw_turn.get("old_side_effects") or [])
    baseline: dict[str, Any] = {
        "old_reply": old_reply,
        "old_called_tools": old_called_tools,
        "old_side_effects": old_side_effects,
    }

    # decrypted_ok status (CRITICAL).
    if not payloads:
        # No baseline payload at all → cannot confirm decryption.
        status["decrypted_ok"] = _MISSING
    elif not decrypted_ok:
        status["decrypted_ok"] = _MISSING
    else:
        status["decrypted_ok"] = _OK

    # baseline_alignment status (CRITICAL) — §4.2 reply/run correlation ONLY.
    # Tool-trace availability is SEPARATE (old_called_tools empty ⟹ tool-dependent
    # invariants become `unknown` in §8); it must NOT poison this CRITICAL field.
    if not payloads or not old_reply:
        status["baseline_alignment"] = _MISSING
    elif not raw_turn.get("baseline_aligned", False):
        status["baseline_alignment"] = _DEGRADED
    else:
        status["baseline_alignment"] = _OK

    return ReplayExportRecord(
        turn_id_hash=turn_id_hash,
        tenant_placeholder=tenant_placeholder,
        user_message=user_message,
        now_iso=now_iso,
        timezone_name=timezone_name,
        locale=locale,
        voice=voice,
        profile=profile,
        memories=memories,
        history=history,
        baseline=baseline,
        reconstruction_status=status,
    )


# ---------------------------------------------------------------------------
# Safety guards (§2, §15)
# ---------------------------------------------------------------------------


class ReplaySafetyError(RuntimeError):
    """Raised when a hard safety guard refuses to let the export run."""


def assert_replay_mode(env: dict[str, str] | None = None) -> None:
    """Refuse to run unless ``REPLAY_MODE=1`` (§2)."""
    env = os.environ if env is None else env
    if env.get("REPLAY_MODE") != "1":
        raise ReplaySafetyError(
            "REPLAY_MODE is not '1'. This read-only export refuses to run "
            "without the explicit replay-mode flag (§2 safety guard)."
        )


def assert_no_write_env(env: dict[str, str] | None = None) -> None:
    """Best-effort refusal if a write-capable env appears loaded (§2).

    Real bot tokens have no place in a read-only export run.  This is a
    best-effort guard (it cannot prove a DSN is read-only), pairing with the
    ``SET TRANSACTION READ ONLY`` enforced on the session itself.
    """
    env = os.environ if env is None else env
    suspicious = [
        key
        for key in ("TELEGRAM_BOT_TOKEN", "MAX_BOT_TOKEN", "SREDA_BOT_TOKEN")
        if (env.get(key) or "").strip()
    ]
    if suspicious:
        raise ReplaySafetyError(
            "Write-capable bot token(s) detected in the environment: "
            f"{suspicious}. Refusing to start a read-only export with a "
            "write-capable env loaded (§2 safety guard)."
        )


def _git_repo_roots(start: Path) -> list[Path]:
    """Return all git-repo roots from ``start`` upward (handles submodules)."""
    roots: list[Path] = []
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            roots.append(candidate)
    return roots


def resolve_quarantine_dir(
    env: dict[str, str] | None = None,
    *,
    repo_anchor: Path | None = None,
) -> Path:
    """Resolve + validate the quarantine output directory (§15).

    Reads ``REPLAY_QUARANTINE_DIR`` (default ``/tmp/sreda-replay-quarantine``)
    and REFUSES any path inside the git repo — exported JSONL is PII and must
    never be committed.
    """
    env = os.environ if env is None else env
    raw = (env.get("REPLAY_QUARANTINE_DIR") or _DEFAULT_QUARANTINE_DIR).strip()
    out_dir = Path(raw).resolve()

    anchor = (repo_anchor or _THIS_DIR).resolve()
    for repo_root in _git_repo_roots(anchor):
        if out_dir == repo_root or repo_root in out_dir.parents:
            raise ReplaySafetyError(
                f"REPLAY_QUARANTINE_DIR {out_dir} is INSIDE the git repo "
                f"({repo_root}). Exported JSONL is PII and must live OUTSIDE "
                "the repo (e.g. /tmp). Refusing to write (§15)."
            )
    return out_dir


def safe_output_path(out_dir: Path, name: str) -> Path:
    """Reject absolute paths / separators / .. in the output name and ensure
    the final path stays inside out_dir (PII must not escape quarantine, §15)."""
    if (not name or name != Path(name).name or name in (".", "..")
            or "/" in name or "\\" in name):
        raise ReplaySafetyError(
            f"Unsafe --output-name {name!r}: must be a bare filename "
            "(no path separators, no '..', not absolute)."
        )
    out_path = (out_dir / name).resolve()
    if out_dir.resolve() not in out_path.parents:
        raise ReplaySafetyError(
            f"Resolved output path {out_path} escapes quarantine dir {out_dir}."
        )
    return out_path


# ---------------------------------------------------------------------------
# Live-schema verification (§4.1 note — catch drift, don't mis-export)
# ---------------------------------------------------------------------------

# Expected (table, columns) the export depends on.  Verified before any
# export so schema drift fails LOUDLY rather than silently mis-exporting.
_EXPECTED_SCHEMA: dict[str, tuple[str, ...]] = {
    "agent_runs": ("id", "thread_id", "tenant_id", "action_type", "status",
                   "input_json", "result_json", "created_at"),
    "outbox_messages": ("id", "tenant_id", "channel_type", "payload_json", "created_at"),
    "tenant_user_profiles": ("tenant_id", "user_id", "display_name",
                             "address_form", "timezone", "communication_style"),
    "assistant_memories": ("id", "tenant_id", "user_id", "tier", "content",
                           "source", "created_at", "embedding_json"),
}


def verify_live_schema(session: Any) -> None:
    """Introspect the live DB and confirm expected tables/columns exist.

    Fails LOUDLY (``ReplaySafetyError``) on drift so a renamed column is
    caught instead of silently producing empty/wrong exports (§4.1 note).
    Uses ``information_schema`` (read-only).
    """
    from sqlalchemy import text  # local import — keeps module import light

    for table, expected_cols in _EXPECTED_SCHEMA.items():
        rows = session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": table},
        ).all()
        present = {r[0] for r in rows}
        if not present:
            raise ReplaySafetyError(
                f"Schema drift: expected table {table!r} not found in the "
                "live DB (information_schema returned no columns)."
            )
        missing = [c for c in expected_cols if c not in present]
        if missing:
            raise ReplaySafetyError(
                f"Schema drift: table {table!r} is missing expected "
                f"column(s) {missing}. Live columns: {sorted(present)}."
            )


# ---------------------------------------------------------------------------
# Thin read-only DB layer
# ---------------------------------------------------------------------------


def _begin_read_only(session: Any) -> None:
    """Mark the current transaction READ ONLY, fail-closed on Postgres.

    On Postgres (prod) this HARD-FAILS if read-only cannot be set or verified —
    we refuse to run a prod export without read-only enforcement (§2).  On
    non-PG backends (SQLite test) it is a no-op — the statement is unsupported
    and we never write anyway.
    """
    from sqlalchemy import text

    name = getattr(getattr(getattr(session, "bind", None), "dialect", None), "name", "")
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
    except Exception as exc:  # noqa: BLE001
        if name == "postgresql":
            raise ReplaySafetyError(
                f"Could not SET TRANSACTION READ ONLY on Postgres: {exc!r}. "
                "Refusing to run a prod export without read-only enforcement (§2)."
            ) from exc
        return
    if name == "postgresql":
        ro = session.execute(text("SHOW transaction_read_only")).scalar()
        if str(ro).lower() != "on":
            raise ReplaySafetyError(
                f"transaction_read_only={ro!r} after SET (expected 'on') — "
                "refusing to run without read-only enforcement (§2)."
            )


def _load_profile(
    session: Any, tenant_id: str, user_id: str, decrypt: Callable[[str], str]
) -> dict[str, Any]:
    """Current ``tenant_user_profiles`` row → profile dict (§4.1 Q2 current).

    Scoped by (tenant_id, user_id) — the runtime scopes profiles per user, so a
    tenant-only query would cross-contaminate PII on multi-user tenants.
    """
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT display_name, address_form, timezone, communication_style "
            "FROM tenant_user_profiles WHERE tenant_id = :t AND user_id = :u LIMIT 1"
        ),
        {"t": tenant_id, "u": user_id},
    ).fetchone()
    if row is None:
        return {}
    display_name_raw, address_form, tz, _comm_style = row
    name, _ = _decrypt_name(display_name_raw, decrypt)
    return {
        "name": name,
        "gender": "",  # not stored on prod profile
        "address": address_form or "",
        # RAW value — preserve provenance.  Empty when the column is null/absent
        # so an UNSOURCED tz stays empty (do NOT default to _DEFAULT_TZ here;
        # build_export_record degrades now_tz_locale on empty raw tz).
        "timezone": tz or "",
        "language": _DEFAULT_LOCALE,  # column absent on prod → ru
    }


def _decrypt_name(raw: Any, decrypt: Callable[[str], str]) -> tuple[str, bool]:
    """Decrypt an encrypted display_name (v2:) → (plaintext, ok)."""
    if not raw:
        return "", True
    if isinstance(raw, str) and raw.startswith(("v2:", "v1:")):
        try:
            return decrypt(raw), True
        except Exception:  # noqa: BLE001
            return "", False
    return str(raw), True


def _load_memories(
    session: Any, tenant_id: str, user_id: str, decrypt: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Current ``assistant_memories`` for the (tenant, user) → memory dicts.

    Raw current-memory dump WITH stored embeddings (§4.1 Q2 current).  This is
    NOT a bge-m3 cosine recall — it is the raw current memory state plus each
    memory's stored ``embedding_json`` vector.  Recall-ranking equivalence
    (cosine top_k=10, min_score=0.1 by the turn query) is DEFERRED to
    run_replay.py / reconstruct, which selects the same top-k OFFLINE from
    these embeddings — the exporter does NOT rank (stays read-only/dumb).
    Scoped by (tenant_id, user_id) — same per-user isolation as the runtime.
    """
    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT content, source, tier, created_at, embedding_json "
            "FROM assistant_memories "
            "WHERE tenant_id = :t AND user_id = :u ORDER BY created_at DESC"
        ),
        {"t": tenant_id, "u": user_id},
    ).all()
    out: list[dict[str, Any]] = []
    for content_raw, source, tier, created_at, embedding_json in rows:
        content, ok = _decrypt_name(content_raw, decrypt)
        if not ok:
            continue
        # Parse the stored vector (JSON-string).  None when absent/garbage so
        # the offline harness can tell "no embedding" from a real vector.  An
        # extra dict key does NOT break the ReplayExportRecord contract
        # (memories are list[dict]; reconstruct maps only the other keys).
        embedding: list | None = None
        if isinstance(embedding_json, str) and embedding_json.strip():
            try:
                parsed = json.loads(embedding_json)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                embedding = parsed
        out.append(
            {
                "content": content,
                "source": source or f"memory:{tier or 'core'}",
                "score": 0.0,  # raw export — recall scoring is consumer-side
                "saved_at": str(created_at) if created_at else "",
                "embedding": embedding,  # parsed vector or None (deferred recall)
            }
        )
    return out


def fetch_raw_turns(
    session: Any,
    *,
    tenant_id: str,
    decrypt: Callable[[str], str],
    limit: int | None = None,
    turn_ids: Iterable[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Read-only query → raw-row dicts ready for :func:`build_export_record`.

    The DB I/O lives here; the pure assembly is in ``build_export_record``.
    Profile + memories are scoped per (tenant_id, user_id) — derived from each
    run's ``input_json.user_id`` — and cached so multi-user tenants do not
    cross-contaminate PII.  History is reconstructed per run from prior
    completed runs of the same thread (chronological cutoff < the target run).
    """
    from sqlalchemy import text

    # Per-user (profile, memories) cache so we load each user's current state
    # at most once across the run set.
    user_ctx_cache: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    where = [
        "tenant_id = :t",
        "action_type = 'conversation.chat'",
        "status = 'completed'",
    ]
    params: dict[str, Any] = {"t": tenant_id}
    if turn_ids is not None:
        ids = list(turn_ids)
        where.append("id = ANY(:ids)")
        params["ids"] = ids
    if since:
        where.append("created_at >= :since")
        params["since"] = since
    if until:
        where.append("created_at <= :until")
        params["until"] = until

    sql = (
        "SELECT id, thread_id, created_at, input_json, result_json "
        "FROM agent_runs WHERE " + " AND ".join(where) +
        " ORDER BY created_at DESC"
    )
    if limit is not None:
        sql += " LIMIT :lim"
        params["lim"] = int(limit)

    runs = session.execute(text(sql), params).all()

    for run_id, thread_id, created_at, input_json_raw, result_json_raw in runs:
        input_data = _as_dict(input_json_raw)
        user_id = str(input_data.get("user_id") or "")

        ctx = user_ctx_cache.get(user_id)
        if ctx is None:
            profile = _load_profile(session, tenant_id, user_id, decrypt)
            memories = _load_memories(session, tenant_id, user_id, decrypt)
            ctx = (profile, memories)
            user_ctx_cache[user_id] = ctx
        profile, memories = ctx
        # Preserve tz provenance: keep an UNSOURCED tz EMPTY (no _DEFAULT_TZ
        # here) so build_export_record degrades now_tz_locale.  A genuinely
        # sourced tz flows through and is marked "ok".
        timezone_name = (profile.get("timezone") or "").strip()
        locale = profile.get("language") or _DEFAULT_LOCALE

        baseline_payloads, aligned = _load_baseline_payloads(
            session, result_json_raw, tenant_id
        )
        history = _load_history(
            session,
            tenant_id=tenant_id,
            thread_id=thread_id,
            before_run_id=run_id,
            before_created_at=str(created_at),
            timezone_name=timezone_name,
            decrypt=decrypt,
        )
        yield {
            "turn_id": run_id,
            "input_json": input_json_raw,
            "created_at": created_at,
            "timezone_name": timezone_name,
            "locale": locale,
            "profile": profile,
            "memories": memories,
            "history": history,
            "baseline_payloads": baseline_payloads,
            "old_called_tools": [],  # trace.log only — not in DB (runbook)
            "old_side_effects": [],
            "baseline_aligned": aligned,
            "trace_reliable": False,  # tool calls come from unreliable trace
        }


def _load_baseline_payloads(
    session: Any, result_json_raw: Any, tenant_id: str
) -> tuple[list[Any], bool]:
    """Resolve the old bot reply payload(s) via the run's correlation ids.

    Returns ``(payloads, aligned)`` where ``aligned`` is True ONLY when there
    were correlation ids AND every referenced ``outbox_message_ids`` row
    resolved for THIS tenant (§4.2).  Outbox rows are tenant-scoped so a
    missing or cross-tenant row can never produce a partial/mispaired baseline
    silently marked "ok".
    """
    from sqlalchemy import text

    result_data = _as_dict(result_json_raw)
    outbox_ids = result_data.get("outbox_message_ids") or []
    payloads: list[Any] = []
    resolved = 0
    for oid in outbox_ids:
        row = session.execute(
            text(
                "SELECT payload_json FROM outbox_messages "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": oid, "t": tenant_id},
        ).fetchone()
        if row is not None:
            resolved += 1
            if row[0]:
                payloads.append(row[0])
    # Aligned only when we had ids AND every referenced id resolved same-tenant.
    aligned = bool(outbox_ids) and resolved == len(outbox_ids)
    return payloads, aligned


def _load_history(
    session: Any,
    *,
    tenant_id: str,
    thread_id: str,
    before_run_id: str,
    before_created_at: str,
    timezone_name: str,
    decrypt: Callable[[str], str],
) -> dict[str, Any]:
    """Reconstruct the {active_turn, closed_turns} history window.

    Prior completed runs of the same thread, strictly BEFORE the target run
    (``created_at < before``), oldest-first — mirrors the prod history loader
    and export_archive's future-leak fix.  Each prior run becomes one
    TurnSnapshot-shaped closed turn with user+bot messages.
    """
    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT id, created_at, input_json, result_json FROM agent_runs "
            "WHERE tenant_id = :t AND thread_id = :tid "
            "AND action_type = 'conversation.chat' AND status = 'completed' "
            "AND id != :rid AND created_at < :before "
            "ORDER BY created_at DESC LIMIT 5"
        ),
        {
            "t": tenant_id,
            "tid": thread_id,
            "rid": before_run_id,
            "before": before_created_at,
        },
    ).all()

    closed_turns: list[dict[str, Any]] = []
    for run_id, created_at, input_json_raw, result_json_raw in reversed(rows):
        input_data = _as_dict(input_json_raw)
        user_text, _ = _extract_user_message(input_data)
        if not user_text:
            continue
        payloads, _ = _load_baseline_payloads(session, result_json_raw, tenant_id)
        bot_parts: list[str] = []
        for payload in payloads:
            text_val, ok = _decrypt_safe(payload, decrypt)
            if ok and text_val:
                bot_parts.append(text_val)
        bot_text = "\n".join(bot_parts)
        ts = _to_iso_aware(created_at, tz_name=timezone_name) or ""
        messages: list[dict[str, Any]] = [
            {"role": "юзер", "text": user_text, "ts": ts}
        ]
        if bot_text:
            messages.append({"role": "среда", "text": bot_text, "ts": ts})
        # prompt_builder._render_history renders CLOSED turns by `summary`
        # ONLY (closed-turn `messages` are NOT rendered), so pack a compact
        # transcript line into `summary` to keep prior dialogue alive.
        u = user_text[:300]
        b = bot_text[:300]
        summary_line = f"юзер: {u}" + (f" / среда: {b}" if b else "")
        closed_turns.append(
            {
                "turn_id": hash_turn_id(str(run_id)),
                "started_at": ts,
                "summary": summary_line,
                "messages": messages,
                "is_active": False,
            }
        )
    # The probe reconstruction treats prior runs as closed turns; the target
    # turn's own exchange is the active turn, but its bot reply is the
    # baseline (not history), so active_turn is None here unless an explicit
    # in-progress active turn is reconstructed (deferred — not needed for the
    # smoke; status will mark active_turn missing, which is non-critical).
    return {"active_turn": None, "closed_turns": closed_turns}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Read-only prod export for the offline-replay harness (#87). "
            "Writes a quarantine JSONL of ReplayExportRecord objects. "
            "Requires REPLAY_MODE=1."
        )
    )
    p.add_argument(
        "--tenant",
        required=True,
        help="Real tenant id to export (e.g. tenant_tg_<chat_id>). "
        "Goes ONLY into the quarantine JSONL, never the repo placeholder.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max turns to export (smoke: 5-10).",
    )
    p.add_argument(
        "--turn-ids",
        default=None,
        help="Comma-separated list of specific agent_run ids to export.",
    )
    p.add_argument("--since", default=None, help="ISO lower bound on created_at.")
    p.add_argument("--until", default=None, help="ISO upper bound on created_at.")
    p.add_argument(
        "--output-name",
        default="replay-export.jsonl",
        help="Filename written inside REPLAY_QUARANTINE_DIR.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  All safety guards live here (import is side-effect free)."""
    args = _build_arg_parser().parse_args(argv)

    # --- HARD safety guards (§2, §15) -------------------------------------
    assert_replay_mode()
    assert_no_write_env()
    out_dir = resolve_quarantine_dir()

    # App imports deferred until after guards pass (avoid touching prod
    # config/settings unless we are genuinely cleared to run).
    from sreda.db.session import get_session_factory
    from sreda.services.encryption import decrypt_value

    turn_ids = (
        [t.strip() for t in args.turn_ids.split(",") if t.strip()]
        if args.turn_ids
        else None
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = safe_output_path(out_dir, args.output_name)

    session_factory = get_session_factory()
    written = 0
    with session_factory() as session:
        _begin_read_only(session)
        verify_live_schema(session)

        with open(out_path, "w", encoding="utf-8") as fh:
            for raw_turn in fetch_raw_turns(
                session,
                tenant_id=args.tenant,
                decrypt=decrypt_value,
                limit=args.limit,
                turn_ids=turn_ids,
                since=args.since,
                until=args.until,
            ):
                record = build_export_record(raw_turn, decrypt=decrypt_value)
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                written += 1
        # NO commit — read-only by construction.

    print(
        f"Exported {written} turn(s) → {out_path}\n"
        "REMINDER: this file is PII (real tenant id + decrypted text). "
        "It lives in the quarantine dir and must NEVER be committed to the repo.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
