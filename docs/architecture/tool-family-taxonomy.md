# Tool family taxonomy + anti-pattern headers

**Owner:** Plan-Execute Epic #74 / Sub-A-77 item #1
**Source code:** `src/sreda/services/tool_schemas/families.py`
**Tests:** `tests/unit/test_tool_families.py`, `tests/unit/test_tool_registry_text.py`
**Status:** foundation patch (Sub-A foundation); consumers ship in Sub-B1

## Why this exists

The plan-execute architecture replaces the legacy ReAct loop with a
planner LLM (`mimo-v2.5-pro`) that produces a structured plan up-front,
then an executor runs it deterministically. The planner sees a system
prompt with a Russian text registry of all available tools.

The 5-round design discussion that produced this taxonomy (recorded in
the parent repo at `vex-assistant/plans/mellow-discovering-conway-final.md`)
identified a recurring failure mode: the planner conflates близкие
семьи (НАПОМИНАНИЯ vs ЗАДАЧИ, ПОКУПКИ vs ИДЕИ). Codex review E-10
showed this is an LLM-attention problem, not just a token-budget
problem — anti-patterns make the disambiguation explicit instead of
relying on the model to infer it from descriptions.

## The 12-family closed taxonomy

| Family value | Russian display | Tools | Anti-pattern targets |
|---|---|---|---|
| `shopping` | ПОКУПКИ | 8 | ДЕНЕЖНЫЕ ОПЕРАЦИИ, ПАМЯТЬ, ЗАДАЧИ, ВНЕШНИЙ КАНАЛ |
| `reminders` | НАПОМИНАНИЯ | 4 | ЗАДАЧИ, ЧЕК-ЛИСТЫ, ПОКУПКИ |
| `recipes` | РЕЦЕПТЫ | 6 | МЕНЮ, ПОКУПКИ, СЕМЬЯ |
| `menu` | МЕНЮ | 5 | РЕЦЕПТЫ, НАПОМИНАНИЯ, ПОКУПКИ |
| `household` | СЕМЬЯ | 4 | ПАМЯТЬ, НАПОМИНАНИЯ, ПОКУПКИ |
| `tasks` | ЗАДАЧИ | 11 | НАПОМИНАНИЯ, ЧЕК-ЛИСТЫ, ПОКУПКИ |
| `checklists` | ЧЕК-ЛИСТЫ | 8 | ЗАДАЧИ, ПОКУПКИ, НАПОМИНАНИЯ |
| `onboarding` | ОНБОРДИНГ | 3 | СЕМЬЯ, ПАМЯТЬ, tasks/checklists |
| `ui` | ИНТЕРФЕЙС | 1 | СБОРЩИК ОТВЕТА, ОЧЕРЕДЬ СООБЩЕНИЙ |
| `memory` | ПАМЯТЬ | 3 | СЕМЬЯ, ЗАДАЧИ/ЧЕК-ЛИСТЫ/РЕЦЕПТЫ, ИСТОРИЯ ХОДА |
| `utility` | СЛУЖЕБНОЕ | 1 | СБОРЩИК ОТВЕТА, АВТОМАТИЧЕСКИЙ АУДИТ, ИНТЕРФЕЙС |
| `web` | ВЕБ | 3 | РЕЦЕПТЫ, КАЛЕНДАРЬ ЮЗЕРА, ДЕНЕЖНЫЕ ОПЕРАЦИИ |

Total: 61 manifest entries (`len(TOOL_FAMILY_MANIFEST)`, пин-тест
`test_manifest_total_size_pinned`). The future `get_recipe_any_source`
(architecture-plan TODO-2) will bump this when the runtime
function ships — currently NOT in `TOOL_FAMILY_MANIFEST` per Codex
Sub-A4 recipes R1 MAJOR #6.

Часть записей манифеста — **ReAct-only** (в манифесте ради фильтра
ReAct-набора, но БЕЗ `ToolSpec` для замороженного plan-execute; канон
#210, список — `families.REACT_ONLY_TOOLS`): `update_recipe`,
`update_checklist_item`, `get_checklist`, `create_memory_category`,
`clear_shopping_list` (#409). Полный комплект спека+парсер+презентер
для мёртвого пути раздувает prod-like префикс планировщика за
headroom-гейт #128 — потому его намеренно не строят.

The taxonomy is **closed** — adding a 13th family requires:
1. Adding the literal value to `Family` in `families.py`.
2. Adding the corresponding `FamilyHeader` to `FAMILY_HEADERS` with
   non-empty `anti_patterns`.
3. Adding/updating entries in `TOOL_FAMILY_MANIFEST`.
4. Reviewing cross-references in neighbouring families.
5. Updating this table.

`families.py` raises `RuntimeError` at import if any of these get out
of sync (Codex R1 MINOR #9).

## Non-family redirect vocabulary

Anti-patterns sometimes need to redirect the planner to a destination
that is **NOT** a tool family. To prevent the planner from inferring
pseudo-families from these phrases, they live in an enumerated
`NonFamilyRedirect` literal (Codex R2 MAJOR #1):

| Phrase | Meaning |
|---|---|
| СБОРЩИК ОТВЕТА | Composer renders the reply directly; no tool involved |
| ИСТОРИЯ ХОДА | Conversation turn state; tracked by the framework, not a tool |
| ОЧЕРЕДЬ СООБЩЕНИЙ | Ack / message-queue infrastructure |
| АВТОМАТИЧЕСКИЙ АУДИТ | `audit_outbox` / journal layer; invisible to user |
| КАЛЕНДАРЬ ЮЗЕРА | Calendar integration (not yet a tool surface) |
| ВНЕШНИЙ КАНАЛ | 3rd-party app (taxi, food delivery, payments) |
| ДЕНЕЖНЫЕ ОПЕРАЦИИ | Payments / billing; not MVP tool surface |

A scan test (`test_anti_pattern_redirects_resolve_to_known_destinations`)
verifies every all-caps Cyrillic phrase in any anti-pattern resolves to
either a real family name or one of these vocab values.

## Cross-domain boundary rules

Tools that span domains have explicit boundary rules in the anti-pattern
text so the planner picks the right family (Codex R2 MAJOR #3):

### Standalone reminder vs reminder attached to task

- **Standalone** «напомни в 18:00 X» → `reminders.schedule_reminder`
- **Attached to existing task** «напомни про задачу T-42 завтра» →
  `tasks.attach_reminder`

The rule lives in `FAMILY_HEADERS["reminders"].anti_patterns`.

### Link task to checklist vs move task into checklist

- **Logical link, both entities remain** «свяжи задачу Y с чек-листом
  C» → `tasks.link_task_to_checklist`
- **Convert task to checklist item, source task is folded in** «перенеси
  задачу Y в чеклист C как пункт» → `checklists.move_task_to_checklist`

The rule lives in `FAMILY_HEADERS["tasks"].anti_patterns`.

### Shopping intent disambiguation

Reconciles the trio shopping vs household vs reminders along an intent
dimension:

- **Concrete physical товар** «купи молоко», «купи подарок Маше» →
  `shopping.add_shopping_items`
- **Abstract идея без названия товара** «подумать о подарке для Маши»
  → `memory.save_episode` or `memory.save_core_fact`
- **Time-bound «купи в 18:00»** → `reminders.schedule_reminder`
  (the reminder text may include «купи»)

## Token budget

- **Char budget (contract):** 7200 chars hard cap on
  `FamilyHeader` content across all 12 families;
  8000 chars soft cap on the full 12-family skeleton (headers + one
  placeholder tool line per family). Enforced by unit tests.
- **Token estimate (informational):** ≈ 50-100 tokens per family ×
  12 = ≈ 1K tokens. Russian Cyrillic averages ~2 bytes/char but tokens
  diverge by tokenizer; the char budget is the contract until Sub-B1
  ships a real MiMo tokenizer integration.

## How the renderer composes the prompt

`render_registry_for_planner(specs: Iterable[ToolSpec]) -> str`:

```
ГРУППА: <RUSSIAN_NAME> (<N> <plural>)
<purpose>
⚠ НЕ ИСПОЛЬЗОВАТЬ:
  • <anti_pattern_1>
  • <anti_pattern_2>
  ...

  <tool_name>(<args_summary>) — <description first line>
  ...
```

Russian plurals follow grammar (1 → «инструмент», 2-4 → «инструмента»,
5+ / 11-14 → «инструментов»). Cache-stability invariants:
- Families render in canonical `FAMILIES` tuple order.
- Tools within a family render alphabetically by `name`.
- Empty families are skipped.
- Tools without a recognised family surface in a final
  `ГРУППА: НЕОТНЕСЁННЫЕ (ОШИБКА КОНФИГА)` block — never silently dropped.

## Migration path

This patch is **additive**:
- `ToolSpec.family: Family | None = None` — optional default keeps
  Sub-A1 / Sub-A4 unblocked.
- `TOOL_FAMILY_MANIFEST` proves the 12-family taxonomy actually covers
  the 56-tool universe before any real ToolSpec migrates.

**Sub-A4** will turn each manifest entry into a real
`ToolSpec(family="…")`.

**Sub-B1** (planner LLM client) will:
1. Build the planner system prompt using
   `render_registry_for_planner(real_specs)` for the cacheable prefix.
2. Add a CI guard that fails the build if the rendered registry
   contains any `ГРУППА: НЕОТНЕСЁННЫЕ` block (i.e. no real ToolSpec
   may ship without a `family` declaration).

## Related docs

- Architecture plan (parent repo):
  `vex-assistant/plans/mellow-discovering-conway-final.md` —
  "Реестр инструментов" section, "Группа 2 — Validator-driven
  parallelism" section.
- Sub-A-77 enhancement set: `vex-assistant#77`.
- Plan-execute epic: `vex-assistant#74` / `sreda#52`.
