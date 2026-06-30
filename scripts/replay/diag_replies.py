"""READ-ONLY diagnostic: where do MAX bot replies live for tenant_max_40921122?

Pinpoints why old_reply/history came back empty:
- dumps result_json (raw keys + value) for 2 recent completed runs
- checks whether outbox_messages has ANY rows for the tenant + their payload shape

READ-ONLY: only SELECT. No writes.
"""

import json
import sys

sys.path.insert(0, "/opt/sreda/src")

from sqlalchemy import text  # noqa: E402

from sreda.db.session import get_session_factory  # noqa: E402

TENANT = "tenant_max_40921122"


def main() -> None:
    sf = get_session_factory()
    with sf() as session:
        print("=== 2 recent completed conversation.chat runs: result_json ===")
        runs = session.execute(
            text("""
                SELECT id, created_at, result_json
                FROM agent_runs
                WHERE tenant_id = :t AND action_type = 'conversation.chat'
                  AND status = 'completed'
                ORDER BY created_at DESC LIMIT 2
            """),
            {"t": TENANT},
        ).all()
        for rid, ts, rj in runs:
            print(f"\nrun {rid} ts={ts}")
            if isinstance(rj, dict):
                print(f"  result_json type=dict keys={list(rj.keys())}")
                print(f"  value={json.dumps(rj, ensure_ascii=False)[:400]}")
            else:
                print(f"  result_json type={type(rj).__name__} raw={str(rj)[:400]}")

        print("\n=== outbox_messages for this tenant (count + 2 samples) ===")
        cnt = session.execute(
            text("SELECT count(*) FROM outbox_messages WHERE tenant_id = :t"),
            {"t": TENANT},
        ).scalar()
        print(f"outbox_messages count for tenant: {cnt}")
        obs = session.execute(
            text("""
                SELECT id, channel_type, status, created_at, payload_json
                FROM outbox_messages WHERE tenant_id = :t
                ORDER BY created_at DESC LIMIT 2
            """),
            {"t": TENANT},
        ).all()
        for oid, ch, st, ts, pj in obs:
            print(f"\noutbox {oid} ch={ch} st={st} ts={ts}")
            if isinstance(pj, dict):
                print(f"  payload keys={list(pj.keys())}")
                tval = pj.get("text")
                print(f"  text prefix={str(tval)[:60]!r}")
            else:
                print(f"  payload type={type(pj).__name__} raw={str(pj)[:200]}")

        # Column existence check for agent_runs (does it carry a reply column?)
        print("\n=== agent_runs columns ===")
        cols = session.execute(
            text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='agent_runs' ORDER BY ordinal_position
            """)
        ).all()
        print([c[0] for c in cols])


if __name__ == "__main__":
    main()
