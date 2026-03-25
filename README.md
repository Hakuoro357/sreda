# Sreda

Application skeleton for the `Sreda` MVP.

Current scope:
- `FastAPI` backend
- `LangGraph` runtime hooks
- `PostgreSQL` data layer
- `Telegram`-first message channel
- site monitoring integration to be rewritten in Python
- open-core with private feature packages

## Structure

- `src/sreda/` application code
- `tests/` test skeleton
- `scripts/dev/` local helper scripts
- `scripts/deploy/` deployment helpers for `Sreda`
- `migrations/` Alembic migration area

## Run

```powershell
.venv\Scripts\python -m uvicorn sreda.main:app --reload
```

## Seed local tenant

```powershell
$env:PYTHONPATH="src"
.venv\Scripts\python scripts\dev\seed_tenant.py --enable-eds-monitor --telegram-account-id <your_telegram_chat_id>
```

## Private feature packages

Additional proprietary features are expected to be installed as separate Python packages.

Example:

```powershell
$env:SREDA_FEATURE_MODULES="sreda_feature_eds_monitor.plugin"
```

Real EDS requests must run on the `Sreda` host. The local machine is only for development and dry checks.

## Privacy Guard

`Sreda` now contains a built-in regex-based `privacy guard` in:

- `src/sreda/services/privacy_guard.py`

Current policy:

- raw user text must not be sent to `LLM` directly;
- only sanitized text or sanitized payloads may be sent to `LLM`;
- the current `EDS monitor` summary flow already uses this guard before `LLM` calls.

Current redaction targets:

- phones
- email
- account numbers / personal account references
- passwords
- logins
- tokens / bearer / api keys / secrets
- secret-bearing URLs

## Inbound Security

`Telegram webhook` now persists inbound events in two layers:

- `secure_records` stores the raw webhook payload in encrypted form
- `inbound_messages` stores only operational metadata and sanitized text

Current behavior:

- raw inbound payload is encrypted with `AES-GCM`
- encryption key comes from `SREDA_ENCRYPTION_KEY`
- sanitized text is produced by `privacy_guard` before any future `LLM` routing
- operational storage no longer keeps the raw Telegram message in plain text

Relevant files:

- `src/sreda/services/encryption.py`
- `src/sreda/services/secure_storage.py`
- `src/sreda/services/inbound_messages.py`
- `src/sreda/api/routes/telegram_webhook.py`
- `src/sreda/db/models/core.py`
- `migrations/versions/20260325_0003_add_inbound_and_secure_storage.py`
