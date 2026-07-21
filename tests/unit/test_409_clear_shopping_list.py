# -*- coding: utf-8 -*-
"""#409 — инструмент `clear_shopping_list` (очистка ВСЕГО списка покупок одним действием).

Прод-факт (замер серией 21.07): «очисти список покупок» — 2 прогона из 5 модель НЕ очищает
(перечисляет содержимое / «не могу одним действием» / переспрашивает). Причина механическая, не
«модель глупая»: инструмента очистки всего списка НЕТ, очистка требует ДВУХ шагов
(`list_shopping` → собрать id → `remove_shopping_items(item_ids=[...])`), и часть прогонов сдаётся
на середине. Фикс — дать ОДИН инструмент без аргументов поверх готового сервисного `clear_pending`.

Контракты, которые фиксируют эти тесты:
  * `ok:cleared:N`; pending → cancelled, bought НЕ трогается (у `clear_bought_shopping` своя роль);
  * пустой список → ЧЕСТНЫЙ исход `ok:cleared:0` (успех, НЕ error — ответ «список уже пуст»);
  * подтверждение: СТАТИЧНАЯ фраза + РОВНО ОДИН interrupt (механизм #405), мутация только после «да»;
  * заземление #393: ответ НАЗЫВАЕТ результат (N позиций), N=0/error НЕ заземляются;
  * изоляция тенанта/юзера.
"""
from __future__ import annotations

from uuid import uuid4

import pytest


# ═══════════════════════════ 1. Реестры / манифест ═══════════════════════════


def test_tool_in_family_manifest_as_shopping_409() -> None:
    """Инструмент обязан быть в манифесте семей — иначе ReAct-фильтр набора
    (`TOOL_FAMILY_MANIFEST.get(name) not in _EXTRA_FAMILIES`) его к Фредди не пропустит."""
    from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST

    assert TOOL_FAMILY_MANIFEST.get("clear_shopping_list") == "shopping"


def test_tool_op_class_is_write_409() -> None:
    """write-класс: иначе `_apply_unified_policy` fail-closed отбросит его как неизвестный
    (и заземление #393 не увидит акт — `collect_successful_writes` гейтит по TOOL_OP_CLASS)."""
    from sreda.services.tool_schemas.families import TOOL_OP_CLASS

    assert TOOL_OP_CLASS.get("clear_shopping_list") == "write"


def test_tool_is_react_only_without_planner_wiring_409() -> None:
    """Инструмент ReAct-only (канон #210, прецедент #262b create_memory_category): спеки,
    парсера и презентера для ЗАМОРОЖЕННОГО plan-execute пути нет НАМЕРЕННО. Полный комплект
    раздувает prod-like префикс планировщика за headroom-гейт #128 (замерено: 71116 > 70500)
    и требует презентер для мёртвого пути. Пин-тест на это решение — чтобы «дополнить до
    полного комплекта» не сделали не глядя."""
    from sreda.services.composer.presenters import _CONFIRM_PHRASES
    from sreda.services.tool_schemas.families import REACT_ONLY_TOOLS
    from sreda.services.tool_schemas.housewife import PARSERS
    from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS, MIGRATED_TOOL_SPECS

    assert "clear_shopping_list" in REACT_ONLY_TOOLS
    for registry, label in ((ALL_TOOL_SPECS, "ALL_TOOL_SPECS"),
                            (MIGRATED_TOOL_SPECS, "MIGRATED_TOOL_SPECS")):
        assert "clear_shopping_list" not in {s.name for s in registry}, (
            f"спека вернулась в {label} — она двигает prod-like префикс планировщика; "
            f"порог держит test_planner_prompt_builder::test_cached_prefix_headroom_gate_prod_like"
        )
    assert "clear_shopping_list" not in PARSERS
    assert "clear_shopping_list" not in {t for (t, _s) in _CONFIRM_PHRASES}


def test_tool_takes_no_arguments_409(db_session) -> None:
    """Без аргументов — ключевое свойство фикса: модели не нужно сперва собирать id
    через list_shopping (именно на этом шаге прогоны срывались)."""
    u, _ = _seed(db_session, [])
    schema = _tool(db_session, u).args_schema
    fields = getattr(schema, "model_fields", {}) if schema is not None else {}
    assert not fields, f"инструмент должен быть без аргументов, а есть: {sorted(fields)}"


def test_react_description_covers_owner_phrases_409() -> None:
    """Фразы владельца из issue — в описании, которое видит Фредди. Триггеры живут ЗДЕСЬ,
    а не в ToolSpec.trigger_examples: последние кормят замороженный планировщик."""
    from sreda.runtime.react_loop import _REACT_TOOL_DESC

    desc = (_REACT_TOOL_DESC.get("clear_shopping_list") or "").lower()
    for phrase in ("очисти список покупок", "удали все покупки",
                   "очисти весь список", "убери всё из покупок"):
        assert phrase in desc, f"нет триггер-фразы {phrase!r} в описании для Фредди"


def test_react_description_is_unambiguous_single_way_409() -> None:
    """ЛОАД-БЕАРИНГ: Фредди видит `_REACT_TOOL_DESC`, а НЕ ToolSpec.description (та кормит
    задепрекейченный планировщик). Описание обязано снимать ровно ту неоднозначность, из-за
    которой модель сдавалась: (а) это ЕДИНСТВЕННЫЙ способ очистить весь список; (б) id не нужны;
    (в) remove_shopping_items — для отдельных позиций."""
    from sreda.runtime.react_loop import _REACT_TOOL_DESC

    desc = _REACT_TOOL_DESC.get("clear_shopping_list")
    assert desc, "нет короткого описания для Фредди — модель увидит docstring, фикс ослаблен"
    low = desc.lower()
    assert "единственный" in low, "описание не заявляет себя единственным способом очистки"
    assert "remove_shopping_items" in desc, "нет разграничения с поштучным удалением"
    assert "id" in low, "не сказано, что id не нужны (модель пойдёт за list_shopping)"


def test_clear_bought_no_longer_tells_model_to_re_ask_409() -> None:
    """Заметка «На "очисти весь список" сначала уточни» снята — она велела ПЕРЕСПРАШИВАТЬ
    вместо действия. Имя clear_shopping_list сюда НЕ пишем: mutex_notes кормят замороженный
    планировщик, а инструмент ReAct-only → гейт registry_quality справедливо ругается на
    «уводит планировщик к недоступному типизированному инструменту». Формулировка
    интент-уровня."""
    from sreda.services.tool_schemas.specs_shopping import CLEAR_BOUGHT_SHOPPING_SPEC

    notes = " ".join(CLEAR_BOUGHT_SHOPPING_SPEC.mutex_notes)
    assert "уточни" not in notes.lower(), "осталась инструкция переспрашивать вместо действия"
    assert "clear_shopping_list" not in notes, \
        "имя ReAct-only инструмента в planner-заметке уводит планировщик к недоступному тулу"


def test_clear_bought_react_description_points_to_new_tool_409() -> None:
    """А вот ЗДЕСЬ перенаправление обязано быть: `_REACT_TOOL_DESC` — то, что реально читает
    Фредди. Иначе, встретив «очисти весь список», он остаётся при clear_bought_shopping."""
    from sreda.runtime.react_loop import _REACT_TOOL_DESC

    assert "clear_shopping_list" in (_REACT_TOOL_DESC.get("clear_bought_shopping") or ""), \
        "описание clear_bought_shopping для Фредди не указывает на очистку всего списка"
    assert "clear_shopping_list" in (_REACT_TOOL_DESC.get("remove_shopping_items") or ""), \
        "описание remove_shopping_items для Фредди не указывает на очистку всего списка"


def test_registered_in_write_intent_validator_409() -> None:
    """HOUSEWIFE_MUTATING_TOOL_NAMES — РАНТАЙМНЫЙ набор (его импортируют handlers/executors),
    не тестовый. Мутирующий инструмент вне него обошёл бы write-intent-проверку."""
    from sreda.services.write_intent_validator import HOUSEWIFE_MUTATING_TOOL_NAMES

    assert "clear_shopping_list" in HOUSEWIFE_MUTATING_TOOL_NAMES


# ═══════════════════════════ 2. Поведение инструмента ═══════════════════════════


def _seed(db_session, statuses):
    """Наполнить список покупок: [(title, status), ...] → вернуть (user, {title: id})."""
    from datetime import datetime, timezone

    from sreda.db.models.housewife_food import ShoppingListItem
    from tests.unit.conftest import seed_telegram_user

    u = seed_telegram_user(db_session)
    db_session.commit()
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    ids = {}
    for title, status in statuses:
        iid = f"sh_{uuid4().hex[:18]}"
        ids[title] = iid
        db_session.add(ShoppingListItem(
            id=iid, tenant_id=u.tenant_id, user_id=u.user_id, title=title,
            category="другое", status=status, added_at=now, updated_at=now))
    db_session.commit()
    return u, ids


def _tool(db_session, u, name="clear_shopping_list"):
    from sreda.services.housewife_chat_tools import build_housewife_tools
    from sreda.services.embeddings import get_embeddings_client

    tools = {t.name: t for t in build_housewife_tools(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        pending_buttons_state=None, menu_display_state=None,
        embedding_client=get_embeddings_client())}
    assert name in tools, f"{name} не зарегистрирован в build_housewife_tools"
    return tools[name]


def test_clears_pending_and_reports_count_409(db_session) -> None:
    from sreda.db.models.housewife_food import ShoppingListItem

    u, ids = _seed(db_session, [("молоко", "pending"), ("хлеб", "pending"), ("сыр", "pending")])
    out = _tool(db_session, u).invoke({})
    assert out == "ok:cleared:3", f"ожидался ok:cleared:3, получено {out!r}"
    db_session.expire_all()
    for title in ("молоко", "хлеб", "сыр"):
        assert db_session.get(ShoppingListItem, ids[title]).status == "cancelled"


def test_does_not_touch_bought_items_409(db_session) -> None:
    """Решение зафиксировано: чистим ТОЛЬКО pending. Купленное — история, у неё свой
    инструмент (clear_bought_shopping). Иначе «очисти список» тихо стирал бы историю."""
    from sreda.db.models.housewife_food import ShoppingListItem

    u, ids = _seed(db_session, [("молоко", "pending"), ("гречка", "bought"),
                                ("старое", "cancelled")])
    assert _tool(db_session, u).invoke({}) == "ok:cleared:1"
    db_session.expire_all()
    assert db_session.get(ShoppingListItem, ids["гречка"]).status == "bought", \
        "купленное НЕ должно затрагиваться очисткой списка"
    assert db_session.get(ShoppingListItem, ids["старое"]).status == "cancelled"


def test_empty_list_is_honest_success_not_error_409(db_session) -> None:
    """Пустой список — НЕ ошибка: исход должен позволить ответить «список уже пуст».
    `error:` увёл бы модель в извинения/переспрос — ровно тот класс, что чиним."""
    u, _ = _seed(db_session, [("гречка", "bought")])
    out = _tool(db_session, u).invoke({})
    assert out == "ok:cleared:0", f"пустой pending → ok:cleared:0, получено {out!r}"
    assert not out.startswith("error"), "пустой список не должен быть ошибкой"


def test_tenant_and_user_isolation_409(db_session) -> None:
    """Чужие строки того же тенанта/другого тенанта очистка НЕ трогает."""
    from datetime import datetime, timezone

    from sreda.db.models.housewife_food import ShoppingListItem

    u, ids = _seed(db_session, [("моё", "pending")])
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    other_user = f"sh_{uuid4().hex[:18]}"
    other_tenant = f"sh_{uuid4().hex[:18]}"
    db_session.add(ShoppingListItem(
        id=other_user, tenant_id=u.tenant_id, user_id="user_other_409", title="чужое-в-тенанте",
        category="другое", status="pending", added_at=now, updated_at=now))
    db_session.add(ShoppingListItem(
        id=other_tenant, tenant_id="tenant_other_409", user_id=u.user_id, title="чужой-тенант",
        category="другое", status="pending", added_at=now, updated_at=now))
    db_session.commit()

    assert _tool(db_session, u).invoke({}) == "ok:cleared:1"
    db_session.expire_all()
    assert db_session.get(ShoppingListItem, other_user).status == "pending"
    assert db_session.get(ShoppingListItem, other_tenant).status == "pending"
    assert db_session.get(ShoppingListItem, ids["моё"]).status == "cancelled"


def test_has_write_guard_prefix_409(db_session) -> None:
    """Мутирующий инструмент обязан нести anti-confab WRITE_GUARD (R-30)."""
    u, _ = _seed(db_session, [])
    assert _tool(db_session, u).description.startswith("[WRITE-TOOL]")


# ═══════════════════════════ 3. Подтверждение (деструктив, #405) ═══════════════════════════


def test_confirm_phrase_is_static_and_about_everything_409() -> None:
    """«Очистить всё» остаётся СТАТИЧНОЙ фразой (как clear_menu/clear_bought_shopping) — честно
    про всё; динамическая с именами врала бы про объём (перечислила бы часть)."""
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    assert _CONFIRM_PHRASE.get("clear_shopping_list") == "очищу весь список покупок"
    resolved = _confirm_phrase("clear_shopping_list", session=None, tenant_id="t", user_id="u")
    assert not callable(resolved), "фраза должна быть статичной, не динамическим резолвером"
    assert resolved == "очищу весь список покупок"


def test_tier_b_passes_bespoke_marked_tool_as_is_409() -> None:
    """Ярус (б) единого пути: обёрнутый _confirm_wrap инструмент несёт маркер и НЕ получает
    второй generic-confirm (иначе двойное подтверждение — прод-баг #405)."""
    from langchain_core.tools import StructuredTool

    from sreda.runtime.react_loop import _apply_unified_policy, _confirm_wrap

    inner = StructuredTool.from_function(
        func=lambda **kw: "ok:cleared:0", name="clear_shopping_list", description="x")
    wrapped = _confirm_wrap(inner, "очищу весь список покупок")
    assert (getattr(wrapped, "metadata", None) or {}).get("sreda_bespoke_confirm") is True
    out = _apply_unified_policy([wrapped], allowed_read=["web"], allowed_write=[])
    assert out == [wrapped], "ярус (б) должен вернуть как есть (identity), иначе двойной confirm"


def test_not_in_autoexec_registry_409() -> None:
    """Очистка деструктивна → НИКОГДА не в autoexec (#389/#392): всегда через подтверждение."""
    from sreda.runtime.react_loop import (
        _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST,
        _UNIFIED_AUTOEXEC_WRITE_TOOLS,
    )

    assert "clear_shopping_list" not in _UNIFIED_AUTOEXEC_WRITE_TOOLS
    assert "clear_shopping_list" not in _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST


@pytest.mark.parametrize("allowed_write", [[], ["shopping"]], ids=["tier_b", "tier_a"])
def test_real_tool_single_interrupt_and_mutation_only_after_yes_409(
        db_session, monkeypatch, allowed_write) -> None:
    """Интеграционно на РЕАЛЬНОМ инструменте из build_slice_tools через РЕАЛЬНУЮ политику
    (образец: test_real_inline_destructives_single_interrupt_tier_b_405): РОВНО ОДИН
    фактический interrupt() в ОБОИХ ярусах; «да» → очистка, «нет» → ноль мутаций."""
    from sreda.db.models.housewife_food import ShoppingListItem
    from sreda.runtime import react_loop
    from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
    from sreda.runtime.react_loop import _apply_unified_policy, build_slice_tools

    counter = {"n": 0, "reply": "да"}

    def _fake_interrupt(*a, **k):
        counter["n"] += 1
        return counter["reply"]

    # module-state → monkeypatch (не прямое присваивание)
    monkeypatch.setattr(react_loop, "interrupt", _fake_interrupt)

    u, ids = _seed(db_session, [("молоко", "pending"), ("хлеб", "pending")])
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    assert "clear_shopping_list" in tools, "инструмент не попал в ReAct-набор"

    out = _apply_unified_policy([tools["clear_shopping_list"]],
                                allowed_read=["web", "shopping"], allowed_write=allowed_write)
    assert len(out) == 1
    tool = out[0]
    assert tool is tools["clear_shopping_list"], \
        "политика вернула ДРУГОЙ объект → добавлен второй generic-confirm (двойное подтверждение)"

    def _invoke():
        counter["n"] = 0
        ctx = ToolRuntimeContext(
            operation_id=f"op_{uuid4().hex[:8]}", execution_id="e", step_id="s",
            tool_name="clear_shopping_list", tenant_id=u.tenant_id, user_id=u.user_id,
            turn_key=f"tk_{uuid4().hex[:8]}")
        with bind_tool_runtime(ctx):
            return tool.invoke({})

    # «нет» → ровно один interrupt, НОЛЬ мутаций
    counter["reply"] = "нет"
    _invoke()
    assert counter["n"] == 1, f"ожидался РОВНО один interrupt, было {counter['n']}"
    db_session.expire_all()
    for title in ("молоко", "хлеб"):
        assert db_session.get(ShoppingListItem, ids[title]).status == "pending", \
            "после «нет» список обязан остаться нетронутым"

    # «да» → ровно один interrupt, очистка состоялась
    counter["reply"] = "да"
    result = _invoke()
    assert counter["n"] == 1, f"ожидался РОВНО один interrupt, было {counter['n']}"
    assert result == "ok:cleared:2", f"после «да» ожидался ok:cleared:2, получено {result!r}"
    db_session.expire_all()
    for title in ("молоко", "хлеб"):
        assert db_session.get(ShoppingListItem, ids[title]).status == "cancelled"


# ═══════════════════════════ 4. Заземление ответа (#393) ═══════════════════════════


def _messages(tool_output, result_kind="ok"):
    """Ход: HumanMessage → AIMessage(tool_call) → ToolMessage(результат)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    return [
        HumanMessage(content="очисти список покупок"),
        AIMessage(content="", tool_calls=[
            {"name": "clear_shopping_list", "args": {}, "id": "call_409"}]),
        ToolMessage(content=tool_output, tool_call_id="call_409",
                    artifact={"result_kind": result_kind}),
    ]


def test_grounding_collects_successful_clear_409() -> None:
    """Успешная очистка — заземляемый акт с СЕРВЕРНЫМ количеством (не 1, не из аргументов)."""
    from sreda.runtime.react_result_report import collect_successful_writes

    acts = collect_successful_writes(_messages("ok:cleared:5"))
    assert len(acts) == 1
    assert acts[0].tool == "clear_shopping_list"
    assert acts[0].count == 5, "количество обязано браться из результата инструмента"


def test_grounding_skips_empty_and_error_409() -> None:
    """Без ложного успеха: N=0 (нечего было чистить) и error НЕ заземляются."""
    from sreda.runtime.react_result_report import collect_successful_writes

    assert collect_successful_writes(_messages("ok:cleared:0")) == ()
    assert collect_successful_writes(_messages("error: internal", result_kind="error")) == ()


def test_fallback_reply_names_the_result_409() -> None:
    """Страховка называет результат («убрала весь список покупок - N позиций»), а не «Готово.»."""
    from sreda.runtime.react_result_report import collect_successful_writes, fallback_reply

    acts = collect_successful_writes(_messages("ok:cleared:5"))
    reply = fallback_reply(acts)
    assert "покупок" in reply and "5" in reply, f"страховка не называет результат: {reply!r}"
    assert "—" not in reply, "длинное тире в тексте пользователю запрещено"


def test_generic_filler_is_not_grounded_409() -> None:
    """Ровно тот прод-класс, что чиним (#393/#376): «Готово.» после успешной очистки
    НЕ считается заземлённым → финализация подменит страховкой."""
    from sreda.runtime.react_result_report import collect_successful_writes, reply_grounds_result

    acts = collect_successful_writes(_messages("ok:cleared:5"))
    assert reply_grounds_result("Готово.", acts) is False
    assert reply_grounds_result("Хорошо, приняла к сведению.", acts) is False


def test_receipt_is_deterministic_and_names_result_409() -> None:
    """R2: раз свободный текст не судим, юзер обязан получить КВИТАНЦИЮ, которая называет
    результат («убрала весь список покупок - 5 позиций»), а не «Готово». Это и есть цель #393
    для этого действия — сузили правило #121 осознанно, но НЕ ценой безымянного ответа."""
    from sreda.runtime.react_result_report import collect_successful_writes, fallback_reply

    acts = collect_successful_writes(_messages("ok:cleared:5"))
    reply = fallback_reply(acts)
    assert "покупок" in reply and "5" in reply, f"квитанция не называет результат: {reply!r}"
    assert "позиц" in reply, "квитанция не называет единицы"


@pytest.mark.parametrize("reply", [
    "Напоминание поставила на 5 минут.",          # R1: постороннее число
    "В списке покупок 5 позиций.",                 # R2 sol: доменный якорь без факта действия
    "Напоминание о покупках поставила на 5 минут.",  # R2 terra: и число, и «покупки», но не очистка
    "Убрала 5 позиций из списка.",                  # R2: корректная реплика без слова «покупки»
    "Готово.",
    "Хорошо, приняла к сведению.",
])
def test_bulk_reply_is_never_judged_by_free_text_409(reply) -> None:
    """R2 (Codex sol+terra, оба: «M1 закрыт неверно»): свободный текст модели для bulk-деструктива
    НЕ судим ВООБЩЕ. Любой текстовой критерий давал либо ложный положительный («в списке покупок
    5 позиций» ≠ отчёт об очистке), либо ложный отрицательный («убрала 5 позиций из списка» —
    корректно, но без слова-якоря). Всегда «не заземлено» → финализация ставит детерминированную
    квитанцию из СТРУКТУРНОГО исхода."""
    from sreda.runtime.react_result_report import collect_successful_writes, reply_grounds_result

    acts = collect_successful_writes(_messages("ok:cleared:5"))
    assert acts, "успешный bulk-акт должен собираться"
    assert reply_grounds_result(reply, acts) is False, \
        f"свободный текст зачтён как отчёт об очистке: {reply!r}"


def test_empty_outcome_gets_honest_deterministic_reply_409() -> None:
    """R1/R2 MAJOR M2 (sol+terra, оба ОТКЛОНИЛИ довод «вне scope»): при ok:cleared:0 набор
    успешных актов ПУСТ → страховка #393 раньше не включалась вовсе, и «убрала весь список
    покупок» после пустого списка уходило юзеру. Теперь исход честный и детерминированный."""
    from sreda.runtime.react_result_report import bulk_outcome_reply, collect_bulk_outcomes

    outcomes = collect_bulk_outcomes(_messages("ok:cleared:0"))
    assert [o.kind for o in outcomes] == ["empty"]
    reply = bulk_outcome_reply(outcomes)
    assert "пуст" in reply.lower(), f"пустой список должен читаться как «уже пуст»: {reply!r}"
    assert "убрала" not in reply.lower(), "ложный успех: нечего было убирать"


def test_error_outcome_gets_honest_deterministic_reply_409() -> None:
    """Тот же класс: после `error:` нельзя отрапортовать успех."""
    from sreda.runtime.react_result_report import bulk_outcome_reply, collect_bulk_outcomes

    outcomes = collect_bulk_outcomes(_messages("error: internal", result_kind="error"))
    assert [o.kind for o in outcomes] == ["error"]
    reply = bulk_outcome_reply(outcomes).lower()
    assert "не получилось" in reply
    assert "убрала" not in reply, "ложный успех после ошибки"


def test_declined_confirm_is_not_an_outcome_409() -> None:
    """Отказ от подтверждения («Хорошо, не трогаю.») НЕ должен читаться как исход — иначе юзер
    получил бы «не получилось очистить» там, где он сам отказался."""
    from sreda.runtime.react_result_report import collect_bulk_outcomes, collect_successful_writes

    msgs = _messages("Хорошо, не трогаю.")
    assert collect_bulk_outcomes(msgs) == ()
    assert collect_successful_writes(msgs) == ()


def test_successful_clear_is_not_double_reported_409() -> None:
    """N>0 покрыт успешным актом (fallback_reply) — в non-success исходы он попадать НЕ должен,
    иначе ход отчитается дважды."""
    from sreda.runtime.react_result_report import collect_bulk_outcomes

    assert collect_bulk_outcomes(_messages("ok:cleared:5")) == ()


def test_bare_tool_name_scrubbed_from_live_reply_409() -> None:
    """R1/R2 MINOR (sol+terra, оба: «на ЖИВОМ ReAct-пути голое имя не ловится»): `_postformat`
    не использует ни `_TOOL_NAMES_SET` (легаси), ни `_KNOWN_TOOL_NAMES` (снимает лишь `name(...)`).
    Жёсткое правило проекта: пользователь не видит никаких технических данных."""
    from sreda.runtime.react_loop import _postformat

    for raw in ("clear_shopping_list убрала весь список покупок - 5 позиций",
                "Вызвала clear_shopping_list(), готово",
                "remove_shopping_items убрала молоко"):
        out = _postformat(raw)
        assert "clear_shopping_list" not in out and "remove_shopping_items" not in out, \
            f"имя инструмента утекло пользователю: {out!r}"


def test_not_exposed_to_legacy_path_without_confirm_409(db_session) -> None:
    """R1 (Codex sol+terra CRITICAL, независимо оба) + R2 sol MINOR («проверяй ПОВЕДЕНЧЕСКИ, не
    поиском строки в исходнике»): у ЛЕГАСИ-пути нет механизма подтверждения — `_invoke_one_tool`
    зовёт `tool.invoke()` напрямую. ReAct-only деструктив там = массовая мутация БЕЗ «да» при
    откате флага / тенанте вне react_loop_enabled_tenants. Проверяем на РЕАЛЬНОМ наборе."""
    from sreda.runtime.handlers import (
        _LEGACY_REACT_ONLY_GRANDFATHERED,
        filter_tools_for_legacy_path,
    )
    from sreda.services.tool_schemas.families import REACT_ONLY_TOOLS

    u, _ = _seed(db_session, [])
    from sreda.services.embeddings import get_embeddings_client
    from sreda.services.housewife_chat_tools import build_housewife_tools

    built = build_housewife_tools(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        pending_buttons_state={}, menu_display_state={},
        embedding_client=get_embeddings_client())
    assert "clear_shopping_list" in {t.name for t in built}, "фабрика инструмент не отдала"

    visible = {t.name for t in filter_tools_for_legacy_path(built)}
    assert "clear_shopping_list" not in visible, \
        "деструктив виден легаси-пути, где НЕТ подтверждения → мутация без «да»"
    assert "get_checklist" not in visible, "#213: get_checklist не должен вернуться на легаси"
    # grandfather сохранён байт-в-байт (поведение чужих фич не менялось)
    for name in _LEGACY_REACT_ONLY_GRANDFATHERED & {t.name for t in built}:
        assert name in visible, f"{name} экспонировался легаси до #409 — поведение менять не должны"
    # гейт по РЕЕСТРУ: любой НЕ-grandfathered ReAct-only инструмент исключён
    for name in (REACT_ONLY_TOOLS - _LEGACY_REACT_ONLY_GRANDFATHERED):
        assert name not in visible, f"{name}: ReAct-only просочился на легаси-путь"


def test_tool_name_in_leak_guards_409() -> None:
    """R1 (sol MINOR): имя инструмента в реестрах скрабберов легаси-пути и llm-слоя."""
    from sreda.runtime.handlers import _TOOL_NAMES_SET
    from sreda.services.llm import _KNOWN_TOOL_NAMES

    assert "clear_shopping_list" in _TOOL_NAMES_SET
    assert "clear_shopping_list" in _KNOWN_TOOL_NAMES


def test_grounding_note_carries_server_facts_409() -> None:
    """Часть 1 (#393): в промпт уходят ТОЛЬКО серверные факты (статус+количество+тип), без имён."""
    from sreda.runtime.react_result_report import collect_successful_writes, grounding_note

    note = grounding_note(collect_successful_writes(_messages("ok:cleared:5")))
    assert "5" in note and "покуп" in note.lower()
