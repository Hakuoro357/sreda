"""DB-to-PromptBuildArgs reconstruction for offline replay harness (#87).

TODO (Phase B — requires VDS SSH + prod DB read-only access):
    This module rebuilds a ``PromptBuildArgs`` from the exported JSONL data
    produced by ``probe_replay_export.py``.  It connects to the LOCAL
    quarantine export (never to prod directly) and reconstructs:

    - user_message        (plaintext from agent_runs.input_json.source_value)
    - history window      (TurnSnapshot list, chronological cutoff)
    - profile snapshot    (ProfileSnapshot from current profile tables)
    - memory snapshot     (re-recalled via bge-m3 with current memory)
    - now moment          (historical turn.created_at + tz/locale)
    - voice meta          (from agent_runs.input_json voice fields)
    - baseline alignment  (old bot reply from outbox_messages.payload_json)

    Reconstruction fidelity gate (§4.3): each case carries per-field status
    {user_text, now+tz+locale, profile, memory_provenance, history_window,
    active_turn, voice_meta, baseline_alignment, decrypted_ok}.
    Gate: 0 missing CRITICAL fields on Boris-tenant smoke (5-10 turns).

Phase B implementation notes:
    - Use read-only DB role / transaction (§2 safety guard).
    - Decrypt v2: payloads via sreda.services.encryption.decrypt_value.
    - No real DB DSN in code — read from REPLAY_DSN env var.
    - Assert REPLAY_MODE=1 before any DB connection.
    - Verify against live schema from probe_runbook before build (§4.1).

Dependencies (Phase B only):
    sreda.services.encryption.decrypt_value
    sreda.db.session (read-only role)
    sreda.runtime.planner.prompt_builder.PromptBuildArgs
"""

# Phase A: no implementation — stubs only.

raise NotImplementedError(
    "reconstruct.py is Phase B — requires VDS SSH + prod DB read-only access.  "
    "Do not run during Phase A."
)
