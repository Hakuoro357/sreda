"""#221 Ф1 — тесты единого ontology-роутера доменов `route_domains`. Имена СИНХРОНЫ с чеклистом приёмки.

Контракт (план v5 §1 + R1-ревью кода):
- `route_domains(text) -> RouteResult` — одна карта «слово→раздел(ы)», заменяет логику трёх словарей
  (_MUST_TASK_PATTERNS #197 / _SEC_* #215 / _FAMILY_ROOTS #165).
- Классы улик: primary/secondary/suppressed/compound_by_connector + all_domains (primary∪secondary для Ф3).
- compound ТОЛЬКО при союзе МЕЖДУ клаузами; longest-match через фразы; longest-match-priority тай-брейк.
- Две проекции: active_families (домены ∩ ленивые, БЕЗ core reminders/tasks); intent_hint=task ТОЛЬКО от
  task-сигнала #197/#215 (НЕ от семейных корней — «#197 финален»).
- intent_only: команда без раздела («что у меня») → intent=task, domain=None.
- keyword-coverage (без молчаливой потери) + НЕГАТИВЫ (нет ложных срабатываний).
"""
import asyncio

import pytest

from sreda.runtime import react_loop
from sreda.runtime.react_routing_data import FAMILY_EXACT_ROOTS, FAMILY_ROOTS
from sreda.runtime.react_preflight import (
    DomainClassResult,
    _DOMAIN_PRIORITY,
    _MUST_TASK_PATTERNS,
    _SEC_CHECKLIST_WORDS,
    _SEC_REMINDER_ROOTS,
    _SEC_TASK_ROOTS,
    _clear_ontology_cache_for_tests,
    _ontology,
    _parse_domains,
    classify_domains,
    compute_allowed_domains,
    route_domains,
)


class _FakeLLM:
    """Мини-LLM для classify_domains: ainvoke → AIMessage(content) или raise (fail-open)."""
    def __init__(self, content=None, exc=None):
        self._content, self._exc = content, exc

    async def ainvoke(self, _messages):
        if self._exc is not None:
            raise self._exc
        from langchain_core.messages import AIMessage
        return AIMessage(content=self._content)


# ── ontology: слово → правильный раздел (longest-match + suppression) ──────────────────────────────
@pytest.mark.parametrize("text,expected_primary", [
    ("покажи дела", "checklists"),
    ("покажи мои дела", "checklists"),
    ("покажи список кино к просмотру", "checklists"),
    ("заведи чек-лист сборы в школу", "checklists"),
    ("покажи задачи", "tasks"),
    ("покажи мои задачи", "tasks"),
    ("напомни завтра погладить кота", "reminders"),
    ("покажи напоминания", "reminders"),
    ("купи молоко", "shopping"),
    ("добавь хлеб в покупки", "shopping"),
    ("меню на неделю", "menu"),
    ("рецепт борща", "recipes"),
    ("кто у меня в семье", "household"),
    ("мой муж", "household"),
    ("запомни число 47", "memory"),
    # #270: команды создания категории детерминированно роутятся в memory (ТОЧНЫЕ формы «категория»)
    # — иначе флагман-команда create_memory_category не доедет до Фредди (write-гейт #221 fail-closed).
    ("заведи категорию машина", "memory"),
    ("создай категорию работа", "memory"),
    ("новая категория здоровье", "memory"),
    ("запомни в категорию машина купить масло", "memory"),
    ("сделай раздел путешествия", "memory"),  # R3: creation-контекст «раздел» → memory (детерминированно)
    # R2/R3-регрессия: генерик «раздел»/«категорическ*» И голое «категория» (общее с shopping) больше
    # НЕ перехватывают чужие домены — memory только в creation-контексте.
    ("покажи раздел покупок", "shopping"),
    ("что в разделе меню", "menu"),
    ("молоко в категорию молочные", "shopping"),  # R3 MAJOR: shopping-команда update_shopping_item
    ("погода на завтра", "web"),
])
def test_route_primary_domain(text, expected_primary):
    assert route_domains(text).primary_domain == expected_primary


def test_route_category_creation_reaches_memory_write_270():
    """#270 acceptance: «заведи категорию X» даёт all_domains=(memory,) → write-гейт #221 пропустит
    create_memory_category (write_domains={memory} ⊆ allowed). Без детерминированного корня всё висело
    на LLM-фолбэке (R1 Claude MAJOR)."""
    for text in ("заведи категорию машина", "создай категорию работа", "новая категория здоровье",
                 "запомни в категорию машина купить масло", "сделай раздел путешествия"):
        r = route_domains(text)
        assert r.all_domains == ("memory",), f"{text!r} → {r.all_domains}, ожидали (memory,)"


def test_route_shopping_category_not_hijacked_by_memory_270():
    """#270 R3 MAJOR (оба Codex): «молоко в категорию молочные» — это shopping-команда
    (update_shopping_item, у товара есть категория), а НЕ память. Голое «категория» больше не
    роутит в memory; memory только в creation-контексте (_MEMORY_CREATION_PHRASES)."""
    assert route_domains("молоко в категорию молочные").primary_domain == "shopping"
    assert route_domains("добавь молоко в категорию молочка").primary_domain == "shopping"
    assert route_domains("добавь в список покупок молоко в категорию молочка").primary_domain == "shopping"


def test_route_creation_phrase_does_not_suppress_content_270():
    """#270 R4 (Claude MAJOR): creation-фраза инъектит memory ТОЛЬКО когда в клаузе НЕТ контент-домена
    (react_preflight.route_domains). Иначе «создай раздел покупок»/«новый раздел меню» глушили бы
    shopping/menu (memory — action-домен). Чистая creation без контент-слова → memory."""
    assert route_domains("создай раздел покупок").primary_domain == "shopping"
    assert route_domains("новый раздел меню на неделю").primary_domain == "menu"
    assert route_domains("создай раздел путешествия").all_domains == ("memory",)
    assert route_domains("заведи категорию машина").all_domains == ("memory",)


def test_category_creation_intent_is_task_270():
    """#270 R2: интент команд создания категории = task ДЕТЕРМИНИРОВАННО. Доменный блок react_loop
    гейтится intent==task; без этого команда висела бы на LLM-классификаторе (R2 Claude MAJOR)."""
    from sreda.runtime.react_preflight import _must_task
    for text in ("заведи категорию машина", "создай категорию работа", "новая категория здоровье",
                 "новую категорию дом", "сделай раздел путешествия", "новый раздел работа"):
        assert _must_task(text), f"{text!r} должно быть intent=task"


def test_route_generic_razdel_not_hijacked_by_memory_270():
    """#270 R2 регрессия (Claude MAJOR): генерик «раздел»/«категорическ*» НЕ уходят в memory —
    перехватывали shopping/menu/chat, т.к. memory это action-домен с высоким приоритетом."""
    assert route_domains("покажи раздел покупок").primary_domain == "shopping"
    assert route_domains("что в разделе меню").primary_domain == "menu"
    assert route_domains("ты категорически не права").primary_domain != "memory"
    assert route_domains("раздели пиццу пополам").primary_domain != "memory"


def test_route_spisok_pokupok_suppresses_checklists():
    r = route_domains("что у меня в списке покупок")
    assert r.primary_domain == "shopping"
    assert "checklists" in r.suppressed_domains
    assert r.compound_by_connector is False
    # #250/#374: у shopping директивы НЕТ (None). #374-R2 пробовал дать shopping-директиву, но она
    # инжектилась и на write/compound/cross-ходы покупок и глушила их (R2 sol/terra/субагент N1-N4) →
    # откат. Покупки разводит few-shot + оговорка в _HINT_CHECKLIST, без хвостовой shopping-директивы.
    assert r.directive is None


def test_route_spisok_kino_is_checklists_not_shopping():
    r = route_domains("покажи список кино")
    assert r.primary_domain == "checklists"
    assert "shopping" not in (r.secondary_domains or ())


# ── морфология (формы, дефис, пробел, ё/е) ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "что у меня в делах", "покажи в списке", "веди чек лист", "заведи чек-лист"])
def test_route_morphology_checklists(text):
    assert route_domains(text).primary_domain == "checklists"


# ── классы улик: составное ТОЛЬКО при союзе МЕЖДУ клаузами, не от пересечения/любого союза ──────────
def test_route_compound_by_connector():
    r = route_domains("добавь молоко в покупки и напомни купить хлеб завтра")
    assert r.compound_by_connector is True
    assert {"shopping", "reminders"} <= set(r.all_domains)


def test_route_no_compound_without_connector():
    r = route_domains("покажи список кино")
    assert r.compound_by_connector is False
    assert not r.secondary_domains


def test_route_connector_not_between_domains_is_not_compound():
    """«в списке покупок и всё» — союз есть, но второй клаузы-домена нет → НЕ compound (R1 MAJOR)."""
    r = route_domains("что у меня в списке покупок и всё")
    assert r.compound_by_connector is False
    assert r.primary_domain == "shopping"


def test_route_compound_core_plus_core_exposes_all_domains():
    """reminders+tasks оба в ядре → active_families пуст, но all_domains несёт оба (для Ф3 split, R1 MAJOR)."""
    r = route_domains("напомни завтра и поставь задачу")
    assert r.compound_by_connector is True
    assert set(r.all_domains) == {"reminders", "tasks"}
    assert r.active_families == ()  # оба резидентны в ядре → предзагружать нечего


# ── intent_hint — ТОЛЬКО от task-сигнала #197/#215, НЕ от семейных корней (рефрейм «#197 финален») ──
@pytest.mark.parametrize("text", ["что у меня сегодня", "что мне нужно сделать", "перенеси на завтра"])
def test_route_intent_only_no_false_domain(text):
    r = route_domains(text)
    assert r.intent_hint == "task"
    assert r.intent_only is True
    assert r.primary_domain is None


@pytest.mark.parametrize("text", [
    "найди мне про Пушкина", "расскажи про погоду в Париже", "найди новости про выборы",
    "найди мне рецепт борща", "погода на завтра"])
def test_route_family_root_alone_does_not_set_task(text):
    """web/recipes-корни на fact/chat-запросах НЕ ставят intent_hint=task (иначе потеря deepseek, #197)."""
    assert route_domains(text).intent_hint is None


def test_route_intent_hint_task_for_section_word():
    assert route_domains("покажи дела").intent_hint == "task"      # _section_hint (#215) → task-сигнал
    assert route_domains("напомни завтра").intent_hint == "task"   # reminder-корень = task-сигнал


def test_route_chatlike_no_domain_no_task():
    r = route_domains("расскажи анекдот")
    assert r.intent_hint is None
    assert r.primary_domain is None


# ── две проекции карты ────────────────────────────────────────────────────────────────────────────
def test_active_families_excludes_core_reminders_tasks():
    assert route_domains("покажи задачи").primary_domain == "tasks"
    assert "tasks" not in route_domains("покажи задачи").active_families
    assert "reminders" not in route_domains("напомни завтра").active_families


def test_active_families_includes_lazy_domains():
    assert "checklists" in route_domains("покажи дела").active_families
    assert "shopping" in route_domains("купи молоко").active_families
    assert "web" in route_domains("погода на завтра").active_families


# ── директива (#215 сохранена) ────────────────────────────────────────────────────────────────────
def test_directive_checklists_mentions_list_checklists():
    d = route_domains("покажи дела").directive or ""
    assert "list_checklists" in d and "list_tasks" in d


# ── НЕГАТИВЫ: нет ложных срабатываний (R1 MAJOR — «тест незнакомца» на отсутствие false-positive) ──
@pytest.mark.parametrize("text,not_domain", [
    ("памятник пушкину стоит", "memory"),       # «памят» не должен ловить «памятник»
    ("списание со счёта банка", "checklists"),  # «списа» ≠ «списк»
    ("сырок и детский сад", "household"),        # «сыр/детский» не household
])
def test_route_no_false_positive(text, not_domain):
    r = route_domains(text)
    assert not_domain not in set(r.all_domains)
    # R2 MINOR: негативы должны и intent_hint держать None (опасный режим — ложный task, не только домен)
    assert r.intent_hint is None
    assert r.active_families == ()


def test_route_v_pamyati_is_memory():
    assert route_domains("что у меня в памяти").primary_domain == "memory"


# ── R2 MAJOR: memory-императив = ДЕЙСТВИЕ (выше content); «запомни X» не должен красть content ──────
@pytest.mark.parametrize("text", [
    "запомни рецепт борща", "сохрани погоду на завтра", "заметка про меню на неделю"])
def test_route_memory_imperative_beats_content(text):
    assert route_domains(text).primary_domain == "memory"


# ── R2 MAJOR: рецепт сильнее meal-контекста (recipes раньше menu) ──────────────────────────────────
@pytest.mark.parametrize("text", ["рецепт на ужин", "найди рецепт завтрака", "что приготовить на ужин"])
def test_route_recipe_beats_meal_context(text):
    assert route_domains(text).primary_domain == "recipes"


# ── R2 MAJOR (high): широкая фраза не должна тащить fact-вопрос в task ──────────────────────────────
@pytest.mark.parametrize("text", [
    "что мне нужно знать про Пушкина", "что мне нужно понять про физику"])
def test_route_broad_phrase_does_not_overclaim_task(text):
    r = route_domains(text)
    assert r.intent_hint is None
    assert r.intent_only is False


# ── R2 MINOR (субагент): action-домен бьёт content при бóльшем счёте content-токенов (priority-first) ─
def test_route_priority_first_within_clause():
    assert route_domains("напомни купить молоко хлеб яйца").primary_domain == "reminders"


# ── R3 MAJOR (high #1): глагол-действие важнее фразы (фраза НЕ обходит action-приоритет) ────────────
@pytest.mark.parametrize("text,expected", [
    ("запомни список покупок", "memory"),            # «запомни» (action) > фраза «список покупок»→shopping
    ("напомни список покупок вечером", "reminders"),  # «напомни» (action) > фраза
    ("сохрани список дел", "memory"),
])
def test_route_action_verb_beats_phrase(text, expected):
    assert route_domains(text).primary_domain == expected


# ── R3 MAJOR (high #2): союз между ТОВАРАМИ (не командами) — НЕ compound ───────────────────────────
def test_route_item_list_after_action_is_not_compound():
    r = route_domains("напомни купить молоко и хлеб завтра")
    assert r.compound_by_connector is False
    assert r.primary_domain == "reminders"


def test_route_two_commands_with_connector_is_compound():
    """Контроль: ДВЕ команды (оба — task-сигнал) через союз → compound."""
    r = route_domains("добавь молоко в покупки и напомни про встречу")
    assert r.compound_by_connector is True
    assert {"shopping", "reminders"} <= set(r.all_domains)


# ── R4 MAJOR (high #1): инфинитив в ТЕЛЕ напоминания — не вторая команда (нет task-сигнала) ─────────
def test_route_infinitive_in_reminder_payload_not_compound():
    r = route_domains("напомни позвонить маме и купить хлеб завтра")
    assert r.compound_by_connector is False
    assert r.primary_domain == "reminders"


# ── R4 MAJOR (medium): команда без глагола-в-списке, но с task-сигналом («список покупок») — compound ─
def test_route_command_via_task_signal_not_verb_list():
    r = route_domains("внеси молоко в список покупок и напомни про встречу")
    assert r.compound_by_connector is True
    assert {"shopping", "reminders"} <= set(r.all_domains)


# ── R4 MAJOR (medium): явная команда не первой клаузой не теряется молча ────────────────────────────
def test_route_single_command_not_first_is_primary():
    assert route_domains("молоко и напомни про встречу").primary_domain == "reminders"


# ── keyword-coverage: каждый триггер старых словарей покрыт (миграция без потерь) ──────────────────
def test_coverage_must_task_patterns():
    missed = [p for p in _MUST_TASK_PATTERNS if route_domains(p).intent_hint != "task"]
    assert not missed, f"_MUST_TASK без покрытия: {missed}"


def test_coverage_section_roots():
    for root in _SEC_REMINDER_ROOTS:
        assert route_domains(root + "и завтра").primary_domain == "reminders"
    for root in _SEC_TASK_ROOTS:
        assert route_domains(root + "и").primary_domain == "tasks"
    for w in _SEC_CHECKLIST_WORDS:
        assert route_domains("покажи " + w).primary_domain == "checklists"


def test_coverage_all_family_roots_reach_domain():
    """КАЖДЫЙ корень (не только roots[0]) достигает свою семью (R1 MAJOR: молчаливая потеря не-первых)."""
    for fam, roots in FAMILY_ROOTS.items():
        for root in roots:
            r = route_domains("покажи " + root + "xyz")  # корень-префикс реального токена
            assert fam in set(r.all_domains), f"корень {root!r} не достиг {fam}"


def test_coverage_all_exact_roots_reach_domain():
    for fam, exacts in FAMILY_EXACT_ROOTS.items():
        for tok in exacts:
            r = route_domains("про " + tok)
            assert fam in set(r.all_domains), f"exact {tok!r} не достиг {fam}"


# ── дрейф онтологии: все домены в приоритете; _prio не падает ──────────────────────────────────────
def test_all_ontology_domains_prioritized():
    onto_domains = set(_ontology()["roots"])
    missing = onto_domains - set(_DOMAIN_PRIORITY)
    assert not missing, f"домены вне _DOMAIN_PRIORITY (упадёт сортировка): {missing}"


def test_ontology_cache_clear_smoke():
    _ontology()
    _clear_ontology_cache_for_tests()  # не должно падать; следующий вызов пересоберёт
    assert "checklists" in _ontology()["roots"]


# ── Ф2: доменный фолбэк-классификатор ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected_domains,conf", [
    ("checklists", ("checklists",), "high"),
    ("weather", ("web",), "high"),                 # weather → web (семья)
    ("shopping, reminders", ("shopping", "reminders"), "low"),  # несколько → low
    ("какой-то мусор", (), "low"),                  # нет валидного домена → low
    ("", (), "low"),
])
def test_parse_domains(raw, expected_domains, conf):
    r = _parse_domains(raw)
    assert r.domains == expected_domains and r.confidence == conf


def test_classify_domains_high_single():
    r = asyncio.run(classify_domains([], "покажи дела", _FakeLLM(content="checklists")))
    assert r.domains == ("checklists",) and r.confidence == "high"


def test_classify_domains_failopen_on_error():
    """Сбой LLM → пусто+low (fail-open в графе по политике, НЕ угаданный домен)."""
    r = asyncio.run(classify_domains([], "что-то", _FakeLLM(exc=RuntimeError("timeout"))))
    assert r == DomainClassResult((), "low")


# ── Ф2: метаданные op-class + read/write домены ────────────────────────────────────────────────────
def test_tool_op_metadata_complete():
    """Полнота: КАЖДЫЙ инструмент манифеста классифицирован (import-time assert уже это гарантирует)."""
    from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST, TOOL_OP_CLASS
    assert set(TOOL_OP_CLASS) == set(TOOL_FAMILY_MANIFEST)


@pytest.mark.parametrize("name,read,write", [
    ("list_shopping", {"shopping"}, set()),                       # read_pure: write=∅
    ("add_shopping_items", {"shopping"}, {"shopping"}),            # обычный write = family
    ("generate_shopping_from_menu", {"menu"}, {"shopping"}),       # ЕДИНСТВ. кросс-override: family menu→write shopping
    ("attach_reminder", {"tasks"}, {"tasks"}),                    # скоуп = family tasks (каскад reminders — внутри)
    ("move_task_to_checklist", {"checklists"}, {"checklists"}),    # скоуп = family checklists
    ("recall_memory", {"memory"}, set()),                        # family memory (broadcast — внутр. деталь, не скоуп)
    ("get_weather", {"web"}, set()),                             # read_external: домен web, write=∅
    ("add_task", {"tasks"}, {"tasks"}),                          # КЛЮЧ: write=family tasks (не вся triple из ToolSpec)
    ("cancel_task", {"tasks"}, {"tasks"}),
])
def test_tool_domains_scoping(name, read, write):
    from sreda.services.tool_schemas.families import tool_read_domains, tool_write_domains
    assert set(tool_read_domains(name)) == read
    assert set(tool_write_domains(name)) == write


# ── Ф2: _apply_domain_policy ───────────────────────────────────────────────────────────────────────
class _T:
    def __init__(self, name): self.name = name


def _names(tools):
    return {t.name for t in tools}


def test_apply_policy_none_no_filter():
    from sreda.runtime.react_loop import _apply_domain_policy
    tools = [_T("add_task"), _T("list_shopping")]
    assert _apply_domain_policy(tools, None, None) is tools  # OFF/legacy — без фильтра


def test_apply_policy_meta_always_pass():
    from sreda.runtime.react_loop import _apply_domain_policy
    out = _apply_domain_policy([_T("ask_human"), _T("need_family")], set(), set())
    assert _names(out) == {"ask_human", "need_family"}


def test_apply_policy_read_gated():
    from sreda.runtime.react_loop import _apply_domain_policy
    out = _apply_domain_policy([_T("list_checklists"), _T("list_tasks")], {"checklists"}, set())
    assert _names(out) == {"list_checklists"}  # list_tasks read=tasks не разрешён


def test_apply_policy_write_gated():
    from sreda.runtime.react_loop import _apply_domain_policy
    tools = [_T("add_shopping_items"), _T("list_shopping")]
    assert _names(_apply_domain_policy(tools, {"shopping"}, set())) == {"list_shopping"}  # запись запрещена
    assert _names(_apply_domain_policy(tools, {"shopping"}, {"shopping"})) == {"add_shopping_items", "list_shopping"}


def test_apply_policy_cross_domain_write_checked():
    """generate_shopping_from_menu пишет shopping: при allowed_write без shopping — НЕ привязывается (R1)."""
    from sreda.runtime.react_loop import _apply_domain_policy
    t = [_T("generate_shopping_from_menu")]
    assert _apply_domain_policy(t, {"menu", "household"}, {"menu"}) == []          # shopping не в write
    assert len(_apply_domain_policy(t, {"menu", "household"}, {"menu", "shopping"})) == 1


def test_apply_policy_recall_memory_scoped_to_memory():
    """recall_memory скоуп=memory: на не-memory маршруте отрезан, на memory — есть (R1 high: не резать на memory)."""
    from sreda.runtime.react_loop import _apply_domain_policy
    assert _apply_domain_policy([_T("recall_memory")], {"checklists"}, set()) == []   # не memory-маршрут
    assert len(_apply_domain_policy([_T("recall_memory")], {"memory"}, set())) == 1   # memory-маршрут


def test_apply_policy_unknown_failclosed():
    from sreda.runtime.react_loop import _apply_domain_policy
    assert _apply_domain_policy([_T("totally_unknown_tool")], {"shopping"}, {"shopping"}) == []


def test_apply_policy_link_task_alias_survives():
    """link_task (бэспоук, runtime-имя≠манифест) канонизируется → не fail-closed (R1 CRITICAL)."""
    from sreda.runtime.react_loop import _apply_domain_policy
    out = _apply_domain_policy([_T("link_task")], {"tasks", "checklists"}, {"tasks", "checklists"})
    assert _names(out) == {"link_task"}


def test_apply_policy_add_task_not_cut_on_tasks_route():
    """Контрпример против ToolSpec-литерала: add_task/cancel_task на чистом tasks-маршруте НЕ режутся."""
    from sreda.runtime.react_loop import _apply_domain_policy
    out = _apply_domain_policy([_T("add_task"), _T("cancel_task")], {"tasks"}, {"tasks"})
    assert _names(out) == {"add_task", "cancel_task"}


def test_every_core_tool_survives_policy_at_its_domain():
    """Pin (R1): КАЖДОЕ имя из _CORE_TOOL_NAMES проходит фильтр при широком task-наборе (ловит fail-closed по имени)."""
    from sreda.runtime.react_loop import _CORE_TOOL_NAMES, _apply_domain_policy
    allowed = {"tasks", "reminders", "checklists", "memory"}
    survived = {t.name for t in _apply_domain_policy([_T(n) for n in _CORE_TOOL_NAMES], allowed, allowed)}
    assert survived == set(_CORE_TOOL_NAMES), f"core отрезаны фильтром: {set(_CORE_TOOL_NAMES) - survived}"


def test_op_class_matches_toolspec_effect():
    """Сверка (R1 medium/субагент): op_class write↔read согласован с ToolSpec.effect — ловит дрейф классификации."""
    import importlib
    from sreda.services.tool_schemas.families import TOOL_OP_CLASS
    eff_by_name = {}
    for m in ("specs_tasks", "specs_shopping", "specs_reminders", "specs_recipes", "specs_menu",
              "specs_household", "specs_checklists", "specs_memory", "specs_web", "specs_ui",
              "specs_utility", "specs_onboarding"):
        mod = importlib.import_module("sreda.services.tool_schemas." + m)
        for v in vars(mod).values():
            if isinstance(v, (list, tuple)):
                for s in v:
                    if hasattr(s, "name") and hasattr(s, "effect"):
                        eff_by_name[s.name] = s.effect
    mismatch = [(n, TOOL_OP_CLASS[n], eff) for n, eff in eff_by_name.items()
                if n in TOOL_OP_CLASS and (TOOL_OP_CLASS[n] == "write") != (eff == "write")]
    assert not mismatch, f"op_class ≠ ToolSpec.effect: {mismatch}"


# ── Ф2: compute_allowed_domains (уверенный раздел → write; удаление под confirm-гейтом; fail-open) ──
def test_allowed_deterministic_single_read_and_write():
    r = route_domains("покажи дела")  # детерм. checklists
    ar, aw = compute_allowed_domains(r, None)
    assert ar == frozenset({"checklists"}) and aw == frozenset({"checklists"})


def test_allowed_compound_read_both_no_write():
    """Составное (детерм.) → читать оба, писать НИ В ОДИН (compound-write → уточнение, не авто)."""
    r = route_domains("добавь молоко в покупки и напомни про встречу")
    ar, aw = compute_allowed_domains(r, None)
    assert ar == frozenset({"shopping", "reminders"}) and aw == frozenset()


def test_allowed_llm_fallback_high_grants_write_221():
    """#221 (Борис 2026-06-29): нет детерм. домена, LLM high (ровно один) → read+write (раньше read-only).
    Безопасность УДАЛЕНИЯ держит confirm-гейт «Да/Нет», а не запрет записи — иначе «убери X» терял инструмент
    удаления (инцидент «убери куриное филе» → отказ «нет возможности удалить»). RED до фикса: write был ∅."""
    r = route_domains("расскажи анекдот")  # нет детерм. домена
    ar, aw = compute_allowed_domains(r, DomainClassResult(("shopping",), "high"))
    assert ar == frozenset({"shopping"})
    assert aw == frozenset({"shopping"})


def test_allowed_low_confidence_is_explicit_deny():
    """Низкая уверенность/нет домена → ∅/∅ = явный deny (ask_human-only), НЕ None."""
    r = route_domains("расскажи анекдот")
    ar, aw = compute_allowed_domains(r, DomainClassResult((), "low"))
    assert ar == frozenset() and aw == frozenset()
    ar2, aw2 = compute_allowed_domains(r, DomainClassResult(("a", "b"), "low"))  # мульти-low → deny
    assert ar2 == frozenset() and aw2 == frozenset()


def test_generate_shopping_reachable_full_pipeline():
    """РЕАЛЬНЫЙ пайплайн route→active_families→_select_tools→compute→_apply_domain_policy (R3 субагент CRITICAL):
    «составь покупки из меню» делает generate_shopping_from_menu ДЕЙСТВИТЕЛЬНО доступным (загрузка семьи + фильтр)."""
    from sreda.runtime.react_loop import _apply_domain_policy, _select_tools
    r = route_domains("составь покупки из меню")
    assert r.cross_intent == "menu_to_shopping"
    assert "menu" in r.active_families and "shopping" in r.active_families  # обе семьи предзагружены
    ar, aw = compute_allowed_domains(r, None)
    assert ar == frozenset({"menu", "shopping"}) and aw == frozenset({"shopping"})
    # реальный отбор кандидатов по active_families (НЕ ручная подача), затем доменный фильтр:
    all_tools = [_T("generate_shopping_from_menu"), _T("list_menu"), _T("list_shopping"), _T("list_checklists")]
    selected = _select_tools(all_tools, r.active_families)
    assert "generate_shopping_from_menu" in _names(selected)  # семья menu загружена → инструмент в наборе
    assert "generate_shopping_from_menu" in _names(_apply_domain_policy(selected, ar, aw))  # пережил фильтр


def test_cross_intent_directional_reverse_no_false_write():
    """Обратное направление «меню из покупок» (нет «из меню») → НЕ menu_to_shopping; generate_shopping недостижим."""
    r = route_domains("составь меню из покупок")
    assert r.cross_intent is None
    ar, aw = compute_allowed_domains(r, None)
    assert ar != frozenset({"menu", "shopping"}) or aw != frozenset({"shopping"})  # не кросс-политика


def test_cross_intent_robust_to_connector():
    """Маркер «из меню» устойчив к союзу-ХВОСТУ (не команда): «...из меню и без орехов» всё равно кросс (R3 MINOR)."""
    r = route_domains("составь покупки из меню и без орехов")
    assert r.cross_intent == "menu_to_shopping"


def test_cross_intent_not_override_compound_command():
    """Cross + ДРУГАЯ команда в соседней клаузе → cross НЕ применяется (не теряем reminders, R4 high/medium)."""
    r = route_domains("напомни про ужин и составь покупки из меню")
    assert r.cross_intent is None
    ar, aw = compute_allowed_domains(r, None)
    assert "reminders" in ar and aw == frozenset({"reminders"})  # команда reminders сохранена, без cross-write shopping


@pytest.mark.parametrize("text", [
    "убери салат из меню и добавь молоко в покупки",  # «из меню»+menu в кл.1, shopping в кл.2 — РАЗНЫЕ клаузы
    "что из меню вкусное и покажи покупки",            # два read в разных клаузах
])
def test_cross_intent_no_false_cross_co_occurrence(text):
    """Со-встречаемость menu+shopping в РАЗНЫХ клаузах → НЕ cross (R4 субагент: иначе ложная shopping-запись)."""
    r = route_domains(text)
    assert r.cross_intent is None
    _, aw = compute_allowed_domains(r, None)
    assert aw != frozenset({"shopping"}) or r.primary_domain == "shopping"  # cross-write shopping не навязан


# ── #308: «запиши/сохрани В СПИСОК X» → checklists (глагол памяти не должен перебивать «в список») ──


def test_route_zapishi_v_spisok_is_checklists_308():
    """#308 (прод-деградация владельца 2026-07-04): «Запиши в список кино посмотреть Пикард» уходил
    в memory (глагол «запиши»=action memory перебивал «список»=checklists) → checklists-инструменты
    отрезаны → петля 8 проходов → «не смог». «в список» (target чек-листа) перевешивает глагол памяти."""
    for text in (
        "запиши в список кино посмотреть пикард",
        "запиши в список кино к просмотру посмотреть сериал пикард",
        "сохрани в список кино дюну",
        "запиши в список поход палатку",
    ):
        assert route_domains(text).primary_domain == "checklists", text


def test_route_v_spisok_pokupok_still_shopping_308():
    """#308 анти-регресс: «в список покупок» остаётся shopping (более специфичный форсер, longest-match),
    НЕ перехватывается общим «в список»→checklists."""
    assert route_domains("запиши в список покупок молоко").primary_domain == "shopping"
    assert route_domains("добавь в список покупок хлеб и масло").primary_domain == "shopping"
    assert route_domains("в список покупок яйца").primary_domain == "shopping"


def test_route_memory_verbs_without_spisok_still_memory_308():
    """#308 анти-регресс: глагол памяти БЕЗ «в список» по-прежнему → memory (не сломать заметки)."""
    assert route_domains("запиши что я люблю кофе").primary_domain == "memory"
    assert route_domains("запомни номер машины 47").primary_domain == "memory"
    assert route_domains("сохрани что встреча в пятницу").primary_domain == "memory"


def test_route_dobav_pokazhi_v_spisok_unchanged_308():
    """#308 анти-регресс: «добавь/покажи в список» уже давали checklists — не сломать."""
    assert route_domains("добавь в список кино пикард").primary_domain == "checklists"
    assert route_domains("покажи список кино к просмотру").primary_domain == "checklists"
