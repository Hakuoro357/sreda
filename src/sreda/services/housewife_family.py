"""Family-members CRUD for the housewife skill.

One row per household member, scoped by (tenant, user). Simple
aggregate: ``count_eaters`` returns how many people the LLM / shopping
scaler should cook for. Defaults to 1 (the user alone) when no
members are recorded, so recipes don't shrink to zero ingredients in
an empty-family edge case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.housewife import FAMILY_ROLES, FamilyMember

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class FamilyMemberInput:
    name: str
    role: str
    birth_year: int | None = None
    age_hint: str | None = None
    notes: str | None = None


class HousewifeFamilyService:
    """CRUD over ``family_members``. All methods tenant+user scoped."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_member(
        self,
        *,
        tenant_id: str,
        user_id: str,
        name: str,
        role: str,
        birth_year: int | None = None,
        age_hint: str | None = None,
        notes: str | None = None,
    ) -> FamilyMember:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("name required")
        if role not in FAMILY_ROLES:
            raise ValueError(f"unknown role: {role!r}")
        if birth_year is not None:
            if not 1900 <= int(birth_year) <= 2100:
                raise ValueError(f"implausible birth_year: {birth_year}")

        row = FamilyMember(
            id=f"fm_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            name=clean_name[:200],
            role=role,
            birth_year=int(birth_year) if birth_year is not None else None,
            age_hint=(age_hint or "").strip()[:64] or None,
            notes=(notes or "").strip()[:500] or None,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def update_member(
        self,
        *,
        tenant_id: str,
        user_id: str,
        member_id: str,
        name: str | None = None,
        role: str | None = None,
        birth_year: int | None = None,
        age_hint: str | None = None,
        notes: str | None = None,
    ) -> FamilyMember | None:
        """Update individual fields. Pass None to leave unchanged.
        Returns None if member not found / cross-tenant."""
        row = self._get_member(tenant_id, user_id, member_id)
        if row is None:
            return None

        if name is not None:
            clean = name.strip()
            if not clean:
                raise ValueError("name cannot be empty")
            row.name = clean[:200]
        if role is not None:
            if role not in FAMILY_ROLES:
                raise ValueError(f"unknown role: {role!r}")
            row.role = role
        if birth_year is not None:
            if not 1900 <= int(birth_year) <= 2100:
                raise ValueError(f"implausible birth_year: {birth_year}")
            row.birth_year = int(birth_year)
        if age_hint is not None:
            row.age_hint = age_hint.strip()[:64] or None
        if notes is not None:
            row.notes = notes.strip()[:500] or None
        row.updated_at = _utcnow()
        self.session.commit()
        return row

    def remove_member(
        self, *, tenant_id: str, user_id: str, member_id: str
    ) -> bool:
        row = self._get_member(tenant_id, user_id, member_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.commit()
        return True

    def add_members_batch(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: list[dict[str, Any]],
    ) -> list[FamilyMember]:
        """Batch version for bulk seeding from LLM ("папа, мама, двое
        детей"). Invalid rows skipped silently."""
        created: list[FamilyMember] = []
        for raw in members or []:
            if not isinstance(raw, dict):
                continue
            try:
                row = self.add_member(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    name=str(raw.get("name", "")),
                    role=str(raw.get("role", "")),
                    birth_year=raw.get("birth_year"),
                    age_hint=raw.get("age_hint"),
                    notes=raw.get("notes"),
                )
                created.append(row)
            except (ValueError, TypeError):
                continue
        return created

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_members(
        self, *, tenant_id: str, user_id: str
    ) -> list[FamilyMember]:
        return (
            self.session.query(FamilyMember)
            .filter(
                FamilyMember.tenant_id == tenant_id,
                FamilyMember.user_id == user_id,
            )
            .order_by(FamilyMember.role, FamilyMember.created_at)
            .all()
        )

    def count_eaters(self, *, tenant_id: str, user_id: str) -> int:
        """How many mouths to feed. Every member counts, children included
        (simpler than modelling partial portions). Fallback to 1 when
        no members recorded — a solo user still gets non-zero scaling."""
        n = self.session.query(FamilyMember).filter(
            FamilyMember.tenant_id == tenant_id,
            FamilyMember.user_id == user_id,
        ).count()
        return max(1, n)

    def get_member(
        self, *, tenant_id: str, user_id: str, member_id: str
    ) -> FamilyMember | None:
        return self._get_member(tenant_id, user_id, member_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_member(
        self, tenant_id: str, user_id: str, member_id: str
    ) -> FamilyMember | None:
        return (
            self.session.query(FamilyMember)
            .filter(
                FamilyMember.id == member_id,
                FamilyMember.tenant_id == tenant_id,
                FamilyMember.user_id == user_id,
            )
            .one_or_none()
        )
