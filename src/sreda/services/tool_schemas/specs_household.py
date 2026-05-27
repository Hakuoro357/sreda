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
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]
"""Cyrillic / Latin names — generous 80 char cap for multi-part names
(«Анна-Мария Кузнецова»). Runtime dedups by case-insensitive
normalised form (housewife_family.py:26-31)."""


AgeHint = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
]
"""Free-form age hint when birth_year unknown («8 лет», «школьник»,
«пенсионер»). 40 char cap keeps the dump compact for list_members.

Add-batch context: empty string is rejected (must have content).
Update context uses ``AgeHintClearable`` below to support clearing."""


AgeHintClearable = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=40),
]
"""Codex Sub-A4 household R1 MAJOR #4: update path needs to support
clearing the field — runtime ``update_member`` stores whatever the
caller passes, including empty string. Empty string == clear.
``min_length`` is intentionally absent (vs ``AgeHint``)."""


FamilyNotes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
"""Free-form per-member notes — allergies, dietary preferences,
chronic conditions. 300 char cap is generous without ballooning
the prompt that includes list dumps."""


FamilyNotesClearable = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=300),
]
"""Codex Sub-A4 household R1 MAJOR #4: clearable variant for the
update path. Empty string == clear (e.g. «убери аллергию у Никиты»).
``min_length`` intentionally absent."""


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

    Codex Sub-A4 household R1 MAJOR #4: ``age_hint`` and ``notes``
    use *Clearable* variants (no min_length) so the planner can
    express «убери аллергию у Никиты» as ``notes=''``. Empty string
    means CLEAR the field. ``name`` and ``birth_year`` and ``role``
    cannot be cleared via update — for those use remove + re-add
    (cleared birth_year would orphan ages computed downstream;
    cleared name violates the dedup invariant; cleared role has no
    sensible default).

    Codex Sub-A4 household R1 (pre-CRITICAL): at least ONE updatable
    field must be non-None. Empty update payload is a planner
    mistake (would be a no-op at runtime); reject so the planner
    notices."""

    model_config = ConfigDict(extra="forbid")
    member_id: FamilyMemberId
    name: FamilyMemberName | None = None
    role: FamilyRole | None = None
    birth_year: BirthYear | None = None
    age_hint: AgeHintClearable | None = None
    notes: FamilyNotesClearable | None = None

    @model_validator(mode="after")
    def _validate_at_least_one_field(self) -> "UpdateFamilyMemberInput":
        if all(
            getattr(self, f) is None
            for f in ("name", "role", "birth_year", "age_hint", "notes")
        ):
            raise ValueError(
                "UpdateFamilyMemberInput requires at least one "
                "updatable field (name/role/birth_year/age_hint/notes). "
                "An empty update is a no-op at runtime — if the goal "
                "is to remove the member, use remove_family_member."
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
    description=(
        "Добавить одного или нескольких членов семьи за раз. "
        "Используй когда юзер описывает семью одной фразой («у меня "
        "жена Катя, сын Никита 10 лет»). Codex R1 MAJOR #5 — возраст: "
        "используй birth_year ТОЛЬКО когда юзер назвал явный год "
        "рождения («Никита 2015 года рождения»); фразы вида «10 лет» / "
        "«школьник» / «пенсионер» иди в age_hint, чтобы не угадывать "
        "год по текущему — на границе дня рождения это смещается. "
        "Codex R1 MAJOR #6 — роли: spouse=муж/жена, child=сын/дочь, "
        "parent=мама/папа, self=сам пользователь; всё остальное "
        "(сестра, брат, бабушка, дядя) → role='other' + точное "
        "отношение в notes. Имена дедуплицируются по нормализованному "
        "виду (case-insensitive); если все имена уже есть — статус "
        "ok:added:0:skipped_as_duplicate:M, иначе "
        "ok:added:N:skipped_as_duplicate:M:ids=[fm_,...]. Если не "
        "уверен что записи уже есть — list_family_members сначала."
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
        "Используй ДЛЯ ДОБАВЛЕНИЯ. Для правки существующего — update_family_member. Для удаления — remove_family_member.",
        "Дубликаты по нормализованному имени — runtime НЕ ошибка, они идут в skipped_as_duplicate. План не должен ретраить.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


LIST_FAMILY_MEMBERS_SPEC = ToolSpec(
    name="list_family_members",
    description=(
        "Показать всех записанных членов семьи юзера: имя, роль, "
        "возраст и заметки. Возвращает СТРУКТУРИРОВАННЫЙ список с "
        "member_id для каждого члена — используй эти id для "
        "update_family_member и remove_family_member (имена-в-промпте "
        "не выдумывай). Codex R1 MAJOR #2: меню/покупки используют "
        "household автоматически — НЕ нужно звать этот tool «для "
        "масштабирования», только когда юзер сам спрашивает про "
        "семью или когда тебе нужны member_id для правки/удаления. "
        "Возвращает раздельные статусы: ok (есть записи) и empty "
        "(никого нет — предложи юзеру add_family_members)."
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
        "Возвращает СЕМЬЮ юзера, не рецепты/покупки/задачи. Меню/покупки уже подтягивают household внутренне — звать list тут не нужно.",
        "Использует структурированный вывод (members[].member_id) — ссылайся на эти id при обновлении/удалении вместо парсинга prose.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


UPDATE_FAMILY_MEMBER_SPEC = ToolSpec(
    name="update_family_member",
    description=(
        "Обновить поля одного члена семьи. Используй когда юзер "
        "корректирует данные: «Маше 9 уже», «у Никиты теперь аллергия "
        "на молоко». Codex R1 MAJOR #3: если юзер называет члена по "
        "имени/роли, а member_id у тебя ещё нет — сначала вызови "
        "list_family_members, найди нужного по name, потом обновляй. "
        "Если несколько совпадений (двое детей с одним именем) — "
        "переспроси юзера. Передавай ТОЛЬКО те поля что меняются — "
        "остальные оставь None. Минимум одно поле должно быть "
        "non-None (пустой апдейт = no-op runtime = ошибка). "
        "Codex R1 MAJOR #4: очистить notes/age_hint можно передав "
        "пустую строку только если поле явно поддерживает clear (см. "
        "input_model description); birth_year не очищается через "
        "update — для этого remove + re-add. Возвращает ok:updated "
        "или error:member_not_found."
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
        "Используй ДЛЯ ПРАВКИ. Для добавления нового члена — add_family_members. Для удаления — remove_family_member.",
        "member_id берётся из list_family_members.members[i].member_id (СТРУКТУРИРОВАННЫЙ вывод, не парсить из prose).",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


REMOVE_FAMILY_MEMBER_SPEC = ToolSpec(
    name="remove_family_member",
    description=(
        "Удалить запись члена семьи. Используй ТОЛЬКО когда юзер явно "
        "просит убрать («Маша переехала, удали»), а не для коррекции "
        "(коррекция — update_family_member). Codex R1 MAJOR #3: если "
        "юзер называет члена по имени, а member_id у тебя ещё нет — "
        "сначала вызови list_family_members, найди нужного по name, "
        "потом удаляй. При нескольких совпадениях — переспроси юзера. "
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
        "Используй ТОЛЬКО для удаления. Коррекция полей — update_family_member.",
        "member_id берётся из list_family_members.members[i].member_id (СТРУКТУРИРОВАННЫЙ вывод).",
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
