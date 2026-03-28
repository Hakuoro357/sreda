from datetime import UTC, datetime

from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    EDSAccount,
    EDSChangeEvent,
    EDSClaimState,
    Tenant,
    TenantFeature,
    User,
    Workspace,
)
from sreda.db.session import get_engine, get_session_factory
from sreda.config.settings import get_settings
from sreda.services.claim_lookup import ClaimLookupService


def test_claim_lookup_service_returns_latest_known_claim(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "claim_lookup.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.add(TenantFeature(id="feature_1", tenant_id="tenant_1", feature_key="eds_monitor", enabled=True))
        session.add(
            EDSAccount(
                id="eds_acc_1",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                tenant_eds_account_id=None,
                site_key="mosreg",
                account_key="eds-1",
                label="EDS кабинет 1",
                login="5047136341",
            )
        )
        session.add(
            EDSClaimState(
                id="state_1",
                eds_account_id="eds_acc_1",
                claim_id="6230173",
                fingerprint_hash="hash_1",
                status="WORK",
                status_name="В работе",
                last_seen_changed="2026-03-28T15:10:00+00:00",
                last_history_order=12,
                last_history_code="HISTORY_SOLVED",
                last_history_date="2026-03-28T15:09:00+00:00",
                updated_at=datetime(2026, 3, 28, 15, 10, tzinfo=UTC),
            )
        )
        session.add(
            EDSChangeEvent(
                id="evt_1",
                eds_account_id="eds_acc_1",
                claim_id="6230173",
                change_type="client_updated",
                has_new_response=True,
                requires_user_action=False,
                created_at=datetime(2026, 3, 28, 15, 11, tzinfo=UTC),
            )
        )
        session.commit()

        result = ClaimLookupService(session).lookup_local_claim("tenant_1", "6230173")
    finally:
        session.close()

    assert result is not None
    assert result.claim_id == "6230173"
    assert result.status_name == "В работе"
    assert result.account_label == "EDS кабинет 1"
    assert result.last_history_code == "HISTORY_SOLVED"
    assert result.latest_change_type == "client_updated"
