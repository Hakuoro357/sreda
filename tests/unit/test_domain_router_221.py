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
import pytest

from sreda.runtime import react_loop
from sreda.runtime.react_routing_data import FAMILY_EXACT_ROOTS, FAMILY_ROOTS
from sreda.runtime.react_preflight import (
    _DOMAIN_PRIORITY,
    _MUST_TASK_PATTERNS,
    _SEC_CHECKLIST_WORDS,
    _SEC_REMINDER_ROOTS,
    _SEC_TASK_ROOTS,
    _clear_ontology_cache_for_tests,
    _ontology,
    route_domains,
)


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
    ("погода на завтра", "web"),
])
def test_route_primary_domain(text, expected_primary):
    assert route_domains(text).primary_domain == expected_primary


def test_route_spisok_pokupok_suppresses_checklists():
    r = route_domains("что у меня в списке покупок")
    assert r.primary_domain == "shopping"
    assert "checklists" in r.suppressed_domains
    assert r.compound_by_connector is False


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
