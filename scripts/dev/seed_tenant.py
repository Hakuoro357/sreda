from __future__ import annotations

import argparse

from sreda.db.repositories.seed import SeedRepository
from sreda.db.session import get_session_factory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a minimal tenant bundle for local development.")
    parser.add_argument("--tenant-id", default="tenant_demo")
    parser.add_argument("--tenant-name", default="Demo tenant")
    parser.add_argument("--workspace-id", default="workspace_demo")
    parser.add_argument("--workspace-name", default="Demo workspace")
    parser.add_argument("--user-id", default="user_demo")
    parser.add_argument("--telegram-account-id", default="100000001")
    parser.add_argument("--assistant-id", default="assistant_demo")
    parser.add_argument("--assistant-name", default="Demo assistant")
    parser.add_argument("--enable-eds-monitor", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = get_session_factory()()
    try:
        repository = SeedRepository(session)
        repository.ensure_tenant_bundle(
            tenant_id=args.tenant_id,
            tenant_name=args.tenant_name,
            workspace_id=args.workspace_id,
            workspace_name=args.workspace_name,
            user_id=args.user_id,
            telegram_account_id=args.telegram_account_id,
            assistant_id=args.assistant_id,
            assistant_name=args.assistant_name,
            eds_monitor_enabled=args.enable_eds_monitor,
        )
    finally:
        session.close()

    print("SEED_OK")


if __name__ == "__main__":
    main()
