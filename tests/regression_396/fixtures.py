"""Фикстуры живых багов надёжности (#396).

Каждая фикстура: начальное состояние БД (``seed``) + скрипт диалога (``turns``,
скриптованные ходы модели) + декларативные ожидания (мутации/фразы/потолки/
инварианты). Судятся ``harness.check_fixture`` (универсальные инварианты +
ожидания). ЗАКРЫТЫЕ баги / синтетика → зелёные; ОТКРЫТЫЕ баги → красные на текущем
main (помечены xfail(strict) в ``test_fixtures.py``, XPASS-сигнал при починке).

Судим по СОСТОЯНИЮ (БД + квитанции + текст реплики + потолки), не «ответ похож».
"""
from __future__ import annotations

from .harness import Fixture, ScriptedTurn, ai_text, ai_tool


# ─────────────────────────── сиды начального состояния БД ───────────────────────────

def _seed_lists(session, user, lists: dict[str, list[str]]) -> None:
    """Создать активные чек-листы с пунктами."""
    from sreda.services.checklists import ChecklistService
    svc = ChecklistService(session)
    for title, items in lists.items():
        cl = svc.create_list(tenant_id=user.tenant_id, user_id=user.user_id, title=title)
        if items:
            svc.add_items(list_id=cl.id, items=items)


def _list_status(session, user, title: str) -> str | None:
    """Статус списка по названию, ВКЛЮЧАЯ archived (find_list_by_title ищет только
    активные → после архива вернул бы None). Прямой запрос по всем статусам."""
    from sreda.db.models import Checklist
    session.expire_all()
    needle = title.strip().lower()
    for cl in session.query(Checklist).filter_by(
            tenant_id=user.tenant_id, user_id=user.user_id).all():
        if (cl.title or "").strip().lower() == needle:
            return cl.status
    return None


# ─────────────────────────── #393 — филлер вместо отчёта (ЗАКРЫТ) ───────────────────────────

F393_ADD = Fixture(
    id="393_add_filler",
    bug="#393",
    summary="успешный add_checklist_items + филлер «приняла к сведению» → реплика ОБЯЗАНА "
            "называть список и пункты (страховка react_result_report). Инв.1 + называние результата.",
    turns=[
        ScriptedTurn(
            "Добавь в список Дача грабли, лопату и перчатки",
            [ai_tool("add_checklist_items",
                     {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]}),
             ai_text("Хорошо, приняла к сведению.")]),
    ],
    ground_result_turns=[0],
    forbid_phrases=["приняла к сведению"],
    require_phrases=[(0, "дача"), (0, "грабл"), (0, "лопат"), (0, "перчатк")],
    expect_mutations={"checklists": 1, "checklist_items": 3},
)


F393_ARCHIVE_RESUME = Fixture(
    id="393_archive_resume_dump",
    bug="#393",
    summary="archive_checklist через confirm → «да» → филлер-дамп чужих списков → реплика ОБЯЗАНА "
            "называть «Дача», а не перечислять другие списки. Пост-confirm заземление.",
    seed=lambda s, u: _seed_lists(s, u, {"Дача": [], "Кино": [], "Поход": []}),
    turns=[
        ScriptedTurn(
            "Удали список Дача",
            [ai_tool("archive_checklist", {"list_id_or_title": "Дача"})]),
        ScriptedTurn(
            "да",
            [ai_text("Вот твои списки дел: Кино, Поход.")],
            is_auto=True),
    ],
    require_phrases=[(1, "дача")],
    forbid_phrases=["кино", "поход"],
    max_confirms=1,
    expect_final=lambda s, u: (
        [] if _list_status(s, u, "Дача") == "archived"
        else [f"список «Дача» не заархивирован (status={_list_status(s, u, 'Дача')})"]),
)


# ─────────────────────────── #390 — техвыдача pending/done/total (ОТКРЫТ) ───────────────────────────

F390_SHOW_LISTS = Fixture(
    id="390_show_lists_tech_leak",
    bug="#390",
    summary="«покажи списки» → get_checklist(overview) отдаёт сырой англ. формат "
            "«N pending, M done, K total» + id — модель пересказывает как есть. Инв.5: "
            "публичный ответ ∩ тех-поля = ∅. Красный по СЫРОМУ ВЫВОДУ инструмента (реальный код).",
    seed=lambda s, u: _seed_lists(s, u, {
        "Кино к просмотру": ["Дюна", "Оппенгеймер"], "Поход": ["палатка", "спички", "котелок"]}),
    turns=[
        ScriptedTurn(
            "покажи мои списки",
            [ai_tool("get_checklist", {"mode": "overview"}),
             ai_text("Вот твои списки: Кино к просмотру и Поход.")]),
    ],
    require_clean_receipts=True,  # ← красный сейчас: инструмент эмитит англ. счётчики
)


# ─────────────────────────── #389 — лишний confirm на добавление (ЗАКРЫТ) ───────────────────────────

F389_SHOPPING_ADD_NO_CONFIRM = Fixture(
    id="389_shopping_add_no_confirm",
    bug="#389",
    summary="«добавь X в покупки» — аддитивная низкорисковая операция БЕЗ встречного "
            "подтверждения. Потолок confirm=0, позиции добавлены.",
    turns=[
        ScriptedTurn(
            "добавь молоко и хлеб в покупки",
            [ai_tool("add_shopping_items", {"items": [{"title": "молоко"}, {"title": "хлеб"}]}),
             ai_text("Готово, добавила молоко и хлеб в список покупок.")]),
    ],
    max_confirms=0,
    expect_mutations={"shopping": 2},
    forbid_phrases=["добавить?"],
)


# ─────────────────────────── #362 — ложная отмена на записи (ОТКРЫТ) ───────────────────────────

F362_DIRECT_WRITE = Fixture(
    id="362_direct_write_not_cancelled",
    bug="#362",
    summary="прямая запись данных «Сахар утром 16 и 2» → add_task ok → реплика подтверждает "
            "запись, НЕ «Отменила, ничего не делаю» (потеря медданных). Инв.4a: мутация ⇒ не отмена.",
    turns=[
        ScriptedTurn(
            "Сахар утром 16 и 2",
            [ai_tool("add_task", {"title": "сахар утром 16, вечером 2"}),
             ai_text("Записала: сахар утром 16, вечером 2.")]),
    ],
    expect_mutations={"tasks": 1},
    forbid_phrases=["отменила", "ничего не делаю"],
)


# ─────────────────────────── синтетика: инв.3 идемпотентность ───────────────────────────

F_IDEMPOTENCY = Fixture(
    id="synthetic_idempotency",
    bug="inv3",
    summary="ОДИН idempotency_key (тот же inbound_message_id → тот же turn_key/operation_id) на "
            "двух ходах ⇒ ≤1 мутация. R2 (ревью sol+terra): пинним inbound_id, иначе тест мерил "
            "лишь дедуп по имени, а не replay-идемпотентность ключа. Инв.3.",
    turns=[
        ScriptedTurn(
            "добавь молоко в покупки",
            [ai_tool("add_shopping_items", {"items": [{"title": "молоко"}]}),
             ai_text("Готово, добавила молоко.")],
            inbound_id="idem-key-1"),
        ScriptedTurn(
            "добавь молоко в покупки",
            [ai_tool("add_shopping_items", {"items": [{"title": "молоко"}]}),
             ai_text("Молоко уже в списке.")],
            inbound_id="idem-key-1"),   # ← ТОТ ЖЕ ключ: реальный replay
    ],
    idempotent_group=([0, 1], "shopping"),
    expect_mutations={"shopping": 1},
)


# ─────────────────────────── синтетика: инв.4 неоднозначная отмена ───────────────────────────

F_AMBIGUOUS_CANCEL = Fixture(
    id="synthetic_ambiguous_cancel",
    bug="inv4",
    summary="неоднозначная цель отмены ⇒ 0 мутаций. R2 (ревью sol+terra): модель РЕАЛЬНО зовёт "
            "деструктивный archive_checklist с needle «дела», совпадающим с ДВУМЯ списками — "
            "runtime не должен молча заархивировать ни один (резолвер+confirm-гейт). Инв.4 по "
            "ЛОГИЧЕСКОМУ снимку (статусы), не только count.",
    seed=lambda s, u: _seed_lists(s, u, {"дела на дачу": ["первое"], "дела на сегодня": ["второе"]}),
    turns=[
        ScriptedTurn(
            "удали список дела",
            [ai_tool("archive_checklist", {"list_id_or_title": "дела"}),
             ai_text("Уточни, какой именно список удалить?")]),
    ],
    ambiguous_cancel_turns=[0],
    expect_final=lambda s, u: (
        [] if all(_list_status(s, u, t) == "active" for t in ("дела на дачу", "дела на сегодня"))
        else ["неоднозначная отмена заархивировала список: "
              + repr({t: _list_status(s, u, t) for t in ("дела на дачу", "дела на сегодня")})]),
)


# Каталог: (фикстура, ожидается_зелёной). ОТКРЫТЫЕ баги → False (xfail(strict), XPASS при починке).
GREEN_FIXTURES = [
    F393_ADD,
    F393_ARCHIVE_RESUME,
    F389_SHOPPING_ADD_NO_CONFIRM,
    F362_DIRECT_WRITE,
    F_IDEMPOTENCY,
    F_AMBIGUOUS_CANCEL,
]
OPEN_BUG_FIXTURES = [
    F390_SHOW_LISTS,   # #390 открыт: сырой формat инструмента англ. → красный до фикса
]
