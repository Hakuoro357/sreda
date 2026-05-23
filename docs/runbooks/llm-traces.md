# Runbook: LLM Trace Logging (Issue #68)

> **Sensitivity**: содержит **decrypted PII** (memories, user text, tool args,
> tenant_id, user_id, channel chat_id). Файлы chmod 0600 + только под `sreda`
> user. **НЕ копировать off-box** ни в каком виде.

## Архитектура (TL;DR)

- Каждая LLM-итерация = до 4 envelope rows (request + response/error для
  primary + опционально fallback) в JSONL файле
  `/var/lib/sreda/private/llm-traces/YYYY-MM-DD/{trace_id}.jsonl`.
- Single writer task + asyncio.Queue + dedicated 1-thread executor →
  strict FIFO ordering per trace.
- `phase="request"` writes — strong semantics (`await persist_request_envelope`
  ждёт диск ДО `ainvoke` → crash-safe).
- `phase="response"`/`"error"` writes — fire-and-forget.
- Retention 5 days (systemd timer `sreda-llm-traces-cleanup.timer` daily).

Подробности: `plans/mellow-discovering-conway-final.md` (R7 approved
после 7 Codex review rounds).

## Feature flags

В `/etc/sreda/.env`:

| Env var | Default | Описание |
|---|---|---|
| `SREDA_LLM_TRACE_LOGGING_ENABLED` | `false` | Master switch. Если `false` — НИЧЕГО не пишется на диск. |
| `SREDA_LLM_TRACE_REQUIRE_PERSIST` | `false` | Compliance-strict: если `true` и persist degraded (queue full / timeout / error) — abort LLM call. Если `false` (default) — degraded persist → admin alert + continue (fail-open). |

⚠ `require_persist=true` BEZ `logging_enabled=true` → ValueError at FastAPI
startup (Settings model_validator). Misconfig fail-loud.

## Emergency disable (полное отключение фичи)

```bash
sudo sed -i '/^SREDA_LLM_TRACE_LOGGING_ENABLED=/d' /etc/sreda/.env
sudo sed -i '/^SREDA_LLM_TRACE_REQUIRE_PERSIST=/d' /etc/sreda/.env
sudo /opt/sreda/scripts/safe_restart.sh
```

Verify:
```bash
sudo systemctl status sreda-uvicorn | head -10
# Send test message, check NO new files appear
sudo ls /var/lib/sreda/private/llm-traces/$(date -u +%F)/ 2>/dev/null
```

## Compliance-strict mode (fail-closed)

Включить если data residency / 152-ФЗ audit требует **гарантированного**
trace на каждый LLM call:

```bash
echo 'SREDA_LLM_TRACE_LOGGING_ENABLED=true' | sudo tee -a /etc/sreda/.env
echo 'SREDA_LLM_TRACE_REQUIRE_PERSIST=true' | sudo tee -a /etc/sreda/.env
sudo /opt/sreda/scripts/safe_restart.sh
```

Trade-off: при queue full / disk full / writer task error — user получит
RuntimeError на LLM turn (bot молчит / возвращает error). Admin alert P1
посылается каждый раз. Используйте только если **regulator requires**.

## Backup policy

### НЕ копировать в backups

`/var/lib/sreda/private/llm-traces/` **никогда** не должна попадать в
backup artefacts. Текущие защитные слои:

1. **Filesystem path** — `/var/lib/sreda/private/` (FHS convention для
   приватных application data) — implicitly excluded в большинстве
   backup tools defaults.
2. **systemd-tmpfiles** + tmpfiles.d config — dir mode 0700, files mode 0600,
   owner `sreda:sreda`. Non-root users (включая backup user accounts) не
   могут читать.
3. **`scripts/backup_postgres.sh`** — содержит post-backup assertion:
   scans new tar artefacts for `llm-traces/` references; exit 1 + admin
   alert P0 если найдено.
4. **`scripts/validate_no_backup_leak.py`** — deploy-time validator.
   Parses ВСЕ известные backup/log-shipping configs (rsync, journald,
   filebeat, promtail, datadog-agent, borgmatic, etc.) с canonical paths
   resolution (`Path.resolve()`, symlink-safe, `/` ancestor explicit).
   Должен запускаться **до** каждого deploy:
   ```bash
   sudo /opt/sreda/.venv/bin/python /opt/sreda/scripts/validate_no_backup_leak.py
   ```
   Exit 1 = leak detected; deploy aborted.

### Если backup leak случился

1. Identify artefact: `validate_no_backup_leak.py` / `backup_postgres.sh`
   logs покажут exact file path.
2. Delete leaked artefact: `sudo shred -u /path/to/leaked.tar.gz`
3. Notify Boris через TG (admin alert уже отправлен автоматически).
4. Root cause: какой config added include path. Fix config + add explicit
   `exclude /var/lib/sreda/private/llm-traces`.
5. Re-run `validate_no_backup_leak.py` to confirm clean state.

## Retention policy

- 5-day rolling window.
- `sreda-llm-traces-cleanup.timer` (systemd) runs daily at 03:40 UTC.
- Parses folder names `YYYY-MM-DD` (UTC). NOT mtime — folder name = source of truth.
- Folders strictly older than `today_utc - 5 days` → `shutil.rmtree`.

Manual cleanup:
```bash
sudo /opt/sreda/.venv/bin/python /opt/sreda/scripts/cleanup_llm_traces.py \
    --root /var/lib/sreda/private/llm-traces --keep-days 5 --verbose
# Dry-run: добавить --dry-run
```

## Replay (debug в другую LLM)

```bash
# Same-provider sanity check
python /opt/sreda/scripts/replay_llm_turn.py --trace-id trace_xxx --root /var/lib/sreda/private/llm-traces

# Cross-provider — требует явный flag + интерактивный confirm
python /opt/sreda/scripts/replay_llm_turn.py --trace-id trace_xxx \
    --provider mimo-v2.5 --allow-cross-provider --diff --show-content
```

`--show-content` обязателен для diff content в stdout (PII opt-in).
`--output /path/log.txt` — file written с mode 0600.

## Diagnostics

### Что писать в admin

При degraded persist (`PersistResult != WRITTEN`):
- P1 alert каждый раз: `llm-trace request persist degraded: <result>`
- Содержит `trace_id`, `iter`, `attempt`, `result`
- Dedupe key: `llm_trace_persist_degraded:<result>` — alerts grouped by result type

### Проверить что writer alive

```bash
sudo journalctl -u sreda-uvicorn --since '1 min ago' \
    | grep -i 'llm-trace writer'
```

Expect: `llm-trace writer started, queue maxsize=1000` once per uvicorn process.

### Проверить disk usage

```bash
sudo du -sh /var/lib/sreda/private/llm-traces/
sudo du -sh /var/lib/sreda/private/llm-traces/*/
```

Acceptable: ~50 MB/day × 5 days = ~250 MB peak. Если > 1 GB — investigate
(возможно queue не drainит, или leak в _TRACE_STATE).

## Связано

- Plan: `plans/mellow-discovering-conway-final.md`
- Decision logs: `plans/archive/mellow-discovering-conway/decision-log-r{1..6}.md`
- Codex reviews: `plans/archive/mellow-discovering-conway/codex-review-r{1..6}.md`
- Issue: https://github.com/Hakuoro357/vex-assistant/issues/68
- Code: `src/sreda/services/llm_trace.py`, `src/sreda/runtime/handlers.py`
