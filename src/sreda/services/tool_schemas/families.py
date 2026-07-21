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
            "Для оплаты «купить билеты», «оформить подписку», «продли тариф» — это ДЕНЕЖНЫЕ ОПЕРАЦИИ (без MVP-tool, ответ юзеру).",
            "Для абстрактных «подумать о подарке» (без названия товара) — это ПАМЯТЬ; для «придумать сюрприз», «собрать идеи» — это ЗАДАЧИ.",
            "Для запроса внешней услуги «закажи такси / еду» — это ВНЕШНИЙ КАНАЛ (без MVP-tool, ответ юзеру).",
        ),
    ),
    "reminders": FamilyHeader(
        russian_name="НАПОМИНАНИЯ",
        purpose=(
            "Точечные напоминания во времени: «напомни в 18:00 X», "
            "«через час Y», «в субботу Z». Триггер — конкретный момент."
        ),
        anti_patterns=(
            # Codex Sub-A4 reminders R3 MAJOR #1: family header is
            # rendered into the planner prompt. Direct mentions of
            # ``attach_reminder`` / ``update_task`` / ``detach_reminder``
            # (from the not-yet-migrated tasks family) would steer
            # the planner toward unavailable typed tools. Softened to
            # family-level («группа ЗАДАЧИ») while tasks family is
            # not migrated; restore exact tool names once the tasks
            # specs ship.
            "Для повторяющихся дел без жёсткого времени (TODO-список) — см. группу ЗАДАЧИ.",
            "Для длинных чек-листов с шагами «собрать сумку» — см. группу ЧЕК-ЛИСТЫ.",
            "Если в просьбе ЕСТЬ конкретное время «напомни купить хлеб в 18:00» — это НАПОМИНАНИЯ (текст напоминания может включать «купи»). Если времени НЕТ — это ПОКУПКИ.",
            "Граница с ЗАДАЧАМИ — CREATE: standalone «напомни в 18:00 X» → НАПОМИНАНИЯ.schedule_reminder. Привязать НОВОЕ напоминание к существующей задаче «напомни про задачу T-42 завтра» → группа ЗАДАЧИ (привязка напоминания к задаче), не НАПОМИНАНИЯ.",
            "Граница с ЗАДАЧАМИ — UPDATE standalone: для напоминаний БЕЗ привязки к задаче «увеличь до 20:00», «измени текст» → НАПОМИНАНИЯ.update_reminder (берёт reminder_id из list_reminders).",
            "Граница с ЗАДАЧАМИ — UPDATE task-linked: для напоминания, ПРИВЯЗАННОГО к задаче, время напоминания зависит от task.time_start + reminder_offset_minutes. Изменить время задачи → группа ЗАДАЧИ (перепланирует напоминание). Изменить только offset перед задачей → группа ЗАДАЧИ с новым offset_minutes (перепривязка). НЕ зови update_reminder напрямую для task-linked — рискуешь рассинхроном offset'а.",
            "Граница с ЗАДАЧАМИ — REMOVE: «убери напоминание у задачи, оставь саму задачу» → группа ЗАДАЧИ (отвязка и отмена напоминания; standalone-копия НЕ создаётся). «Отмени напоминание целиком» → НАПОМИНАНИЯ.cancel_reminder. Сейчас обе операции отменяют напоминание; разница только в том, что отвязка ещё чистит ссылку в задаче.",
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
            "Граница с ЧЕК-ЛИСТАМИ: link_task_to_checklist связывает СУЩЕСТВУЮЩУЮ задачу с СУЩЕСТВУЮЩИМ чек-листом (логическая связь, оба остаются как отдельные сущности). move_task_to_checklist (в ЧЕК-ЛИСТАХ) превращает задачу в пункт чек-листа (задача сворачивается, отменяется). Если юзер «свяжи задачу со списком» — link_task_to_checklist. Если «перенеси задачу в список как пункт» — move_task_to_checklist.",
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
            "Первичная настройка профиля. Активная тема — только "
            "обращение/имя юзера (`addressing`). Остальные темы "
            "(self_intro/family/diet/routine/pain_point) — backlog, "
            "сейчас не в потоке."
        ),
        anti_patterns=(
            "Активный сейчас только topic=addressing. Остальные темы из TOPIC_DESCRIPTIONS — backlog, не вызывай для них onboarding_answered/onboarding_deferred.",
            "Для добавления нового члена семьи (после онбординга или вообще) — см. группу СЕМЬЯ.",
            "Для запоминания случайного факта в произвольной переписке — см. группу ПАМЯТЬ.",
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
            "Для отправки сообщения юзеру с кнопками — см. группу ИНТЕРФЕЙС; для обычного текстового ответа — это СБОРЩИК ОТВЕТА (не tool, не семья).",
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
            "Для оплаты / покупки билетов — это ДЕНЕЖНЫЕ ОПЕРАЦИИ (без MVP-tool, ответ юзеру).",
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
    "ДЕНЕЖНЫЕ ОПЕРАЦИИ",       # payments / billing — not MVP tool surface
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
# Tool family manifest — every implemented tool name → exactly one family.
# Source: ``plans/mellow-discovering-conway.md`` section "Реестр
# инструментов". Total 55 implemented tools.
# Codex R1 MAJOR #4 — proves taxonomy completeness against the real
# 55-tool universe before any ToolSpec is migrated to declare family.
# Codex Sub-A4 recipes R1 MAJOR #6: ``get_recipe_any_source``
# (architecture-map TODO-2) is intentionally NOT in this manifest
# until the runtime function ships — restore in the commit that adds
# the spec.
# ---------------------------------------------------------------------------


TOOL_FAMILY_MANIFEST: Final[Mapping[str, Family]] = MappingProxyType({
    # ---- shopping (8) -----------------------------------------------------
    "add_shopping_items": "shopping",
    "list_shopping": "shopping",
    "mark_shopping_bought": "shopping",
    "remove_shopping_items": "shopping",
    "update_shopping_item": "shopping",
    "update_shopping_items_category": "shopping",
    "clear_bought_shopping": "shopping",
    # #409: очистка ВСЕГО списка одним действием (pending → cancelled). До него «очисти список
    # покупок» требовала двух шагов (list_shopping → id → remove_shopping_items) и на проде
    # срывалась в 2 прогонах из 5.
    "clear_shopping_list": "shopping",
    # ---- reminders (4) ----------------------------------------------------
    "schedule_reminder": "reminders",
    "list_reminders": "reminders",
    "update_reminder": "reminders",
    "cancel_reminder": "reminders",
    # ---- recipes (6 + 1 from TODO-2 = 7) ---------------------------------
    "save_recipe": "recipes",
    "save_recipes_batch": "recipes",
    "search_recipes": "recipes",
    "get_recipe": "recipes",
    "update_recipe": "recipes",  # #210: правка рецепта на месте (не delete+save)
    "delete_recipe": "recipes",
    # Codex Sub-A4 recipes R1 MAJOR #6: ``get_recipe_any_source`` removed
    # from the manifest until the runtime function actually ships
    # (architecture-map TODO-2). A manifest that lists not-yet-built
    # tools risks leaking those names into planner-facing surfaces.
    # Restore this entry in the same commit that adds the tool's
    # ToolSpec + parser.
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
    # ---- checklists (10) --------------------------------------------------
    "create_checklist": "checklists",
    "add_checklist_items": "checklists",
    "list_checklists": "checklists",
    "show_checklist": "checklists",
    "get_checklist": "checklists",  # #213 Срез A: единый read (items|overview); старая пара → deprecated-алиасы
    "list_checklist_items": "checklists",
    "move_task_to_checklist": "checklists",
    "mark_checklist_item_done": "checklists",
    "update_checklist_item": "checklists",  # #210: правка пункта на месте (не delete+add)
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
    "create_memory_category": "memory",
    "save_episode": "memory",
    "recall_memory": "memory",
    # ---- utility (1) ------------------------------------------------------
    "log_unsupported_request": "utility",
    # ---- web (3) ----------------------------------------------------------
    # Codex R1 CRITICAL (both branches): names match @lc_tool-decorated
    # runtime function names, NOT build_*_tool factory names.
    "get_weather": "web",
    "web_search": "web",
    "fetch_url": "web",
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


# ReAct-only инструменты: зарегистрированы в манифесте (фильтр ReAct-набора
# `_EXTRA_FAMILIES` в react_loop пускает инструмент в набор Фредди только если
# у него есть запись с допустимой семьёй), но НАМЕРЕННО без plan-execute
# ``ToolSpec``. Причина: старый plan-execute планировщик заморожен/депрекейчен
# (канон 2026-06-19) — новые семейные инструменты идут только в ReAct, и
# тратиться на их spec-обвязку (input/output-схемы, парсеры, презентеры) для
# умирающего пути не нужно. Инвариант «у каждого манифест-инструмента есть
# spec» (``assert_manifest_matches_specs`` + семейные guard-тесты) исключает
# эти имена. #210: правка чек-листов/рецептов на месте.
REACT_ONLY_TOOLS: Final[frozenset[str]] = frozenset({
    "update_checklist_item",
    "update_recipe",
    "get_checklist",  # #213 Срез A: единый read чек-листов, только ReAct
    # #262b/#270: долг Среза 1 закрыт в #213 Срезе A — полный spec+parser
    # комплект для мёртвого пути раздувал prod-like префикс планировщика
    # (headroom-гейт) и требовал презентер. ReAct-only по канону #210.
    "create_memory_category",
    # #409: очистка всего списка покупок. Живёт только на ReAct-пути (Фредди);
    # спека/парсер/презентер для замороженного plan-execute не строятся — они
    # раздували prod-like префикс планировщика за headroom-гейт (#128).
    "clear_shopping_list",
})


# ---------------------------------------------------------------------------
# #221 Ф2: operation-class + read/write домены per-tool.
# Контракт фильтра «разрешённых разделов» (_apply_domain_policy в react_loop): инструмент привязывается,
# только если его read_domains ⊆ allowed_read И write_domains ⊆ allowed_write. КРИТИЧНО (R1-ревью): гейтить
# по write_domains, НЕ по family — кросс-доменные инструменты (семья ≠ мутируемое) иначе протекут.
# op_class: read_pure (чтение БД), read_external (внешний API), write (мутация durable-данных пользователя).
# Полнота (каждый инструмент манифеста классифицирован) проверяется на ИМПОРТЕ ниже (R1: unknown → raise,
# не молчаливый fail-closed на проде). Классификация — по реальному коду (см. plans/221 decision-log).
# ---------------------------------------------------------------------------
TOOL_OP_CLASS: Final[Mapping[str, str]] = MappingProxyType({
    "add_shopping_items": "write", "list_shopping": "read_pure", "mark_shopping_bought": "write",
    "remove_shopping_items": "write", "update_shopping_item": "write",
    "update_shopping_items_category": "write", "clear_bought_shopping": "write",
    "clear_shopping_list": "write",  # #409: деструктив — всегда через подтверждение, НЕ в autoexec
    "schedule_reminder": "write", "list_reminders": "read_pure", "update_reminder": "write",
    "cancel_reminder": "write",
    "save_recipe": "write", "save_recipes_batch": "write", "search_recipes": "read_pure",
    "get_recipe": "read_pure", "update_recipe": "write", "delete_recipe": "write",
    "plan_week_menu": "write", "update_menu_item": "write", "list_menu": "read_pure",
    "generate_shopping_from_menu": "write", "clear_menu": "write",
    "add_family_members": "write", "list_family_members": "read_pure", "update_family_member": "write",
    "remove_family_member": "write",
    "add_task": "write", "list_tasks": "read_pure", "update_task": "write", "complete_task": "write",
    "uncomplete_task": "write", "cancel_task": "write", "delete_task": "write",
    "attach_reminder": "write", "detach_reminder": "write", "link_task_to_checklist": "write",
    "unlink_task": "write",
    "create_checklist": "write", "add_checklist_items": "write", "list_checklists": "read_pure",
    "show_checklist": "read_pure", "get_checklist": "read_pure",  # #213 Срез A
    "list_checklist_items": "read_pure", "move_task_to_checklist": "write",
    "mark_checklist_item_done": "write", "update_checklist_item": "write",
    "delete_checklist_item": "write", "archive_checklist": "write",
    "onboarding_answered": "write", "onboarding_deferred": "write", "onboarding_complete": "write",
    "reply_with_buttons": "write",   # ToolSpec.effect=write (write_domains=ui); не в ReAct-наборе — скоуп моот
    "save_core_fact": "write", "save_episode": "write", "recall_memory": "read_pure",
    "create_memory_category": "write",
    "log_unsupported_request": "write",  # ToolSpec.effect=write (write_domains=utility); не в ReAct-наборе
    "get_weather": "read_external", "web_search": "read_external", "fetch_url": "read_external",
})
_VALID_OP_CLASSES: Final[frozenset[str]] = frozenset({"read_pure", "read_external", "write"})

# СЕМАНТИКА (R1-ревью Ф2, важно): read/write домены здесь = ДОМЕН СКОУПИНГА (на каком маршруте инструмент
# доступен), а НЕ литеральные домены ToolSpec.read_domains/write_domains (те описывают ВСЕ затронутые сторы для
# конфликт-детекта plan-execute и ШИРЕ). Литерал ToolSpec для скоупинга НЕ годится: напр. add_task имеет
# write=(tasks,checklists,reminders) — гейт по нему ОТРЕЗАЛ БЫ add_task на чистом tasks-маршруте (каскад
# напоминания/чек-листа — ВНУТРИ управления задачей, не отдельное намерение). Поэтому скоуп = family по дефолту.
# ДЕФОЛТ: read=(family,); write=(family,) если op_class=="write", иначе (). Override — ТОЛЬКО когда ПЕРВИЧНАЯ
# мутация в ДРУГОМ домене, чем family (настоящий кросс-домен). Единственный такой — generate_shopping_from_menu
# (семья menu, но создаёт СПИСОК ПОКУПОК). recall_memory НЕ нуждается в read-override: его family и так = memory,
# а ToolSpec-broadcast (checklists/reminders) для скоупинга ИГНОРИРУЕТСЯ дефолтом (=family) → recall доступен на
# memory-маршруте (R1 high). Read-оверрайдов сейчас нет.
_TOOL_READ_DOMAIN_OVERRIDES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({})
_TOOL_WRITE_DOMAIN_OVERRIDES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "generate_shopping_from_menu": ("shopping",),            # family=menu, но первичная запись — shopping
})


def tool_read_domains(name: str) -> frozenset[str]:
    """Домены, к которым инструмент относится по чтению/членству (дефолт = его семья)."""
    return frozenset(_TOOL_READ_DOMAIN_OVERRIDES.get(name, (TOOL_FAMILY_MANIFEST[name],)))


def tool_write_domains(name: str) -> frozenset[str]:
    """Домены, которые инструмент МУТИРУЕТ (∅ для не-write; дефолт write = его семья)."""
    if TOOL_OP_CLASS.get(name) != "write":
        return frozenset()
    return frozenset(_TOOL_WRITE_DOMAIN_OVERRIDES.get(name, (TOOL_FAMILY_MANIFEST[name],)))


# #213 Срез A: депрекейт-алиасы read-инструментов чек-листов. LLM-origin вызов
# старого имени при SREDA_CHECKLIST_UNIFIED=ON канонизируется в цель (react_loop
# tool-node, ДО unavailable-ветки; аргументы маппит _map_deprecated_args там же).
# Internal-пути (parse-путь housewife, replay, eval) зовут старые имена напрямую
# и от флага не зависят. Старые имена ОСТАЮТСЯ в манифесте/op-class (нужны
# internal-путям и тестам согласия); при ON они лишь скрыты из LLM-экспозиции.
DEPRECATED_TOOL_ALIASES: Final[Mapping[str, str]] = MappingProxyType({
    "list_checklists": "get_checklist",
    "show_checklist": "get_checklist",
})


def _validate_tool_op_metadata() -> None:
    """Import-time полнота/согласованность (R1-ревью: unknown op-class → raise, не молчание на проде)."""
    miss = set(TOOL_FAMILY_MANIFEST) - set(TOOL_OP_CLASS)
    extra = set(TOOL_OP_CLASS) - set(TOOL_FAMILY_MANIFEST)
    if miss or extra:
        raise RuntimeError(f"TOOL_OP_CLASS рассинхрон с манифестом: missing={miss}, extra={extra}")
    bad = {n: c for n, c in TOOL_OP_CLASS.items() if c not in _VALID_OP_CLASSES}
    if bad:
        raise RuntimeError(f"TOOL_OP_CLASS неизвестные классы: {bad}")
    for n in {**_TOOL_READ_DOMAIN_OVERRIDES, **_TOOL_WRITE_DOMAIN_OVERRIDES}:
        if n not in TOOL_FAMILY_MANIFEST:
            raise RuntimeError(f"override для несуществующего инструмента: {n!r}")
    # #213 Срез A: alias и его цель обязаны существовать в манифесте, цель не может
    # сама быть алиасом (иначе цепочка канонизации).
    for alias, target in DEPRECATED_TOOL_ALIASES.items():
        if alias not in TOOL_FAMILY_MANIFEST or target not in TOOL_FAMILY_MANIFEST:
            raise RuntimeError(f"deprecated-alias вне манифеста: {alias!r}→{target!r}")
        if target in DEPRECATED_TOOL_ALIASES:
            raise RuntimeError(f"deprecated-alias цепочка запрещена: {alias!r}→{target!r}")
    for n in _TOOL_WRITE_DOMAIN_OVERRIDES:
        if TOOL_OP_CLASS[n] != "write":
            raise RuntimeError(f"write-override для не-write инструмента: {n!r} ({TOOL_OP_CLASS[n]})")
    # R1-ревью MINOR: токены доменов в оверрайдах — реальные семьи (опечатка «shoping» иначе = тихий fail-closed).
    bad_tokens = {tok for vals in (*_TOOL_READ_DOMAIN_OVERRIDES.values(), *_TOOL_WRITE_DOMAIN_OVERRIDES.values())
                  for tok in vals if tok not in FAMILIES}
    if bad_tokens:
        raise RuntimeError(f"override содержит неизвестные домены (не в FAMILIES): {bad_tokens}")


_validate_tool_op_metadata()


# ---------------------------------------------------------------------------
# Runtime bridge: real ToolSpec set ↔ TOOL_FAMILY_MANIFEST agreement
# ---------------------------------------------------------------------------


def assert_manifest_matches_specs(specs: object) -> None:
    """Verify a collection of real ``ToolSpec`` instances matches the
    audited ``TOOL_FAMILY_MANIFEST`` exactly. Codex R3 MAJOR #1 — without
    this bridge, a real spec could declare the wrong family and still
    pass the ``НЕОТНЕСЁННЫЕ`` CI guard.

    Sub-A4 should call this from a CI test once real specs are wired
    in (one call passing all tenant tools). The check is here (not in
    Sub-A4 tests) so the invariant lives next to ``TOOL_FAMILY_MANIFEST``
    — if anyone edits the manifest, this function and its consumers
    enforce the bridge automatically.

    Raises ``AssertionError`` with a structured diff so CI output points
    directly at the offending tool(s).

    The ``specs`` argument is typed ``object`` to avoid a circular
    import on ``ToolSpec`` at module load — at call time we duck-type
    on ``.name`` and ``.family``.
    """

    # Build the actual map; tolerate iterables that are not lists.
    actual: dict[str, Family | None] = {}
    for spec in specs:  # type: ignore[attr-defined]
        name = getattr(spec, "name", None)
        family = getattr(spec, "family", None)
        if name is None:
            raise AssertionError(
                f"assert_manifest_matches_specs: spec {spec!r} has no "
                f"`name` attribute — not a valid ToolSpec."
            )
        if name in actual:
            raise AssertionError(
                f"assert_manifest_matches_specs: duplicate tool name "
                f"{name!r} in specs — every ToolSpec name must be unique."
            )
        actual[name] = family

    expected_names = set(TOOL_FAMILY_MANIFEST.keys())
    actual_names = set(actual.keys())
    # ReAct-only инструменты (#210) живут в манифесте, но НЕ имеют plan-execute
    # spec'а — не считаем их «пропавшими из спеков» (plan-execute заморожен).
    missing = expected_names - actual_names - REACT_ONLY_TOOLS
    extra = actual_names - expected_names
    family_mismatches = [
        (name, actual[name], TOOL_FAMILY_MANIFEST[name])
        for name in expected_names & actual_names
        if actual[name] != TOOL_FAMILY_MANIFEST[name]
    ]

    if missing or extra or family_mismatches:
        msgs = []
        if missing:
            msgs.append(f"  Missing from specs (in manifest): {sorted(missing)}")
        if extra:
            msgs.append(f"  Extra in specs (not in manifest): {sorted(extra)}")
        if family_mismatches:
            msgs.append("  Family mismatches (spec ≠ manifest):")
            for name, actual_fam, expected_fam in family_mismatches:
                msgs.append(
                    f"    {name!r}: spec={actual_fam!r} manifest={expected_fam!r}"
                )
        raise AssertionError(
            "Runtime ToolSpec set diverges from TOOL_FAMILY_MANIFEST:\n"
            + "\n".join(msgs)
            + "\n\nFix: update the manifest to match the new tool, or "
            "fix the spec's family declaration. See "
            "docs/architecture/tool-family-taxonomy.md."
        )


__all__ = [
    "FAMILIES",
    "FAMILY_HEADERS",
    "Family",
    "FamilyHeader",
    "NON_FAMILY_REDIRECTS",
    "NonFamilyRedirect",
    "REACT_ONLY_TOOLS",
    "TOOL_FAMILY_MANIFEST",
    "assert_manifest_matches_specs",
]
