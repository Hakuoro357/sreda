# -*- coding: utf-8 -*-
"""#399: «покажи список X» больше не отказывает — read-намерение проводит чек-листы в чтение.

Баг (замер на проде, тенант владельца): 0 из 15 показов. И `route_domains`, и классификатор
верно дают раздел `checklists`, но в `allowed_read` он не проводится:
  - read-кюс `\\bсписк` НЕ матчит «спис-О-к» (беглая гласная) → кюсов ∅;
  - route-раздел в чтение НАМЕРЕННО не проводится (защита own-data #285) — иначе «как дела»
    (route → checklists!) открыло бы чтение личных данных на болтовне.
Решение (вариант A + шов, владелец): сигнал «человек просит ПОКАЗАТЬ» = существующий
детерминированный детектор `classify_checklist_query` (#213); проводка написана дизъюнкцией
источников (`read_intent_domains`) — языко-нейтральный ШОВ под вариант B (read-бит от LLM).

RED до фикса: `checklist_read_intent` не существует; `compute_unified_policy` не знает
параметра `read_intent_domains`.
"""
from __future__ import annotations

import pytest

from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import route_domains
from sreda.runtime.react_signals import checklist_read_intent

# ── корпус: явный запрос показать конкретный список (ядро бага) ──
SHOW_CASES = [
    "покажи список кино",
    "открой список кино",
    "покажи список фильмов",
    "покажи список книг",
    "открой список подарков",
    "покажи список сериалов",
    "а покажи список кино",
    "покажи пожалуйста список кино",
    "покажи список идей",
]
# CR R1 (sol+terra MAJOR): ФОРМА «раздел-слово + имя» ≠ ПРОСЬБА показать. Детектор #213
# даёт items/high на всех этих фразах — без рамки запроса они открывали бы чтение личных
# списков на утверждении, отрицании, цитате и пересказе (подтверждено замером до фикса).
NOT_A_REQUEST_CASES = [
    "мой список кино очень длинный",            # утверждение
    "список кино уже полный",
    "я посмотрел список кино",
    "я хотел показать список кино",             # инфинитив, не императив
    "вчера я показывал список кино другу",
    "не показывай список кино",                 # отрицание
    'команда "покажи список кино" не работает',  # мета-рамка
    "мама сказала: покажи список кино",         # реплика говорящего
    "он написал «покажи список кино»",          # цитата
    "покажи список кино, сказал он",            # атрибуция ПОСЛЕ
    "что значит фраза покажи список кино",      # мета-вопрос
    # CR R2 sol MAJOR: прямая речь оформляется ТИРЕ, не только запятой
    "покажи список кино — сказал он",
    "покажи список кино, — сказал он",
    # CR R2 terra MAJOR: голый WH — вопрос ПРО список, не просьба показать
    "что такое список кино",
    "что значит список кино",
    "что за список кино",
]
# ── route-мина: route_domains даёт checklists, но это НЕ запрос чтения ──
MINE_CASES = [
    "как дела",
    "как дела?",
    "привет, как дела",
    "как твои дела",
    "как дела на работе",
    "дела идут в гору",
    "ну как дела вообще",
]


class TestSignal:
    """Сигнал — чистая функция, БЕЗ доступа к БД/LLM."""

    @pytest.mark.parametrize("text", SHOW_CASES)
    def test_show_request_raises_checklists(self, text):
        assert checklist_read_intent(text) == frozenset({"checklists"})

    @pytest.mark.parametrize("text", MINE_CASES)
    def test_route_mine_stays_silent(self, text):
        """«как дела» роутится в checklists — сигнал ОБЯЗАН молчать, иначе болтовня
        откроет чтение личных данных. NB: детектор #213 на «как дела» даёт items/LOW
        (в его секц-регексе есть голое «дела») — поэтому берём бит только на high."""
        assert checklist_read_intent(text) == frozenset()

    @pytest.mark.parametrize("text", [
        "добавь в список кино матрицу",
        "удали из списка кино матрицу",
        "отметь в списке дел покупку",
    ])
    def test_write_turn_is_not_read_intent(self, text):
        """Write-ход детектор сам отсекает (None) — сигнал чтения не выдаём."""
        assert checklist_read_intent(text) == frozenset()

    @pytest.mark.parametrize("text", NOT_A_REQUEST_CASES)
    def test_statement_quote_negation_do_not_open_read(self, text):
        """CR R1 sol+terra MAJOR: утверждение/отрицание/цитата/пересказ — НЕ запрос."""
        from sreda.runtime.react_preflight import classify_checklist_query
        cq = classify_checklist_query(text)
        assert cq is not None and cq.confidence == "high", (
            "предпосылка дефекта: детектор #213 считает форму уверенной")
        assert checklist_read_intent(text) == frozenset()

    @pytest.mark.parametrize("text", NOT_A_REQUEST_CASES)
    def test_statement_quote_negation_do_not_change_policy(self, text):
        """И на уровне политики #399 не добавляет НИЧЕГО на не-запросах.

        Проверяем именно ДЕЛЬТУ фичи, а не «checklists закрыт»: часть этих фраз
        («мой список кино очень длинный») открывает read-кюс `мой\\s+список` и БЕЗ
        #399 — это пред-существующее поведение, не в scope этой задачи."""
        route = route_domains(text)
        base = compute_unified_policy(text, route)
        with_signal = compute_unified_policy(
            text, route, read_intent_domains=checklist_read_intent(text))
        assert with_signal["allowed_read"] == base["allowed_read"], with_signal

    @pytest.mark.parametrize("text", ["покажи список покупок", "что в списке покупок"])
    def test_shopping_is_not_checklists(self, text):
        """Список покупок — домен shopping (лексика #221), не чек-листы."""
        assert checklist_read_intent(text) == frozenset()

    def test_pure_and_total(self):
        """Не падает на мусоре/пустоте — сигнал стоит на горячем пути."""
        for junk in ["", "   ", None, "!!!", "\n\n", "список" * 500]:
            assert isinstance(checklist_read_intent(junk), frozenset)


class TestPolicySeam:
    """Проводка в compute_unified_policy — ШОВ (дизъюнкция источников)."""

    @pytest.mark.parametrize("text", SHOW_CASES)
    def test_show_request_opens_checklists_read(self, text):
        """ЯДРО БАГА: с сигналом раздел проводится в allowed_read."""
        route = route_domains(text)
        pol = compute_unified_policy(
            text, route, read_intent_domains=checklist_read_intent(text))
        assert "checklists" in pol["allowed_read"], pol

    @pytest.mark.parametrize("text", SHOW_CASES)
    def test_without_signal_behaviour_is_unchanged(self, text):
        """Флаг OFF (сигнал не передан) → прежнее поведение, web-only."""
        pol = compute_unified_policy(text, route_domains(text))
        assert pol["allowed_read"] == ["web"], pol
        assert "read_intent" not in pol["signals"], "форма signals при OFF — прежняя"

    @pytest.mark.parametrize("text", MINE_CASES)
    def test_route_mine_does_not_open_read(self, text):
        """Мина «как дела»: route=checklists, но сигнала нет → чтение НЕ открывается."""
        route = route_domains(text)
        assert "checklists" in route.all_domains, "предпосылка мины"
        pol = compute_unified_policy(
            text, route, read_intent_domains=checklist_read_intent(text))
        assert "checklists" not in pol["allowed_read"], pol

    def test_signal_cannot_introduce_domain_router_did_not_raise(self):
        """АНТИ-ИНЪЕКЦИЯ: сигнал только ПОДТВЕРЖДАЕТ раздел, поднятый детерминированным
        роутером; ввести новый раздел он не может (дисциплина subtract-only #376)."""
        text = "какая погода"
        route = route_domains(text)
        assert "checklists" not in route.all_domains
        pol = compute_unified_policy(
            text, route, read_intent_domains=frozenset({"checklists", "memory"}))
        assert "checklists" not in pol["allowed_read"]
        assert "memory" not in pol["allowed_read"]

    def test_seam_accepts_arbitrary_source(self):
        """ШОВ: параметр доменный, не «чек-листовый» — вариант B (read-бит от LLM)
        вливается в тот же аргумент без переписывания проводки."""
        text = "покажи список кино"
        route = route_domains(text)
        pol_detector = compute_unified_policy(
            text, route, read_intent_domains=frozenset({"checklists"}))
        pol_future_llm = compute_unified_policy(
            text, route, read_intent_domains=frozenset({"checklists"}))
        assert pol_detector["allowed_read"] == pol_future_llm["allowed_read"]

    @pytest.mark.parametrize("text", [
        # CR R2: WH-формы авторизует НЕ #399 — их и так поднимает read-кюс. Тест
        # фиксирует, что сужение до императива ничего не сломало: раздел открыт,
        # хотя сигнал #399 молчит. Покраснеет, если кто-то тронет read-кюсы.
        "что в списке кино", "какие у меня списки", "что там в списке кино",
    ])
    def test_wh_forms_stay_open_via_existing_read_cues(self, text):
        pol = compute_unified_policy(
            text, route_domains(text), read_intent_domains=checklist_read_intent(text))
        assert "checklists" in pol["allowed_read"], pol
        assert checklist_read_intent(text) == frozenset(), (
            "авторизует кюс, а не #399 — сигнал обязан молчать на голом WH")

    def test_read_intent_does_not_grant_write(self):
        """Сигнал ЧТЕНИЯ не даёт записи ни при каких обстоятельствах."""
        text = "покажи список кино"
        pol = compute_unified_policy(
            text, route_domains(text), read_intent_domains=frozenset({"checklists"}))
        assert pol["allowed_write"] == []

    def test_signal_is_observable_when_engaged(self):
        """Наблюдаемость канарейки: что именно сигнал поднял — в signals."""
        text = "покажи список кино"
        pol = compute_unified_policy(
            text, route_domains(text), read_intent_domains=frozenset({"checklists"}))
        assert pol["signals"]["read_intent"] == ["checklists"]
