"""Tool family taxonomy + anti-pattern headers for the planner prompt.

Sub-A-77 item #1 (Epic #74): every family in the tool registry gets a
short "scope of use" block with explicit exclusions like
``⚠ НЕ ИСПОЛЬЗОВАТЬ для X — см. группу Y``. This shrinks family-confusion
errors at the LLM-attention level (Codex E-10 family-grouping was about
attention, not just token count — anti-patterns make the disambiguation
explicit instead of relying on the model to infer it from descriptions).

Budget: anti-pattern content is bounded at the **char level** by tests
(7200 chars hard cap for headers, 8000 chars soft cap for the full
12-family skeleton). Russian Cyrillic averages ~2 bytes/char but tokens
diverge by tokenizer; a rough «~50-100 tokens per family» estimate maps
to ≈300-600 chars in Russian — verify with a real tokenizer when Sub-B1
ships and adjust caps if the estimate is materially off. Treat the
char budget as the contract; the token claim is informational.

The family taxonomy is closed at 12 values — every tool in the 55-tool
registry maps to exactly one family via ``TOOL_FAMILY_MANIFEST`` (this
module). Adding a 13th family is a design decision (cross-family
confusion analysis + new headers + manifest review), not a casual
entry. The 12 categories match the family table in the plan-execute
architecture plan (`mellow-discovering-conway.md` — section "Реестр
инструментов").

This module is **pure data** — no imports from ToolSpec, no runtime
dependencies on the registry. Tests verify each header is well-formed
and stays within the token budget; the registry text renderer
(`registry_text.py`, separate module) consumes this dict.

Codex R1 follow-ups (see plans/sub-a-77-1-review-r1.md):
- Family literal vs FAMILIES tuple drift now checked via
  ``typing.get_args(Family)`` at import + in tests (MAJOR #1).
- ``ToolSpec.family`` is now ``Family | None = None`` so the patch is
  additive (MAJOR #3) — НЕОТНЕСЁННЫЕ surfacing in the renderer is
  reachable when a ToolSpec ships without a family declaration
  (MAJOR #2).
- ``TOOL_FAMILY_MANIFEST`` covers all 55 planned tool names → tests
  prove taxonomy completeness (MAJOR #4).
- Import-time invariants use explicit ``raise`` not ``assert`` so
  ``python -O`` doesn't strip them (MINOR #9).
- ``FAMILY_HEADERS`` wrapped in ``MappingProxyType`` so accidental
  mutation raises ``TypeError`` (MINOR #10).
- Non-family redirect destinations live in ``NON_FAMILY_REDIRECTS``
  so the planner knows ``денежные операции``, ``сборщик ответа``,
  ``внешний канал`` aren't tools (MAJOR #7).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal, Mapping, get_args

from pydantic import BaseModel, ConfigDict, Field


Family = Literal[
    "shopping",
    "reminders",
    "recipes",
    "menu",
    "household",
    "tasks",
    "checklists",
    "onboarding",
    "ui",
    "memory",
    "utility",
    "web",
]
"""Closed enumeration of tool families.

Every ``ToolSpec`` must declare exactly one family from this list. New
values require a new ``FAMILY_HEADERS`` entry + anti-pattern review (do
neighbouring families need cross-reference updates? does the new family
need its own anti-pattern block?).

Naming notes:
- ``household`` (not ``family``) — avoids name collision with the
  ``family`` field on ToolSpec; covers семья / nutrition / family members.
- ``utility`` — catch-all for system tools like ``log_unsupported_request``.
"""


FAMILIES: Final[tuple[Family, ...]] = (
    "shopping",
    "reminders",
    "recipes",
    "menu",
    "household",
    "tasks",
    "checklists",
    "onboarding",
    "ui",
    "memory",
    "utility",
    "web",
)
"""Canonical order families appear in the planner prompt.

Order = rough frequency in housewife traffic (shopping/reminders/recipes
top the list per `#68` LLM trace analysis). Stable iteration order
matters for prompt-cache hits — reshuffling the list would invalidate
the entire cached prefix on every tenant.
"""


class FamilyHeader(BaseModel):
    """Anti-pattern block prepended to each family in the planner prompt.

    ``purpose`` — one short sentence in Russian explaining when to look
    at this family. Plain text, no markdown.

    ``anti_patterns`` — bullet items, each of the shape «<сценарий> →
    <куда вместо этого>». Empty list is **not allowed** — every family
    must declare at least one explicit exclusion. Items themselves are
    rendered with a ``⚠ НЕ ИСПОЛЬЗОВАТЬ:`` marker by the renderer.

    Construction is frozen so the headers stay immutable at import time;
    accidental mutation in tests / scripts is caught at runtime.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    russian_name: str = Field(min_length=1)
    """Display name in the planner prompt, e.g. ``ПОКУПКИ``.

    Capitalized + Russian — matches how the registry is shown to the
    Russian-speaking planner LLM. Don't translate at render time;
    bake it in here.
    """

    purpose: str = Field(min_length=20)
    """One-sentence scope-of-use, Russian, ≥20 chars.

    The ≥20 lower bound rejects placeholder strings like «покупки» that
    would slip through ``min_length=1`` without being informative.
    """

    anti_patterns: tuple[str, ...] = Field(min_length=1)
    """Explicit exclusions, each ≥10 chars (enforced by validator below).

    Tuple (not list) — pydantic v2 makes this frozen alongside the
    parent model. Order matters: the strongest disambiguation goes
    first because LLM attention tapers off down the list.
    """


def _validate_anti_pattern_strings(headers: Mapping[Family, FamilyHeader]) -> None:
    """Enforce per-item invariants the pydantic Field can't express alone.

    Splitting this out of the FamilyHeader model keeps the model's own
    validator simple and lets us cross-check FAMILY_HEADERS as a whole
    at import time (e.g. forbid duplicates across families which would
    indicate a copy-paste bug).
    """

    seen: dict[str, Family] = {}
    for family, header in headers.items():
        for ap in header.anti_patterns:
            if len(ap.strip()) < 10:
                raise ValueError(
                    f"FAMILY_HEADERS[{family!r}].anti_patterns: item "
                    f"{ap!r} is shorter than 10 chars — "
                    f"anti-patterns must be informative, not stubs."
                )
            normalized = ap.strip().lower()
            if normalized in seen and seen[normalized] != family:
                raise ValueError(
                    f"FAMILY_HEADERS: duplicate anti-pattern {ap!r} in "
                    f"families {seen[normalized]!r} and {family!r}. "
                    f"Each anti-pattern belongs to exactly one family — "
                    f"if both families need the same exclusion, write "
                    f"it differently to reflect each family's angle."
                )
            seen[normalized] = family


# ---------------------------------------------------------------------------
# The 12 family headers — closed taxonomy. Edit cautiously: any change
# here invalidates the planner prompt cache for all tenants.
# ---------------------------------------------------------------------------


FAMILY_HEADERS: Final[Mapping[Family, FamilyHeader]] = {
    "shopping": FamilyHeader(
        russian_name="ПОКУПКИ",
        purpose=(
            "Управление физическим списком покупок в магазине: "
            "конкретные товары, которые юзер реально купит. "
            "«Купи молоко», «добавь хлеб в покупки», «отметь куплено»."
        ),
        anti_patterns=(
            "Для оплаты «купить билеты», «оформить подписку», «продли тариф» — это денежные операции, без MVP-tool; ответ юзеру без вызова.",
            "Для абстрактных «подумать о подарке» (без названия товара) — это ПАМЯТЬ; для «придумать сюрприз», «собрать идеи» — это ЗАДАЧИ.",
            "Для запроса внешней услуги «закажи такси / еду» — внешний канал, без MVP-tool; ответ юзеру без вызова.",
        ),
    ),
    "reminders": FamilyHeader(
        russian_name="НАПОМИНАНИЯ",
        purpose=(
            "Точечные напоминания во времени: «напомни в 18:00 X», "
            "«через час Y», «в субботу Z». Триггер — конкретный момент."
        ),
        anti_patterns=(
            "Для повторяющихся дел без жёсткого времени (TODO-список) — см. группу ЗАДАЧИ.",
            "Для длинных чек-листов с шагами «собрать сумку» — см. группу ЧЕК-ЛИСТЫ.",
            "Если в просьбе ЕСТЬ конкретное время «напомни купить хлеб в 18:00» — это НАПОМИНАНИЯ (текст напоминания может включать «купи»). Если времени НЕТ — это ПОКУПКИ.",
        ),
    ),
    "recipes": FamilyHeader(
        russian_name="РЕЦЕПТЫ",
        purpose=(
            "Личная книга рецептов и поиск рецептов с приготовлением блюд: "
            "состав, шаги, порции."
        ),
        anti_patterns=(
            "Для планирования меню на неделю — см. группу МЕНЮ (рецепты только хранят информацию).",
            "Для «добавь молоко в покупки» по найденному рецепту — это ПОКУПКИ, не рецепт.",
            "Для калорийности/диет без блюда — см. группу СЕМЬЯ (пищевые ограничения).",
        ),
    ),
    "menu": FamilyHeader(
        russian_name="МЕНЮ",
        purpose=(
            "Планирование меню на неделю по дням и приёмам пищи: "
            "что на завтрак/обед/ужин, генерация покупок по меню."
        ),
        anti_patterns=(
            "Для разового приготовления одного блюда — см. группу РЕЦЕПТЫ.",
            "Для напоминания «приготовить ужин в 19:00» — это НАПОМИНАНИЯ, меню только описывает что готовить.",
            "Для «куплю на завтра» без меню — это ПОКУПКИ, не меню.",
        ),
    ),
    "household": FamilyHeader(
        russian_name="СЕМЬЯ",
        purpose=(
            "Состав семьи и долговременные свойства членов семьи: "
            "дети, пищевые ограничения, аллергии, диетические привычки. "
            "СЕМЬЯ хранит «кто и какой», действия — в других группах."
        ),
        anti_patterns=(
            "Для разовых фактов из переписки (не атрибуты человека) — см. группу ПАМЯТЬ.",
            "Для напоминания «забрать Машу из школы» — это НАПОМИНАНИЯ; СЕМЬЯ хранит только состав/свойства, не действия с членом семьи.",
            "Для покупки чего-либо родственнику (товар) — это ПОКУПКИ; СЕМЬЯ не ведёт списки и не вызывается из «купи Y для Z».",
        ),
    ),
    "tasks": FamilyHeader(
        russian_name="ЗАДАЧИ",
        purpose=(
            "TODO-список повторяющихся или долгих дел без жёсткого "
            "времени: завершить, отменить, перепривязать."
        ),
        anti_patterns=(
            "Для точечного напоминания во времени «сделай Y в 17:00» — см. группу НАПОМИНАНИЯ.",
            "Для пошаговой инструкции / чек-листа из 5+ пунктов — см. группу ЧЕК-ЛИСТЫ.",
            "Для «купить молоко» — это ПОКУПКИ, не задача.",
        ),
    ),
    "checklists": FamilyHeader(
        russian_name="ЧЕК-ЛИСТЫ",
        purpose=(
            "Структурированные многошаговые списки с прогрессом: "
            "сборы, рутины, повторяющиеся процедуры."
        ),
        anti_patterns=(
            "Для одной задачи без шагов — см. группу ЗАДАЧИ.",
            "Для разовых пунктов в магазин — см. группу ПОКУПКИ.",
            "Для напоминания «делай шаг X в 18:00» — НАПОМИНАНИЯ, чек-лист только хранит структуру.",
        ),
    ),
    "onboarding": FamilyHeader(
        russian_name="ОНБОРДИНГ",
        purpose=(
            "Системные ответы юзера на вопросы первичной настройки: "
            "имя, состав семьи, предпочтения при регистрации."
        ),
        anti_patterns=(
            "Для добавления нового члена семьи после онбординга — см. группу СЕМЬЯ.",
            "Для запоминания случайного факта в произвольной переписке — см. группу ПАМЯТЬ.",
            "Для пропуска шагов с возвратом позже — используй только инструменты онбординга, не вызывай tasks/checklists.",
        ),
    ),
    "ui": FamilyHeader(
        russian_name="ИНТЕРФЕЙС",
        purpose=(
            "Вспомогательные действия с UI Telegram: кнопки, "
            "форматированные ответы с inline-выбором."
        ),
        anti_patterns=(
            "Для обычного текстового ответа — это работа СБОРЩИКА ОТВЕТА (не tool, не семья).",
            "Для голосовых уведомлений / медиа — UI tools работают только с inline-кнопками; без MVP-tool, ответ юзеру без вызова.",
            "Для долгих ack «работаю» — это уровень ОЧЕРЕДИ СООБЩЕНИЙ (не tool, не семья).",
        ),
    ),
    "memory": FamilyHeader(
        russian_name="ПАМЯТЬ",
        purpose=(
            "Долговременные факты и эпизоды из произвольной переписки "
            "для будущих обращений: «вспомни что я говорил про X»."
        ),
        anti_patterns=(
            "Для устойчивых атрибутов семьи (аллергии/диеты) — см. группу СЕМЬЯ.",
            "Для recall чек-листа/задачи/рецепта — это группы ЗАДАЧИ/ЧЕК-ЛИСТЫ/РЕЦЕПТЫ, не память.",
            "Для текущего короткого контекста разговора — это ИСТОРИЯ ХОДА (не tool, не семья).",
        ),
    ),
    "utility": FamilyHeader(
        russian_name="СЛУЖЕБНОЕ",
        purpose=(
            "Внутренние сигналы движку Среды: «запрос не поддерживается», "
            "«нужна эскалация», диагностика."
        ),
        anti_patterns=(
            "Для нормального отказа юзеру по бизнес-причине — это СБОРЩИК ОТВЕТА (не tool, не семья).",
            "Для логирования действий с данными — это АВТОМАТИЧЕСКИЙ АУДИТ (не tool, не семья), юзеру не виден.",
            "Для отправки сообщения юзеру — это группа ИНТЕРФЕЙС или СБОРЩИК ОТВЕТА.",
        ),
    ),
    "web": FamilyHeader(
        russian_name="ВЕБ",
        purpose=(
            "Запросы во внешний мир: погода, поиск в интернете, "
            "загрузка содержимого по URL для последующего парсинга."
        ),
        anti_patterns=(
            "Для поиска рецепта в личной книге — см. группу РЕЦЕПТЫ; веб — последний fallback.",
            "Для запросов «когда у меня встреча» — это КАЛЕНДАРЬ ЮЗЕРА (без MVP-tool, ответ юзеру).",
            "Для оплаты / покупки билетов — это денежные операции (без MVP-tool, ответ юзеру).",
        ),
    ),
}


# Validate the dict at import time so any future edit that introduces
# a duplicate / too-short anti-pattern fails loud immediately rather
# than after the prompt is built and shipped.
_validate_anti_pattern_strings(FAMILY_HEADERS)


# Coverage invariants: ``Family`` literal values, the ``FAMILIES`` tuple,
# and ``FAMILY_HEADERS`` keys must all be the same set. ``assert`` is
# stripped by ``python -O``; we use explicit ``raise`` so the invariant
# holds even under optimization (Codex R1 MINOR #9).
_LITERAL_VALUES: Final[frozenset[str]] = frozenset(get_args(Family))
_TUPLE_VALUES: Final[frozenset[str]] = frozenset(FAMILIES)
_HEADER_KEYS: Final[frozenset[str]] = frozenset(FAMILY_HEADERS.keys())
if not (_LITERAL_VALUES == _TUPLE_VALUES == _HEADER_KEYS):
    raise RuntimeError(
        "Family taxonomy drift: "
        f"Literal={_LITERAL_VALUES} TUPLE={_TUPLE_VALUES} "
        f"HEADERS={_HEADER_KEYS}. All three must match — see "
        "docstring for the closed-taxonomy invariant."
    )


# Wrap the dict in MappingProxyType so accidental mutation at runtime
# raises ``TypeError`` instead of silently rebinding ``FAMILY_HEADERS``
# (Codex R1 MINOR #10 — Final[Mapping[...]] only blocks rebinding for
# type checkers, not contents at runtime).
FAMILY_HEADERS = MappingProxyType(dict(FAMILY_HEADERS))  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Non-family redirect vocabulary — destinations that anti-patterns can
# point to which are NOT in the closed family taxonomy. Documented in one
# place so the planner can recognise «отдай юзеру», «без MVP-tool», etc.
# as legitimate non-tool destinations rather than mis-spelled family names.
# Codex R1 MAJOR #7.
# ---------------------------------------------------------------------------


NonFamilyRedirect = Literal[
    "СБОРЩИК ОТВЕТА",         # composer renders the reply directly
    "ИСТОРИЯ ХОДА",            # turn state, not stored via tool
    "ОЧЕРЕДЬ СООБЩЕНИЙ",       # ack/queue infrastructure
    "АВТОМАТИЧЕСКИЙ АУДИТ",    # audit_outbox/journal layer
    "КАЛЕНДАРЬ ЮЗЕРА",         # not yet integrated as tool
    "ВНЕШНИЙ КАНАЛ",           # 3rd party app (taxi, food delivery)
]


NON_FAMILY_REDIRECTS: Final[frozenset[str]] = frozenset(get_args(NonFamilyRedirect))
"""Legitimate destinations for anti-pattern redirects outside the
closed family taxonomy. Used by the registry-text test
``test_anti_pattern_redirects_resolve_to_known_destinations`` to verify
every mention of `<NAME>` follows the form `<FAMILY_NAME>` (a real
family) OR `<NON_FAMILY_REDIRECT>` (listed here) OR is qualified with
``без MVP-tool`` / ``не tool`` etc. — never a stray uppercase phrase
that the planner might mistake for a family name.
"""


# ---------------------------------------------------------------------------
# Tool family manifest — every planned tool name → exactly one family.
# Source: ``plans/mellow-discovering-conway.md`` section "Реестр
# инструментов" + TODO-2 add of get_recipe_any_source. Total 55 + 1.
# Codex R1 MAJOR #4 — proves taxonomy completeness against the real
# 55-tool universe before any ToolSpec is migrated to declare family.
# ---------------------------------------------------------------------------


TOOL_FAMILY_MANIFEST: Final[Mapping[str, Family]] = MappingProxyType({
    # ---- shopping (7) -----------------------------------------------------
    "add_shopping_items": "shopping",
    "list_shopping": "shopping",
    "mark_shopping_bought": "shopping",
    "remove_shopping_items": "shopping",
    "update_shopping_item": "shopping",
    "update_shopping_items_category": "shopping",
    "clear_bought_shopping": "shopping",
    # ---- reminders (4) ----------------------------------------------------
    "schedule_reminder": "reminders",
    "list_reminders": "reminders",
    "update_reminder": "reminders",
    "cancel_reminder": "reminders",
    # ---- recipes (5 + 1 from TODO-2 = 6) ---------------------------------
    "save_recipe": "recipes",
    "save_recipes_batch": "recipes",
    "search_recipes": "recipes",
    "get_recipe": "recipes",
    "delete_recipe": "recipes",
    "get_recipe_any_source": "recipes",
    # ---- menu (5) ---------------------------------------------------------
    "plan_week_menu": "menu",
    "update_menu_item": "menu",
    "list_menu": "menu",
    "generate_shopping_from_menu": "menu",
    "clear_menu": "menu",
    # ---- household (4) ----------------------------------------------------
    "add_family_members": "household",
    "list_family_members": "household",
    "update_family_member": "household",
    "remove_family_member": "household",
    # ---- tasks (11) -------------------------------------------------------
    "add_task": "tasks",
    "list_tasks": "tasks",
    "update_task": "tasks",
    "complete_task": "tasks",
    "uncomplete_task": "tasks",
    "cancel_task": "tasks",
    "delete_task": "tasks",
    "attach_reminder": "tasks",
    "detach_reminder": "tasks",
    "link_task_to_checklist": "tasks",
    "unlink_task": "tasks",
    # ---- checklists (8) ---------------------------------------------------
    "create_checklist": "checklists",
    "add_checklist_items": "checklists",
    "list_checklists": "checklists",
    "show_checklist": "checklists",
    "move_task_to_checklist": "checklists",
    "mark_checklist_item_done": "checklists",
    "delete_checklist_item": "checklists",
    "archive_checklist": "checklists",
    # ---- onboarding (3) ---------------------------------------------------
    "onboarding_answered": "onboarding",
    "onboarding_deferred": "onboarding",
    "onboarding_complete": "onboarding",
    # ---- ui (1) -----------------------------------------------------------
    "reply_with_buttons": "ui",
    # ---- memory (3) -------------------------------------------------------
    "save_core_fact": "memory",
    "save_episode": "memory",
    "recall_memory": "memory",
    # ---- utility (1) ------------------------------------------------------
    "log_unsupported_request": "utility",
    # ---- web (3) ----------------------------------------------------------
    "weather_tool": "web",
    "web_search_tool": "web",
    "fetch_url_tool": "web",
})
"""Static mapping of every planned MVP tool name to its family.

Used by tests to verify the family taxonomy actually covers the real
55-tool universe (Codex R1 MAJOR #4 — placeholder tools in unit tests
don't prove completeness on their own). Sub-A4 will turn each entry
into a real ``ToolSpec`` instance with the matching ``family`` field.

Adding a tool: append the name + family. Removing a tool: drop the
entry. Test ``test_manifest_covers_all_55_tools`` enforces the count
≥55 so accidental deletes fail loud.
"""


__all__ = [
    "FAMILIES",
    "FAMILY_HEADERS",
    "Family",
    "FamilyHeader",
    "NON_FAMILY_REDIRECTS",
    "NonFamilyRedirect",
    "TOOL_FAMILY_MANIFEST",
]
