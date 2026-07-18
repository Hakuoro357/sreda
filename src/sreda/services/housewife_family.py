"""Family-members CRUD for the housewife skill.

One row per household member, scoped by (tenant, user). Simple
aggregate: ``count_eaters`` returns how many people the LLM / shopping
scaler should cook for. Defaults to 1 (the user alone) when no
members are recorded, so recipes don't shrink to zero ingredients in
an empty-family edge case.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.housewife import FAMILY_ROLES, FamilyMember

_NAME_WS_RE = re.compile(r"\s+")


def _normalise_name(name: str) -> str:
    """Canonical form for family-member dedup. Lowercased, stripped,
    internal whitespace collapsed. 'Катя', 'КАТЯ' and '  катя  '
    collapse to the same key so the LLM can't accidentally create
    duplicates by capitalising differently across turns."""
    return _NAME_WS_RE.sub(" ", (name or "").strip().lower())

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


@dataclass(slots=True)
class AddFamilyMembersResult:
    """#115 by-name outcome for ``add_members_batch_detailed`` so the live voice
    can confirm who was added / already existed / repeated / was invalid."""

    created: list["FamilyMember"] = field(default_factory=list)
    duplicates_existing: list[str] = field(default_factory=list)  # names
    duplicates_in_batch: list[str] = field(default_factory=list)  # names
    invalid: list[str] = field(default_factory=list)  # nameable validation drops


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
        _existing_snapshot: list[tuple[FamilyMember, str]] | None = None,
    ) -> FamilyMember:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("name required")
        if role not in FAMILY_ROLES:
            raise ValueError(f"unknown role: {role!r}")
        if birth_year is not None:
            if not 1900 <= int(birth_year) <= 2100:
                raise ValueError(f"implausible birth_year: {birth_year}")

        # Dedup by normalised name — LLM may call add_family_members
        # across multiple turns ("onboarding added the family", "menu
        # planning re-added them"). Without this we get 9 rows for
        # 5 people. Case / whitespace-insensitive; we DON'T dedup on
        # role, because the same name with a different role still
        # should be considered a duplicate (the LLM just guessed the
        # role differently the second time).
        key = _normalise_name(clean_name)
        # audit 2026-07-18 MINOR (N+1): batch передаёт snapshot — пары
        # (member, normalised_name). Без него каждый add_member батча дёргал
        # полный SELECT; с ORM-объектами вместо пар — refresh-SELECT на каждый
        # expired атрибут после commit'ов (expire_on_commit).
        existing_snapshot = (
            _existing_snapshot
            if _existing_snapshot is not None
            else [
                (m, _normalise_name(m.name))
                for m in self.list_members(tenant_id=tenant_id, user_id=user_id)
            ]
        )
        for existing, existing_key in existing_snapshot:
            if existing_key == key:
                # #202 (Codex medium R1 MAJOR): НЕ логируем имя (ПД) — на ctx-пути (ReAct) это нарушает
                # PII-правило проекта. Достаточно tenant/user/id существующей строки.
                logger.info(
                    "add_member: dedup hit tenant=%s user=%s → existing id=%s",
                    tenant_id, user_id, existing.id,
                )
                return existing

        # #202 household: ctx-путь (ReAct) оснащает op_id+hash (эталон recipes/checklists). pre-check по
        # op_id ловит повтор tool_call И морфовариант той же леммы в батче (_normalise_name только
        # whitespace/регистр — «Маша»/«Маши» им НЕ схлопываются, а op_id по лемме совпал бы → коллизия;
        # add_members_batch коммитит каждого → следующий видит). hash — кросс-ходовой ключ-лемма. Легаси
        # (ctx None) → ключи None, байт-в-байт. fail-closed user_id + tenant-guard.
        from sreda.runtime.planner.tool_runtime import current_tool_runtime

        op_id_value: str | None = None
        nhash_value: str | None = None
        ctx = current_tool_runtime()
        if ctx is not None:
            if not user_id:
                raise ValueError("add_member ctx path: user_id обязателен (fail-closed).")
            if ctx.tenant_id and ctx.tenant_id != tenant_id:
                raise ValueError(
                    f"add_member ctx path: ctx.tenant_id={ctx.tenant_id!r} != {tenant_id!r} — leak.")
            from sreda.services.operation_id import (
                compute_normalized_title_hash,
                compute_operation_id_create,
            )
            from sreda.services.text_normalization import normalize_for_dedup

            op_id_value = compute_operation_id_create(
                plan_id=ctx.execution_id, step_id=ctx.step_id, action="create",
                entity_type="family_member", logical_key=normalize_for_dedup(clean_name))
            nhash_value = compute_normalized_title_hash(
                clean_name, entity_type="family_member", tenant_id=tenant_id,
                user_id=user_id or "") or None
            existing_op = (
                self.session.query(FamilyMember)
                .filter_by(tenant_id=tenant_id, user_id=user_id, operation_id=op_id_value)
                .one_or_none()
            )
            if existing_op is not None:
                return existing_op
            if nhash_value is not None:
                existing_hash = (
                    self.session.query(FamilyMember)
                    .filter_by(tenant_id=tenant_id, user_id=user_id,
                               normalized_title_hash=nhash_value)
                    .first()
                )
                if existing_hash is not None:
                    return existing_hash

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
            operation_id=op_id_value,  # #202: key-policy (ctx) / None (легаси)
            normalized_title_hash=nhash_value,
        )
        self.session.add(row)
        try:
            self.session.flush()
            # audit 2026-07-18 MINOR (N+1): detach ДО commit — ТОЛЬКО в batch-пути
            # (snapshot передан): иначе expire_on_commit обнуляет атрибуты
            # возвращаемой строки, и batch-caller дёргает refresh-SELECT на
            # каждый row.id. INSERT уже во flush'е; чтение полей с
            # detached-объекта с загруженными атрибутами бесплатно.
            # Одиночный вызов (snapshot=None) сохраняет session-bound контракт:
            # возвращённая строка refresh'абельна — контракт #202 (эталон
            # recipes/checklists: add_* возвращают persistent-объект).
            if _existing_snapshot is not None:
                self.session.expunge(row)
            self.session.commit()
        except IntegrityError:
            # Гонка по operation_id — на сингл-тенанте ReAct не ожидается, fail-safe: откат + replay.
            self.session.rollback()
            if op_id_value is not None:
                racer = (
                    self.session.query(FamilyMember)
                    .filter_by(tenant_id=tenant_id, user_id=user_id, operation_id=op_id_value)
                    .one_or_none()
                )
                if racer is not None:
                    return racer
            raise
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
        clear_birth_year: bool = False,
        age_hint: str | None = None,
        notes: str | None = None,
    ) -> FamilyMember | None:
        """Update individual fields. Pass None to leave unchanged.
        Returns None if member not found / cross-tenant.

        Codex Sub-A4 household R3 MAJOR: ``clear_birth_year=True``
        explicitly nulls the column (replaces destructive remove +
        re-add workaround). Mutually exclusive with ``birth_year=N``
        — passing both raises ``ValueError``."""
        if birth_year is not None and clear_birth_year:
            raise ValueError(
                "birth_year and clear_birth_year are mutually exclusive "
                "— set one or neither, not both."
            )
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
        elif clear_birth_year:
            row.birth_year = None
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
        детей"). Invalid rows skipped silently. Returns the list of
        NEWLY CREATED members — entries that short-circuited on a
        dedup hit are NOT returned, so the LLM can tell from the
        ``result`` length how many actually landed in the book.

        Dedup in two passes:
          1. Within-batch: if LLM sent "Катя" twice in one list, only
             the first counts.
          2. Against DB: if a member with the same normalised name
             already exists, skip (previous turn already added them).
        """
        # Back-compat: existing callers/tests expect the list of created rows.
        return self.add_members_batch_detailed(
            tenant_id=tenant_id, user_id=user_id, members=members
        ).created

    def add_members_batch_detailed(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: list[dict[str, Any]],
    ) -> AddFamilyMembersResult:
        """#115: same logic as ``add_members_batch`` but returns the full by-name
        outcome (created / duplicates_existing / duplicates_in_batch / invalid) so
        the live voice can confirm who landed vs was already there vs repeated vs
        dropped. Untitled / non-dict rows have no name and are not reported."""
        result = AddFamilyMembersResult()
        seen_in_batch: set[str] = set()
        existing_members = self.list_members(tenant_id=tenant_id, user_id=user_id)
        existing_keys = {_normalise_name(m.name) for m in existing_members}
        # audit 2026-07-18 MINOR (N+1): snapshot пар (member, normalised_name) —
        # add_member не перечитывает БД и не трогает expired-атрибуты ORM.
        existing_snapshot = [(m, _normalise_name(m.name)) for m in existing_members]
        # #202: id уже-существующих + созданных В ЭТОМ батче — для by-name классификации МОРФО-дубля
        # (одна лемма, мимо whitespace-_normalise_name): add_member на ctx-пути вернёт РАНЕЕ созданную
        # строку (op_id/hash pre-check схлопнул) → НЕ считать «созданным» дважды (контракт #115).
        existing_ids = {m.id for m in existing_members}
        created_ids: set[str] = set()
        for raw in members or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            key = _normalise_name(name)
            if key in seen_in_batch:
                result.duplicates_in_batch.append(name)  # #115
                continue
            seen_in_batch.add(key)
            if key in existing_keys:
                result.duplicates_existing.append(name)  # #115 (already in DB)
                continue
            try:
                row = self.add_member(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    name=name,
                    role=str(raw.get("role", "")),
                    birth_year=raw.get("birth_year"),
                    age_hint=raw.get("age_hint"),
                    notes=raw.get("notes"),
                    _existing_snapshot=existing_snapshot,
                )
            except (ValueError, TypeError):
                result.invalid.append(name)  # #115 nameable validation drop
                continue
            existing_keys.add(key)
            # #202: классифицируем по id результата add_member (морфо-дубль мимо whitespace-дедупа).
            if row.id in existing_ids:
                result.duplicates_existing.append(name)  # морфо-дубль уже-существующего члена
            elif row.id in created_ids:
                result.duplicates_in_batch.append(name)  # морфо-дубль в этом же батче
            else:
                created_ids.add(row.id)
                existing_snapshot.append((row, key))  # snapshot актуален для след. элементов
                result.created.append(row)
        return result

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
