"""VDS read-only export for offline replay harness (#87).

TODO (Phase B — requires VDS SSH access; runs ON the VDS via run_probe.sh):
    Export archived turns for the replay harness.  Reads from prod DB with a
    read-only role/transaction.  Writes to a local quarantine JSONL file
    (outside the repo — never committed).

    Per §4.1 sources:
    - user_message  from agent_runs.input_json.source_value (plaintext)
    - full input modality (voice_meta, callbacks) from agent_runs.input_json
    - history window from conversation_turns / agent_runs (chronological)
    - old bot reply from outbox_messages.payload_json (decrypt v2:)
    - old tool_calls from /var/log/sreda/trace.log (unreliable; coverage audit)

    PII: exported JSONL lives ONLY in local quarantine dir (§15).
    The quarantine path is set via REPLAY_QUARANTINE_DIR env var.

Phase B implementation notes:
    - Assert REPLAY_MODE=1 before any connection.
    - Use read-only DB transaction.
    - Use stable correlation id (run_id / turn_id) for baseline alignment (§4.2).
    - Export opaque turn-id hashes (SHA-256 prefix) alongside raw data so
      the repo-side manifest can reference turns without PII.
    - Verify live schema before build (§4.1 note).
"""

# Phase A: no implementation — stubs only.

raise NotImplementedError(
    "probe_replay_export.py is Phase B — requires VDS SSH + prod DB access.  "
    "Do not run during Phase A."
)
