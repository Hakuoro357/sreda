"""CRUD for ``TenantUserProfile`` + ``TenantUserSkillConfig``.

Thin layer over SQLAlchemy: validates enum-like fields (``source``,
``communication_style``, ``notification_priority``) at write-time so we
don't silently accept garbage, and owns the JSON encoding/decoding of
``quiet_hours`` / ``interest_tags`` / ``skill_params`` so callers work
with plain Python values.

Callers own the transaction — this repo only ``flush()``-es.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.user_profile import (
    TenantUserProfile,
    TenantUserProfileProposal,
    TenantUserSkillConfig,
)


NOTIFICATION_PRIORITIES = frozenset({"urgent", "normal", "low", "mute"})
COMMUNICATION_STYLES = frozenset({"terse", "casual", "formal"})
UPDATE_SOURCES = frozenset(
    {"user_command", "agent_tool_direct", "agent_tool_confirmed", "system"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


class UserProfileRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- profile

    def get_profile(self, tenant_id: str, user_id: str) -> TenantUserProfile | None:
        return (
            self.session.query(TenantUserProfile)
            .filter_by(tenant_id=tenant_id, user_id=user_id)
            .one_or_none()
        )

    def get_or_create_profile(
        self, tenant_id: str, user_id: str
    ) -> TenantUserProfile:
        existing = self.get_profile(tenant_id, user_id)
        if existing is not None:
            return existing
        now = _utcnow()
        profile = TenantUserProfile(
            id=_id("tup"),
            tenant_id=tenant_id,
            user_id=user_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(profile)
        self.session.flush()
        return profile

    def update_profile(
        self,
        tenant_id: str,
        user_id: str,
        *,
        source: str = "user_command",
        actor_user_id: str | None = None,
        display_name: str | None = None,
        tz: str | None = None,
        quiet_hours: list[dict[str, Any]] | None = None,
        communication_style: str | None = None,
        interest_tags: list[str] | None = None,
    ) -> TenantUserProfile:
        if source not in UPDATE_SOURCES:
            raise ValueError(f"unknown source: {source!r}")
        if (
            communication_style is not None
            and communication_style not in COMMUNICATION_STYLES
        ):
            raise ValueError(
                f"unknown communication_style: {communication_style!r}"
            )
        if quiet_hours is not None:
            _validate_quiet_hours(quiet_hours)

        profile = self.get_or_create_profile(tenant_id, user_id)
        if display_name is not None:
            profile.display_name = display_name
        if tz is not None:
            profile.timezone = tz
        if quiet_hours is not None:
            profile.quiet_hours_json = json.dumps(quiet_hours, ensure_ascii=False)
        if communication_style is not None:
            profile.communication_style = communication_style
        if interest_tags is not None:
            profile.interest_tags_json = json.dumps(interest_tags, ensure_ascii=False)
        profile.updated_by_source = source
        profile.updated_by_user_id = actor_user_id
        profile.updated_at = _utcnow()
        self.session.flush()
        return profile

    @staticmethod
    def decode_quiet_hours(profile: TenantUserProfile) -> list[dict[str, Any]]:
        try:
            value = json.loads(profile.quiet_hours_json or "[]")
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def decode_interest_tags(profile: TenantUserProfile) -> list[str]:
        try:
            value = json.loads(profile.interest_tags_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(value, list):
            return []
        return [str(tag) for tag in value if isinstance(tag, str)]

    # ---------------------------------------------------------- skill configs

    def list_skill_configs(
        self, tenant_id: str, user_id: str
    ) -> list[TenantUserSkillConfig]:
        return (
            self.session.query(TenantUserSkillConfig)
            .filter_by(tenant_id=tenant_id, user_id=user_id)
            .order_by(TenantUserSkillConfig.feature_key.asc())
            .all()
        )

    def get_skill_config(
        self, tenant_id: str, user_id: str, feature_key: str
    ) -> TenantUserSkillConfig | None:
        return (
            self.session.query(TenantUserSkillConfig)
            .filter_by(tenant_id=tenant_id, user_id=user_id, feature_key=feature_key)
            .one_or_none()
        )

    def upsert_skill_config(
        self,
        tenant_id: str,
        user_id: str,
        feature_key: str,
        *,
        source: str = "user_command",
        actor_user_id: str | None = None,
        notification_priority: str | None = None,
        token_budget_daily: int | None = None,
        skill_params: dict[str, Any] | None = None,
    ) -> TenantUserSkillConfig:
        if source not in UPDATE_SOURCES:
            raise ValueError(f"unknown source: {source!r}")
        if (
            notification_priority is not None
            and notification_priority not in NOTIFICATION_PRIORITIES
        ):
            raise ValueError(
                f"unknown notification_priority: {notification_priority!r}"
            )
        row = self.get_skill_config(tenant_id, user_id, feature_key)
        now = _utcnow()
        if row is None:
            row = TenantUserSkillConfig(
                id=_id("tusc"),
                tenant_id=tenant_id,
                user_id=user_id,
                feature_key=feature_key,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
        if notification_priority is not None:
            row.notification_priority = notification_priority
        if token_budget_daily is not None:
            row.token_budget_daily = max(0, int(token_budget_daily))
        if skill_params is not None:
            row.skill_params_json = json.dumps(
                skill_params, ensure_ascii=False, sort_keys=True
            )
        row.updated_by_source = source
        row.updated_by_user_id = actor_user_id
        row.updated_at = now
        self.session.flush()
        return row

    # ------------------------------------------------------------ proposals

    DEFAULT_PROPOSAL_TTL = timedelta(hours=1)

    def create_proposal(
        self,
        tenant_id: str,
        user_id: str,
        *,
        field_name: str,
        proposed_value: Any,
        justification: str | None = None,
        ttl: timedelta | None = None,
    ) -> TenantUserProfileProposal:
        now = _utcnow()
        row = TenantUserProfileProposal(
            id=_id("tupp"),
            tenant_id=tenant_id,
            user_id=user_id,
            field_name=field_name,
            proposed_value_json=json.dumps(proposed_value, ensure_ascii=False),
            justification=justification,
            status="pending",
            created_at=now,
            expires_at=now + (ttl or self.DEFAULT_PROPOSAL_TTL),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_proposal(self, proposal_id: str) -> TenantUserProfileProposal | None:
        return self.session.get(TenantUserProfileProposal, proposal_id)

    def mark_proposal_status(
        self,
        proposal_id: str,
        *,
        status: str,
    ) -> TenantUserProfileProposal | None:
        """Move a proposal to a terminal state. Returns the updated row,
        or ``None`` if no such proposal exists. Idempotent: calling with
        the same status twice is a no-op."""
        if status not in {"confirmed", "rejected", "expired"}:
            raise ValueError(f"cannot set proposal status to {status!r}")
        row = self.session.get(TenantUserProfileProposal, proposal_id)
        if row is None:
            return None
        if row.status != "pending":
            return row
        row.status = status
        row.completed_at = _utcnow()
        self.session.flush()
        return row

    @staticmethod
    def decode_proposed_value(proposal: TenantUserProfileProposal) -> Any:
        try:
            return json.loads(proposal.proposed_value_json)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def is_proposal_expired(proposal: TenantUserProfileProposal, now: datetime) -> bool:
        expires = proposal.expires_at
        if expires.tzinfo is None:
            # SQLite drops tzinfo on roundtrip; treat naive as UTC.
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= now

    @staticmethod
    def decode_skill_params(config: TenantUserSkillConfig) -> dict[str, Any]:
        try:
            value = json.loads(config.skill_params_json or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


def _validate_quiet_hours(value: list[dict[str, Any]]) -> None:
    if not isinstance(value, list):
        raise ValueError("quiet_hours must be a list")
    for i, window in enumerate(value):
        if not isinstance(window, dict):
            raise ValueError(f"quiet_hours[{i}] must be a dict")
        fh = window.get("from_hour")
        th = window.get("to_hour")
        weekdays = window.get("weekdays", list(range(7)))
        if not isinstance(fh, int) or not 0 <= fh <= 23:
            raise ValueError(f"quiet_hours[{i}].from_hour must be int 0..23")
        if not isinstance(th, int) or not 0 <= th <= 23:
            raise ValueError(f"quiet_hours[{i}].to_hour must be int 0..23")
        if not isinstance(weekdays, list) or not all(
            isinstance(d, int) and 0 <= d <= 6 for d in weekdays
        ):
            raise ValueError(f"quiet_hours[{i}].weekdays must be list of 0..6")
