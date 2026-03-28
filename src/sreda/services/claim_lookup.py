from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from sreda.db.models import EDSAccount, EDSChangeEvent, EDSClaimState

CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ClaimLookupResult:
    claim_id: str
    status_name: str | None
    account_label: str
    account_login_masked: str
    last_seen_changed: str | None
    last_history_code: str | None
    last_history_date: str | None
    latest_change_type: str | None


class ClaimLookupService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lookup_local_claim(self, tenant_id: str, claim_id: str) -> ClaimLookupResult | None:
        row = (
            self.session.query(EDSClaimState, EDSAccount)
            .join(EDSAccount, EDSAccount.id == EDSClaimState.eds_account_id)
            .filter(
                EDSAccount.tenant_id == tenant_id,
                EDSClaimState.claim_id == claim_id,
            )
            .order_by(EDSClaimState.updated_at.desc(), EDSClaimState.id.desc())
            .first()
        )
        if row is None:
            return None

        claim_state, account = row
        latest_change = (
            self.session.query(EDSChangeEvent)
            .filter(
                EDSChangeEvent.eds_account_id == account.id,
                EDSChangeEvent.claim_id == claim_id,
            )
            .order_by(EDSChangeEvent.created_at.desc(), EDSChangeEvent.id.desc())
            .first()
        )
        return ClaimLookupResult(
            claim_id=claim_state.claim_id,
            status_name=claim_state.status_name,
            account_label=account.label,
            account_login_masked=_mask_login(account.login),
            last_seen_changed=claim_state.last_seen_changed,
            last_history_code=claim_state.last_history_code,
            last_history_date=claim_state.last_history_date,
            latest_change_type=latest_change.change_type if latest_change is not None else None,
        )

    def build_claim_reply(self, result: ClaimLookupResult) -> str:
        lines = [
            f"Заявка #{result.claim_id}",
            "",
            f"Статус: {result.status_name or 'Неизвестно'}",
            f"Источник: {result.account_label}",
            f"ЛК EDS: {result.account_login_masked}",
        ]
        if result.last_seen_changed:
            lines.append(f"Последнее изменение: {_format_timestamp(result.last_seen_changed)}")
        if result.last_history_code:
            lines.append(f"Код истории: {result.last_history_code}")
        if result.last_history_date:
            lines.append(f"Дата истории: {_format_timestamp(result.last_history_date)}")
        if result.latest_change_type:
            lines.append(f"Последнее событие: {result.latest_change_type}")
        return "\n".join(lines)


def is_valid_claim_id(value: str) -> bool:
    return bool(CLAIM_ID_PATTERN.fullmatch(value.strip()))


def _mask_login(login: str) -> str:
    normalized = login.strip()
    if len(normalized) <= 6:
        return normalized
    return f"{normalized[:4]}***{normalized[-3:]}"


def _format_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%d.%m.%Y %H:%M")
