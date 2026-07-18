"""Read-only export of a MAX dialogue archive from prod DB.

Runs on VDS via run_probe.sh (which loads /etc/sreda/.env first).
Writes to /tmp/replay-sample.json — scp'd back to local.

Selects the last ~30 conversation.chat agent_runs for the tenant from
env ``SREDA_REPLAY_TENANT`` (реальный id не коммитим — audit 2026-07-18), plus:
  - user_message (decrypted from input_json)
  - per-run history substitute (prior completed runs, same thread,
    user_text + bot_text pairs — mirrors _load_chat_history logic)
  - old bot reply (decrypted from outbox_messages via result_json)
  - profile fields from tenant_user_profiles

READ-ONLY: only SELECT + decrypt_value. NO writes / DDL / restart.
"""

import json
import os
import sys

sys.path.insert(0, "/opt/sreda/src")

from sqlalchemy import text  # noqa: E402

from sreda.db.session import get_session_factory  # noqa: E402
from sreda.services.encryption import decrypt_value  # noqa: E402

TENANT = os.environ.get("SREDA_REPLAY_TENANT", "")
if not TENANT:
    raise SystemExit(
        "Set SREDA_REPLAY_TENANT=<tenant_id> (real ids are not committed — "
        "audit 2026-07-18)."
    )
LIMIT = 30
OUTPUT = "/tmp/replay-sample.json"


def _safe_decrypt(val: str | None) -> str:
    if not val:
        return ""
    if isinstance(val, str) and val.startswith("v2:"):
        try:
            return decrypt_value(val)
        except Exception as exc:
            return f"<dec-err:{type(exc).__name__}>"
    return val


def _payload_text(raw) -> str:
    """Extract reply text from an outbox_messages.payload_json value.

    payload_json is a ``v2:``-encrypted STRING whose plaintext is a JSON
    object ``{"text": ...}``. So: decrypt the WHOLE string FIRST, then parse
    JSON, then read ``text``. (The earlier bug parsed the ciphertext as JSON
    → always {} → empty reply.)"""
    if not raw:
        return ""
    if isinstance(raw, str) and raw.startswith("v2:"):
        try:
            obj = json.loads(decrypt_value(raw))
        except Exception:
            return ""
        if not isinstance(obj, dict):
            return ""
    else:
        obj = _as_dict(raw)
    return (obj.get("text") or "").strip()


def _as_dict(val) -> dict:
    """Coerce a JSONB column value (already a dict from psycopg) or a JSON
    string to a plain dict. Returns {} on None or parse failure."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            result = json.loads(val)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    return {}


def _load_prior_history(
    session,
    thread_id: str,
    before_run_id: str,
    before_created_at: str,
) -> list[dict]:
    """Mirror of handlers._load_chat_history — prior completed runs for the
    same thread, up to 5 turns, oldest first.

    Fix (dual-Codex issue #87): original query excluded only ``id != :rid``,
    which allowed early turns to see LATER replies as "history" when the
    same thread had multiple runs. Added ``AND created_at < :created_at``
    so history is strictly prior to the current run's timestamp.
    """
    rows = session.execute(
        text("""
            SELECT id, input_json, result_json
            FROM agent_runs
            WHERE tenant_id = :t
              AND thread_id = :tid
              AND action_type = 'conversation.chat'
              AND status = 'completed'
              AND id != :rid
              AND created_at < :created_at
            ORDER BY created_at DESC
            LIMIT 5
        """),
        {"t": TENANT, "tid": thread_id, "rid": before_run_id, "created_at": before_created_at},
    ).all()

    turns = []
    for row in reversed(rows):  # oldest first for chronological feed
        run_id, input_json_raw, result_json_raw = row
        try:
            input_data = _as_dict(input_json_raw)
            user_text = (
                (input_data.get("params") or {}).get("text") or ""
            ).strip()
            if not user_text:
                continue
            result_data = _as_dict(result_json_raw)
            outbox_ids = result_data.get("outbox_message_ids") or []
            bot_parts = []
            for oid in outbox_ids:
                ob = session.execute(
                    text("SELECT payload_json FROM outbox_messages WHERE id = :id"),
                    {"id": oid},
                ).fetchone()
                if ob is None or not ob[0]:
                    continue
                t = _payload_text(ob[0])
                if t:
                    bot_parts.append(t)
            bot_text = "\n".join(bot_parts)
            if not bot_text:
                continue
            turns.append({"user": user_text, "bot": bot_text})
        except Exception as exc:
            # Skip malformed rows — partial history beats blocking
            print(f"  [warn] history run {run_id}: {exc}", file=sys.stderr)
    return turns


def main() -> None:
    sf = get_session_factory()
    results = []

    with sf() as session:
        # Fetch profile
        profile_row = session.execute(
            text("""
                SELECT display_name, address_form, timezone, communication_style
                FROM tenant_user_profiles
                WHERE tenant_id = :t
                LIMIT 1
            """),
            {"t": TENANT},
        ).fetchone()

        profile = {}
        if profile_row:
            display_name_raw, address_form, timezone, comm_style = profile_row
            profile = {
                "name": _safe_decrypt(display_name_raw),
                "address": _safe_decrypt(address_form) if address_form else "",
                "timezone": timezone or "Europe/Moscow",
                "language": "ru",  # column absent on prod; default to Russian
                "communication_style": comm_style or "",
            }

        print(f"Profile: {profile.get('name', '?')}, tz={profile.get('timezone')}")

        # Fetch last N conversation.chat runs
        runs = session.execute(
            text("""
                SELECT id, thread_id, created_at, input_json, result_json
                FROM agent_runs
                WHERE tenant_id = :t
                  AND action_type = 'conversation.chat'
                  AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT :lim
            """),
            {"t": TENANT, "lim": LIMIT},
        ).all()

        print(f"Fetched {len(runs)} runs", file=sys.stderr)

        for row in reversed(runs):  # chronological order
            run_id, thread_id, created_at, input_json_raw, result_json_raw = row
            try:
                input_data = _as_dict(input_json_raw)
                params = input_data.get("params") or {}
                user_text = (params.get("text") or "").strip()
                source_type = input_data.get("source_type", "")
                is_voice = source_type == "telegram_voice"

                if not user_text:
                    print(f"  [skip] run {run_id}: empty user_text", file=sys.stderr)
                    continue

                # Old bot reply
                result_data = _as_dict(result_json_raw)
                outbox_ids = result_data.get("outbox_message_ids") or []
                old_reply_parts = []
                for oid in outbox_ids:
                    ob = session.execute(
                        text("SELECT payload_json FROM outbox_messages WHERE id = :id"),
                        {"id": oid},
                    ).fetchone()
                    if ob is None or not ob[0]:
                        continue
                    t = _payload_text(ob[0])
                    if t:
                        old_reply_parts.append(t)
                old_reply = "\n".join(old_reply_parts)

                # History substitute — pass created_at to prevent future-history leak
                history = _load_prior_history(session, thread_id, run_id, str(created_at))

                results.append({
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "created_at": str(created_at),
                    "user_message": user_text,
                    "is_voice": is_voice,
                    "old_reply": old_reply,
                    "history": history,
                    "profile": profile,
                })
                print(
                    f"  [ok] run {run_id} ts={created_at} "
                    f"msg={user_text[:50]!r}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"  [err] run {run_id}: {exc}", file=sys.stderr)

    print(f"\nExported {len(results)} turns → {OUTPUT}", file=sys.stderr)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(results)} turns written to {OUTPUT}")


if __name__ == "__main__":
    main()
