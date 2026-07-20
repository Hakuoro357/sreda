"""RED-тесты САМОГО харнесса: каждый инвариант ЛОВИТ нарушение (и молчит на чистом).

Доказательство режущей способности на уровне инвариантов — синтетика, без
``handle_turn``. Если инвариант перестанет ловить нарушение (регресс детектора) —
эти тесты краснеют. Так «зелёная фикстура» не сможет тихо потерять проверку.
"""
from __future__ import annotations

from .invariants import (
    inv_ambiguous_cancel_zero_mutations,
    inv_failed_not_done,
    inv_idempotency,
    inv_mutated_not_cancelled,
    inv_no_false_success,
    inv_public_no_tech,
    tech_tokens,
)
from .model import DialogOutcome, ToolReceipt, TurnOutcome

_Z = {"shopping": 0, "tasks": 0, "checklists": 0, "checklist_items": 0, "reminders": 0}


def _turn(reply: str, *, tool_calls=(), receipts=(), before=None, after=None,
          awaiting_confirm=False, user_text="u") -> TurnOutcome:
    before = dict(before if before is not None else _Z)
    after = dict(after if after is not None else before)
    return TurnOutcome(
        user_text=user_text, is_auto=False, reply=reply,
        awaiting_confirm=awaiting_confirm, db_before=before, db_after=after,
        tool_calls=list(tool_calls), receipts=list(receipts))


def _dialog(*turns: TurnOutcome) -> DialogOutcome:
    return DialogOutcome(fixture_id="synthetic", turns=list(turns), final_db={})


def _ok_receipt(name: str) -> ToolReceipt:
    return ToolReceipt(name=name, result_kind="ok", content="okv2:added:...", status="applied")


def _err_receipt(name: str) -> ToolReceipt:
    return ToolReceipt(name=name, result_kind="error", content="error:not_found", status="failed")


# ─────────────────────────── инв.1 no_false_success ───────────────────────────

def test_inv1_catches_claim_without_effect():
    """Реплика «добавила молоко», но БД не изменилась и нет applied-квитанции → красный."""
    d = _dialog(_turn("Добавила молоко.", tool_calls=["add_shopping_items"]))
    v = inv_no_false_success(d)
    assert v and v[0].severity == "CRITICAL", v


def test_inv1_silent_when_effect_real():
    d = _dialog(_turn("Добавила молоко.", tool_calls=["add_shopping_items"],
                      receipts=[_ok_receipt("add_shopping_items")],
                      after={**_Z, "shopping": 1}))
    assert inv_no_false_success(d) == []


def test_inv1_silent_on_question_infinitive():
    """«Добавить молоко?» — инфинитив-вопрос, не утверждение успеха → не красный."""
    d = _dialog(_turn("Добавить молоко в покупки?", tool_calls=["add_shopping_items"],
                      awaiting_confirm=True))
    assert inv_no_false_success(d) == []


def test_inv1_silent_on_read_done():
    """«Готово, вот список» на ЧТЕНИИ (нет write-инструмента) → не ложный успех."""
    d = _dialog(_turn("Готово, вот твой список.", tool_calls=["get_checklist"]))
    assert inv_no_false_success(d) == []


# ─────────────────────────── инв.2 failed_not_done ───────────────────────────

def test_inv2_catches_success_over_failed_tool():
    d = _dialog(_turn("Готово, добавила.", tool_calls=["add_checklist_items"],
                      receipts=[_err_receipt("add_checklist_items")]))
    v = inv_failed_not_done(d)
    assert v and v[0].severity == "CRITICAL", v


def test_inv2_silent_when_honest_failure():
    d = _dialog(_turn("Не получилось найти список, уточни название.",
                      tool_calls=["add_checklist_items"],
                      receipts=[_err_receipt("add_checklist_items")]))
    assert inv_failed_not_done(d) == []


def test_inv2_catches_mixed_success_and_failure():
    """R3 (ревью sol+terra R2): один вызов ok, другой failed, а реплика «Готово, всё добавила» —
    один успех НЕ должен глушить провал. Красный."""
    d = _dialog(_turn("Готово, всё добавила.", tool_calls=["add_checklist_items", "add_task"],
                      receipts=[_ok_receipt("add_checklist_items"), _err_receipt("add_task")],
                      after={**_Z, "checklist_items": 1}))
    assert inv_failed_not_done(d), "смешанный успех+провал под success-claim не пойман"


def test_inv1_noop_receipt_is_false_success():
    """R3: no-op квитанция (status=noop, напр. ok:updated:0) не считается applied →
    «Обновила» без реального изменения = ложный успех."""
    noop = ToolReceipt(name="update_task", result_kind="ok", content="ok:updated:0", status="noop")
    d = _dialog(_turn("Обновила задачу.", tool_calls=["update_task"], receipts=[noop]))
    assert inv_no_false_success(d), "ложный успех после no-op не пойман"


def test_inv1_catches_plural_claim_forms():
    """R3 (ревью terra R2): множественное число «добавили/создали/внесли/обновили» без эффекта."""
    for reply in ("Добавили молоко.", "Создали список.", "Внесли запись.", "Обновили данные."):
        d = _dialog(_turn(reply, tool_calls=["add_checklist_items"]))
        assert inv_no_false_success(d), f"пропущена plural-форма: {reply!r}"


# ─────────────────────────── инв.4a mutated_not_cancelled (#362) ───────────────────────────

def test_inv4a_catches_false_cancel_on_write():
    """Репро #362: add_task состоялся, а реплика «Отменила, ничего не делаю» → красный."""
    d = _dialog(_turn("Отменила, ничего не делаю.", tool_calls=["add_task"],
                      receipts=[_ok_receipt("add_task")], after={**_Z, "tasks": 1}))
    v = inv_mutated_not_cancelled(d)
    assert v and v[0].severity == "CRITICAL", v


def test_inv4a_silent_when_cancel_and_no_mutation():
    d = _dialog(_turn("Отменила, ничего не делаю."))
    assert inv_mutated_not_cancelled(d) == []


def test_inv4a_silent_when_write_reported_honestly():
    d = _dialog(_turn("Записала сахар 16.", tool_calls=["add_task"],
                      receipts=[_ok_receipt("add_task")], after={**_Z, "tasks": 1}))
    assert inv_mutated_not_cancelled(d) == []


# ─────────────────────────── инв.5 public_no_tech (#390) ───────────────────────────

def test_inv5_catches_pending_done_total_leak():
    """Репро #390: сырой формат инструмента списков в реплике → красный."""
    d = _dialog(_turn("[checklist_ab12cd34ef56] · Кино · 2 pending, 2 done, 4 total"))
    v = inv_public_no_tech(d)
    assert v and v[0].severity == "CRITICAL", v


def test_inv5_silent_on_human_format():
    d = _dialog(_turn("Кино к просмотру — 2 из 4 сделано. Поход — 7 дел, ничего не сделано."))
    assert inv_public_no_tech(d) == []


def test_inv5_latin_user_content_not_flagged():
    """Латиница как контент юзера («iPhone») — НЕ техвыдача (иначе ложные срабатывания)."""
    assert tech_tokens("Купила iPhone и milk.") == []
    d = _dialog(_turn("Купила iPhone."))
    assert inv_public_no_tech(d) == []


def test_inv5_catches_iso_and_snake_case():
    assert "add_task" in tech_tokens("вызвала add_task")
    assert any("2026-07-20" in x for x in tech_tokens("на 2026-07-20"))


# ─────────────────────────── инв.3 idempotency ───────────────────────────

def test_inv3_catches_duplicate_mutation_same_key():
    d = _dialog(
        _turn("Добавила молоко.", after={**_Z, "shopping": 1}),
        _turn("Добавила молоко.", before={**_Z, "shopping": 1}, after={**_Z, "shopping": 2}),
    )
    v = inv_idempotency(d, turns=[0, 1], table="shopping")
    assert v and v[0].severity == "MAJOR", v


def test_inv3_silent_when_second_add_deduped():
    d = _dialog(
        _turn("Добавила молоко.", after={**_Z, "shopping": 1}),
        _turn("Уже есть молоко.", before={**_Z, "shopping": 1}, after={**_Z, "shopping": 1}),
    )
    assert inv_idempotency(d, turns=[0, 1], table="shopping") == []


# ─────────────────────────── инв.4 ambiguous_cancel ───────────────────────────

def test_inv4_catches_mutation_on_ambiguous_cancel():
    d = _dialog(_turn("Убрала.", after={**_Z, "checklists": 1}))
    v = inv_ambiguous_cancel_zero_mutations(d, turns=[0])
    assert v and v[0].severity == "CRITICAL", v


def test_inv4_silent_when_no_mutation():
    d = _dialog(_turn("Уточни, что именно отменить?"))
    assert inv_ambiguous_cancel_zero_mutations(d, turns=[0]) == []


# ─────────── R2 (ревью sol+terra): корпус форм успеха/отмены/техвыдачи ───────────

def test_inv1_catches_passive_claim_forms():
    """Пассив/безличные («список обновлён», «задача создана», «всё внесено») без эффекта."""
    for reply in ("Список обновлён.", "Задача создана.", "Всё внесено.",
                  "Запись сохранена.", "Чек-лист заархивирован."):
        d = _dialog(_turn(reply, tool_calls=["add_checklist_items"]))
        assert inv_no_false_success(d), f"пропущена форма: {reply!r}"


def test_inv1_silent_on_already_applied():
    """already_applied (цель уже в нужном состоянии) — не ложный успех, даже без новой строки."""
    r = ToolReceipt(name="add_shopping_items", result_kind="ok",
                    content="okv2:added_with_dups:...", status="already_applied")
    d = _dialog(_turn("Молоко уже добавила.", tool_calls=["add_shopping_items"], receipts=[r]))
    assert inv_no_false_success(d) == []


def test_inv4a_catches_extra_cancel_forms():
    for reply in ("Ничего не меняла.", "Оставила без изменений.", "Не стала менять."):
        d = _dialog(_turn(reply, tool_calls=["add_task"], receipts=[_ok_receipt("add_task")],
                          after={**_Z, "tasks": 1}))
        assert inv_mutated_not_cancelled(d), f"пропущена форма отмены: {reply!r}"


def test_inv5_catches_protocol_prefixes():
    """Сырые протокол-квитанции в реплике юзеру: ok:archived / error: / not_found:."""
    for reply in ("Готово: ok:archived.", "error: internal ошибка", "not_found: список"):
        d = _dialog(_turn(reply))
        assert inv_public_no_tech(d), f"пропущена протокол-утечка: {reply!r}"


def test_inv5_does_not_overflag_english_list_names():
    """R2: общие сущ. (Shopping/Checklist/Done как ИМЯ) больше не флагаются (меньше ложных красных).
    Но счётчик «done» в формате #390 всё ещё ловится _EN_TECH_RE."""
    assert tech_tokens("Твой список Shopping — 3 дела.") == []
    assert tech_tokens("Список Checklist готов.") == []
    assert "done" in [x.lower() for x in tech_tokens("2 pending, 2 done")]
