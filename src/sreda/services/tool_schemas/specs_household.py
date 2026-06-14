"""ToolSpec instances for the HOUSEHOLD family (Sub-A4 phase 5).

4 tools migrated: ``add_family_members``, ``list_family_members``,
``update_family_member``, ``remove_family_member``.

Sources of truth:
- Tool signatures: ``services/housewife_chat_tools.py:1554`` (add batch),
  ``:1606`` (list), ``:1643`` (update), ``:1685`` (remove).
- Output schemas: ``services/tool_schemas/housewife.py`` — 4 outputs,
  all ``HousewifeToolError``-aware.
- ID factory: ``housewife_family.py:96`` — ``fm_<24 hex>``.
- Roles: ``db/models/housewife.py:152`` —
  ``FAMILY_ROLES = ("self", "spouse", "child", "parent", "other")``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import FamilyMemberId
from sreda.services.tool_schemas.housewife import (
    AddFamilyMembersOutput,
    ListFamilyMembersOutput,
    RemoveFamilyMemberOutput,
    UpdateFamilyMemberOutput,
)


# ---------------------------------------------------------------------------
# Household-specific aliases
# ---------------------------------------------------------------------------


FamilyRole = Literal["self", "spouse", "child", "parent", "other"]
"""Five canonical roles — single source of truth at
``db/models/housewife.py:152``. Runtime ``add_members_batch``
(housewife_family.py:160) drops members with unknown roles silently
— planner-side validation rejects upfront so the LLM gets a clear
ValidationError instead of a hidden batch shrink."""


FamilyMemberName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Cyrillic / Latin names. Codex Sub-A4 household R2 MAJOR (new):
cap widened from 80 → 200 to match runtime truncation
``clean_name[:200]`` at housewife_family.py:99. Mini App / legacy
rows may have longer names than the original 80-char limit, which
would block list_family_members for those tenants. Runtime dedups
by case-insensitive normalised form (housewife_family.py:26-31)."""


AgeHint = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
"""Free-form age hint when birth_year unknown («8 лет», «школьник»,
«пенсионер»). Codex Sub-A4 household R2 MAJOR (new): cap widened
from 40 → 64 to match runtime ``[:64]`` at housewife_family.py:102
AND the DB column ``String(64)`` at db/models/housewife.py:190.
Add-batch context: empty string is rejected (must have content).
Update context uses ``AgeHintClearable`` below to support clearing."""


AgeHintClearable = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=64),
]
"""Codex Sub-A4 household R1 MAJOR #4 + R2 MAJOR (new): update path
clearable variant — empty string == clear. ``min_length`` absent
(vs ``AgeHint``). Cap matches runtime/DB at 64."""


FamilyNotes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Free-form per-member notes — allergies, dietary preferences,
chronic conditions. Codex Sub-A4 household R2 MAJOR (new): cap
widened from 300 → 500 to match runtime ``notes[:500]`` at
housewife_family.py:103. (DB column is EncryptedString with no
fixed length, but service truncates at 500.)"""


FamilyNotesClearable = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=500),
]
"""Codex Sub-A4 household R1 MAJOR #4 + R2 MAJOR (new): update path
clearable variant. Empty string == clear (e.g. «убери аллергию у
Никиты»). Cap matches runtime at 500."""


# Runtime accepts birth_year roughly in [1900, current_year+1]; the
# housewife_chat_tools.py docstring says "implausible birth_year
# skipped silently". Schema rejects upfront so the planner sees
# ValidationError instead of a hidden batch shrink.
BirthYear = Annotated[int, Field(ge=1900, le=2100)]
"""Birth year — wide range covers four generations of household
members. ``2100`` upper bound matches general Sreda forward-compat."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class FamilyMemberDraft(BaseModel):
    """One member in the add-batch payload.

    Codex Sub-A4 household: ``birth_year`` XOR ``age_hint`` is NOT
    enforced — both can be supplied; runtime stores both and uses
    ``birth_year`` for age display when available (``housewife_chat_tools.py:1632``).
    Reject only when BOTH are absent? No — runtime accepts neither
    (returns role+name only). Schema mirrors runtime: birth_year
    and age_hint are independent optionals."""

    model_config = ConfigDict(extra="forbid")
    name: FamilyMemberName
    role: FamilyRole
    birth_year: BirthYear | None = None
    age_hint: AgeHint | None = None
    notes: FamilyNotes | None = None


class AddFamilyMembersInput(BaseModel):
    """Batch-add household members in one call. Runtime dedups by
    normalised name — duplicates count toward ``skipped_as_duplicate``
    in the output, NOT a validation error.

    Codex Sub-A4 household: ``members`` MUST be non-empty — runtime
    returns ``error: empty batch`` for empty list. Schema rejects
    upfront with the same semantics so the planner sees the issue
    before invoking the tool."""

    model_config = ConfigDict(extra="forbid")
    members: list[FamilyMemberDraft] = Field(min_length=1, max_length=20)


class ListFamilyMembersInput(BaseModel):
    """No arguments. Runtime returns all members for the current
    (tenant, user). Empty for first-time users."""

    model_config = ConfigDict(extra="forbid")


class UpdateFamilyMemberInput(BaseModel):
    """Point-update one member's fields. ``member_id`` required;
    every other field is optional — pass only what changes.

    Codex Sub-A4 household R1 MAJOR #4 + R2 MAJOR (new): clear
    semantics are now unambiguous:
    - ``age_hint=""`` or ``notes=""`` clears the respective text
      field (clearable variants below allow empty string).
    - ``clear_birth_year=True`` clears the birth_year column —
      explicit boolean instead of relying on ``birth_year=None``
      which already means «no change». This avoids the «remove +
      re-add» destructive workaround (would change member_id and
      orphan notes/role).
    - ``name`` and ``role`` cannot be cleared via update (cleared
      name violates dedup; cleared role has no sensible default).

    ``birth_year=N`` and ``clear_birth_year=True`` are mutually
    exclusive — set ONE or NEITHER; both raises.

    At least ONE updatable field/flag must be non-None/non-False.
    Empty update payload is a planner mistake (would be a no-op at
    runtime); reject so the planner notices.
    """

    model_config = ConfigDict(extra="forbid")
    member_id: FamilyMemberId
    name: FamilyMemberName | None = None
    role: FamilyRole | None = None
    birth_year: BirthYear | None = None
    clear_birth_year: bool = False
    age_hint: AgeHintClearable | None = None
    notes: FamilyNotesClearable | None = None

    @model_validator(mode="after")
    def _validate_birth_year_xor_clear(self) -> "UpdateFamilyMemberInput":
        if self.birth_year is not None and self.clear_birth_year:
            raise ValueError(
                "birth_year and clear_birth_year are mutually exclusive — "
                "set birth_year to update, OR clear_birth_year=True to clear, "
                "but not both. Pick one."
            )
        return self

    @model_validator(mode="after")
    def _validate_at_least_one_field(self) -> "UpdateFamilyMemberInput":
        all_unset = (
            all(
                getattr(self, f) is None
                for f in ("name", "role", "birth_year", "age_hint", "notes")
            )
            and not self.clear_birth_year
        )
        if all_unset:
            raise ValueError(
                "UpdateFamilyMemberInput requires at least one "
                "updatable field (name/role/birth_year/clear_birth_year/"
                "age_hint/notes). An empty update is a no-op at "
                "runtime — if the goal is to remove the member, use "
                "remove_family_member."
            )
        return self


class RemoveFamilyMemberInput(BaseModel):
    """Delete one member by id. Single field — keeps planner contract
    minimal (no soft-vs-hard delete distinction at this layer)."""

    model_config = ConfigDict(extra="forbid")
    member_id: FamilyMemberId


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


ADD_FAMILY_MEMBERS_SPEC = ToolSpec(
    name="add_family_members",
    # #128/#144: ужато — статус-форматы дублировались с output-строкой/mutex
    description=(
        "Добавить одного или нескольких членов семьи за раз («жена "
        "Катя, сын Никита 10 лет»). Возраст: birth_year ТОЛЬКО при "
        "явном годе; «10 лет»/«школьник» → age_hint (год не угадывать). "
        "Роли: spouse=муж/жена, child=сын/дочь, parent=мама/папа, "
        "self=сам юзер; прочее (сестра, бабушка) → role='other' + "
        "отношение в notes. Имена дедуп (case-insensitive). Не уверен, "
        "что записи новые — сначала list_family_members."
    ),
    family="household",
    effect="write",
    read_domains=[],
    write_domains=["household"],
    input_model=AddFamilyMembersInput,
    output_model=AddFamilyMembersOutput,
    trigger_examples=[
        "у меня жена Катя, сын Никита 10 лет",
        "запиши семью: муж и двое детей",
        "запомни что у меня сын-школьник",
        "у меня брат Серёжа",
    ],
    mutex_notes=[
        "ДЛЯ ДОБАВЛЕНИЯ. Правка → update_family_member; удаление → remove_family_member.",
        "Дубли по нормализованному имени — НЕ ошибка, идут в skipped_as_duplicate. План не ретраит.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


LIST_FAMILY_MEMBERS_SPEC = ToolSpec(
    name="list_family_members",
    description=(
        "Показать всех членов семьи: имя, роль, возраст, заметки; "
        "member_id каждого — для update/remove_family_member (id не "
        "выдумывай). Меню/покупки берут household сами — зови только "
        "когда юзер спрашивает про семью или нужны member_id. Статусы: "
        "ok и empty (никого — предложи add_family_members)."
    ),
    family="household",
    effect="read",
    read_domains=["household"],
    write_domains=[],
    input_model=ListFamilyMembersInput,
    output_model=ListFamilyMembersOutput,
    trigger_examples=[
        "кто у меня в семье",
        "покажи мою семью",
        "сколько человек я записал",
        "есть ли уже Маша в семье",
    ],
    mutex_notes=[
        "Возвращает СЕМЬЮ, не рецепты/покупки/задачи. Меню/покупки берут household сами — list тут не нужен.",
        "Структурированный вывод (members[].member_id) — ссылайся на эти id при update/remove, не парсь prose.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


UPDATE_FAMILY_MEMBER_SPEC = ToolSpec(
    name="update_family_member",
    # #128/#144: ужато; ролевые правила — ссылкой на add_family_members
    description=(
        "Обновить поля одного члена семьи: «Маше 9 уже», «у Никиты "
        "аллергия на молоко». Нет member_id — сначала list_family_members "
        "по name; несколько совпадений — переспроси. Только меняющиеся "
        "поля, минимум одно non-None. Правила role — как в "
        "add_family_members. Clear: notes='' и age_hint='' очищают; "
        "birth_year — только clear_birth_year=True (не с birth_year=N). "
        "name и role очистить нельзя."
    ),
    family="household",
    effect="write",
    read_domains=[],
    write_domains=["household"],
    input_model=UpdateFamilyMemberInput,
    output_model=UpdateFamilyMemberOutput,
    trigger_examples=[
        "Маше 9 уже",
        "у Никиты аллергия на молоко",
        "Катя теперь не ребёнок, это жена",
        "у мамы день рождения 1985",
    ],
    mutex_notes=[
        "ДЛЯ ПРАВКИ. Добавить нового → add_family_members; удаление → remove_family_member.",
        "member_id — из list_family_members.members[i].member_id (структурированный вывод, не prose).",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


REMOVE_FAMILY_MEMBER_SPEC = ToolSpec(
    name="remove_family_member",
    description=(
        "Удалить запись члена семьи. ТОЛЬКО когда юзер явно просит "
        "убрать («Маша переехала, удали»); коррекция — "
        "update_family_member. Нет member_id — сначала "
        "list_family_members; несколько совпадений — переспроси. "
        "Возвращает ok:removed или error:member_not_found."
    ),
    family="household",
    effect="write",
    read_domains=[],
    write_domains=["household"],
    input_model=RemoveFamilyMemberInput,
    output_model=RemoveFamilyMemberOutput,
    trigger_examples=[
        "удали Машу из семьи",
        "Никита больше не живёт с нами, убери",
        "удали запись о сестре",
        "вычеркни папу",
    ],
    mutex_notes=[
        "ТОЛЬКО для удаления. Коррекция полей — update_family_member.",
        "member_id — из list_family_members.members[i].member_id (структурированный вывод).",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


HOUSEHOLD_SPECS: list[ToolSpec] = [
    ADD_FAMILY_MEMBERS_SPEC,
    LIST_FAMILY_MEMBERS_SPEC,
    UPDATE_FAMILY_MEMBER_SPEC,
    REMOVE_FAMILY_MEMBER_SPEC,
]


__all__ = [
    "ADD_FAMILY_MEMBERS_SPEC",
    "AddFamilyMembersInput",
    "AgeHint",
    "AgeHintClearable",
    "BirthYear",
    "FamilyMemberDraft",
    "FamilyMemberName",
    "FamilyNotes",
    "FamilyNotesClearable",
    "FamilyRole",
    "HOUSEHOLD_SPECS",
    "LIST_FAMILY_MEMBERS_SPEC",
    "ListFamilyMembersInput",
    "REMOVE_FAMILY_MEMBER_SPEC",
    "RemoveFamilyMemberInput",
    "UPDATE_FAMILY_MEMBER_SPEC",
    "UpdateFamilyMemberInput",
]
