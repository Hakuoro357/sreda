# Retention & Privacy Policy — Plan-Execute artifacts

**Owner:** Boris Печорин (boris@sredaspace.ru)
**Last updated:** 2026-05-25
**Scope:** All persistence introduced by Plan-Execute Epic (`Hakuoro357/vex-assistant#74`) plus the legacy ``user_data_change_feed`` already in production.
**Regulatory base:** 152-ФЗ Part 2 («Уничтожение персональных данных по достижении цели обработки»).

This document is the single source of truth for what gets stored where, for how long, encrypted or not, and how to purge it. Any new persistence added by future Phase A sub-issues must extend this table before being merged.

---

## Field classification

Three PII tiers, applied per column:

| Class | What | Encryption | Indexing |
|---|---|---|---|
| **1 — High PII** | Raw user text, message contents, decrypted entity titles, full plan/execution payloads | `v2:` at-rest (via `services.encryption.encrypt_value`) | NEVER on encrypted columns. Add a separate hash column if search is needed. |
| **2 — Medium PII** | Structural metadata (template ids, plan_trace_ids, fencing tokens, model names, latencies, counts) | Plaintext | Allowed |
| **3 — System metadata** | UUIDs, foreign keys, timestamps without user content | Plaintext | Allowed |

## Per-table retention & encryption

| Table | Class-1 fields | Retention | Cleanup mechanism | Source sub-issue |
|---|---|---|---|---|
| `agent_runs` | `input_json`, `context_json`, `result_json`, `error_message_sanitized` | 90 days (pre-existing) | Existing retention worker | pre-existing |
| `inbound_messages` | encrypted payload via `secure_records` | 90 days | Existing retention worker | pre-existing |
| `outbox_messages` | `payload_json` (v2:) | 90 days | Existing retention worker | pre-existing |
| `secure_records` | `encrypted_json` (v2:) | 90 days | Existing retention worker | pre-existing |
| `user_data_change_feed` | `payload` (entity title, content) | 30 days | DELETE cron (per Group 6.4 from plan; not yet implemented) | Category I |
| `audit_outbox` | same as `user_data_change_feed` | Until relayed + 7 days DLQ | Relay worker + DLQ archive | Category I |
| `message_jobs` | `message_payload.payload` (raw Telegram update) | 7 days after status ∈ {done, failed, dead_letter} | DELETE cron (planned for Sub-A4 deploy phase) | Sub-A2 (#76) |
| `planner_executions` | `raw_planner_response`, `plan_json`, `execution_log_json` | 30 days | DELETE cron | Sub-A7 (planned) |
| `planner_gaps` | `user_message`, `tool_args_json`, `actual_result_json` | Until `status` ∈ {patched, wontfix} + 90 days (GEPA training corpus) | Manual + scheduled cleanup | Sub-A7 (planned) |
| `conversation_turns` | `summary` (v2:) | Bessrochno (analytics + recovery) | None — closed-turn `summary` retained for `agent_threads` lifetime | Sub-A6 (planned) |
| `agent_runs.turn_id` (FK) | n/a | inherits `agent_runs` | inherits | Sub-A6 (planned) |
| LangGraph checkpoint table | graph state (may contain plan + execution snapshot) | While job active + 24 h after `done` | PostgresSaver native cleanup | Sub-A6 (planned) |
| `#68` LLM traces (filesystem) | full request + response envelopes with PII | 5 days | `sreda-llm-traces-cleanup.timer` (deployed) | issue #68 |

## Encryption rules

* **At-rest encryption:** every column flagged as Class 1 is wrapped with `encrypt_value(text) → v2:<key_id>:<nonce_b64>:<ciphertext_b64>` (`services/encryption.py`). Reads call `decrypt_value(v2:...)`.
* **JSONB Class-1 fields** (`plan_json`, `execution_log_json`, `payload`) are encrypted as the **whole serialized JSON string**, NOT per sub-field. Simpler migration story, no risk of missing a nested key.
* **No index on encrypted columns.** If we need to search by user message text or similar, add a parallel `*_hash` column with a hash of the plaintext (e.g. SHA-256 truncated to 16 bytes). Hash is enough for dedup / lookup without leaking plaintext.
* **Decryption never logs plaintext.** Logging decrypted values to `uvicorn.log` or `/var/log/sreda/trace.log` is a breach; admin pages decrypt only at render time, never in cookies/cached HTML.
* **Backups** rely on Timeweb's volume-level encryption. Filesystem `/var/lib/sreda/private/` is `chmod 0700`, all files `chmod 0600` (enforced by `_open_trace_file` + tmpfiles config).

## Cleanup verification

For each table with explicit retention:

1. The cleanup mechanism (cron / relay / native) must log when it ran and how many rows it removed. Audit log goes to `uvicorn.log` (structured) and counts surface in admin metrics.
2. A weekly QA check (operator runbook in `docs/runbooks/`) sample-counts rows older than the retention window — expected zero. Non-zero triggers a P1 admin alert.
3. Backups beyond the retention window get re-encrypted with a forward-rotated key, and old keys are destroyed annually — operationally simpler than per-row scrub.

## Cross-process / external transmission

| Destination | Allowed payload | Notes |
|---|---|---|
| LLM providers (MiMo, Claude when used) | Plaintext user text, profile, memories, plan structure | TLS in transit. Provider stores prompts ≤ 30 days (MiMo) / 0 days (Claude with no-retention flag). Boris's contract reviewed and accepted. |
| GEPA training | Decrypted `planner_gaps` data | Local processing only, no external service. |
| Admin TG channel (chat_id 352612382) | Sanitized alert text only — never raw user content | `send_admin_alert` truncates, no decryption inside formatter. |
| Backup storage (Timeweb) | Encrypted at-rest only | Volume-level encryption + per-row v2 where applicable. |
| Logs (`uvicorn.log`, `/var/log/sreda/trace.log`) | Metadata only, NO raw user text | `error_message_sanitized` column convention enforces this on the persistence side; logging discipline enforced via code review. |

## Right-to-erasure (юзер просит удалить)

**Current state (2026-05-25):** manual procedure. Boris executes a script that:

1. Finds all rows across listed tables WHERE `tenant_id = ?`.
2. Deletes them in dependency order (children → parents).
3. Records the erasure event in `audit_log` for compliance trail.

**Post-MVP (Task #29 in epic):** automate via `scripts/erase_tenant.py` with dry-run + irreversible confirmation flow.

## When to update this doc

Add a row for any new table that stores user-derived data. The PR review checklist (CI gate to be added) verifies the doc's table count matches the database schema's PII-bearing tables.

If the answer to «can this column ever contain raw user text or a decrypted business identifier?» is yes — it's Class 1, encrypt it, document the retention.

## References

* Architecture plan with the full Group 7 (PII/Retention) decision: `plans/mellow-discovering-conway.md` section «Privacy / Retention / Encryption».
* Encryption module: `src/sreda/services/encryption.py`
* Trace file lifecycle: `docs/runbooks/llm-trace-retention.md` (issue #68 deliverable)
* 152-ФЗ ст. 5, 21 (минимизация, уничтожение): <https://legalacts.ru/doc/152_FZ-o-personalnyh-dannyh/>
