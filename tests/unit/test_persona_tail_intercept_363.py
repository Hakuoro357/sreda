"""#363 — хвост-персона перехватывает ПОДТВЕРЖДЕНИЕ записи дневника здоровья (#361).

Прод (tenant_max_142322319, MAX, 11.07): «Трекрезан» / «На ужин котлета и овощной гарнир» →
``save_core_fact`` ОТРАБОТАЛ (данные лежат в ``assistant_memories``), НО финальная реплика = ЭХО
служебного хвоста «Поняла, буду отвечать, опираясь на реальные результаты инструментов…» вместо
подтверждения записи. Это класс #386/#393 (действие свершилось, ответ = служебный хвост), НЕ
молчаливая потеря данных (скан 516 ходов на проде: 0 истинных потерь — каждый write-ход с
мета-ответом реально записал).

Фикс (owner-approved Вариант 1): расширить аллоулист заземления #393 (``react_result_report``) на
инструменты дневника здоровья ``save_core_fact`` / ``save_episode``. Механика — ровно вариант C:
  * ``grounding_note`` (промпт) — ТОЛЬКО серверные факты (успех + тип «в память»), БЕЗ текста факта;
  * имя/текст факта — только в ВЫВОД юзеру: голос называет из tool-результата + детерминированная
    страховка ``fallback_reply`` вставляет из args. Итог: ответ НАЗЫВАЕТ записанное, не эхо-хвост.
  * ISO-дату из контента еды («2026-07-11: ужин — …») в ВЫВОД НЕ пускаем (правило «никаких ISO-дат
    юзеру») — strip префикса даты перед показом.

Границы (owner, узкий scope): ТОЛЬКО write-половина дневника. Чтения (show_checklist эхо) → #386.
Дубли (повтор юзера) → #397. ``create_checklist`` НЕ добавляем (возвращает существующий → неотличимо
от создания → риск ложного успеха; см. ``test_collect_excludes_create_and_generic_393``).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_result_report import (
    collect_successful_writes,
    fallback_reply,
    grounding_note,
    reply_grounds_result,
)


# ─────────────────────────── синтетика ───────────────────────────

def _ai_call(name: str, args: dict, cid: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _tm(content: str, *, name: str, cid: str = "c1", kind: str | None = "ok") -> ToolMessage:
    art = {"result_kind": kind} if kind else None
    return ToolMessage(content=content, name=name, tool_call_id=cid, artifact=art)


_MEM_ID = "mem_" + "a" * 24
# дословный прод-хвост-эхо (мета-ответ вместо подтверждения)
_META_ECHO = "Поняла, буду отвечать, опираясь на реальные результаты инструментов."
_META_ECHO2 = "Поняла, буду отвечать именно так, как ты просишь."


def _core_msgs(content: str = "Трекрезан", result: str | None = None) -> list:
    return [
        HumanMessage(content=content),
        _ai_call("save_core_fact", {"content": content}),
        _tm(result if result is not None else f"saved_core:{_MEM_ID}", name="save_core_fact"),
    ]


def _episode_msgs(summary: str = "сахар вечером 10 и 4", result: str | None = None) -> list:
    return [
        HumanMessage(content=summary),
        _ai_call("save_episode", {"summary": summary}),
        _tm(result if result is not None else f"saved_episode:{_MEM_ID}", name="save_episode"),
    ]


# ─────────────────────────── collect_successful_writes (RED: пока не в аллоулисте) ───────────────────────────

def test_collect_save_core_fact_target_363():
    """«Трекрезан» → успешный target-акт с ОТОБРАЖАЕМЫМ именем факта (из args.content)."""
    acts = collect_successful_writes(_core_msgs("Трекрезан"))
    assert len(acts) == 1
    assert acts[0].tool == "save_core_fact" and acts[0].kind == "target"
    assert acts[0].target == "Трекрезан" and acts[0].count == 1


def test_collect_save_episode_target_363():
    acts = collect_successful_writes(_episode_msgs("сахар вечером 10 и 4"))
    assert len(acts) == 1
    assert acts[0].tool == "save_episode" and acts[0].target == "сахар вечером 10 и 4"


def test_collect_save_core_fact_error_not_grounded_363():
    """МУТ-guard: семантический error-текст (result_kind=ok у ВЫЗВАННОГО инструмента, но контент —
    ошибка) НЕ имеет success-префикса saved_core: → не заземляем (иначе ложный успех)."""
    assert collect_successful_writes(
        _core_msgs("Трекрезан", result="error: некорректное имя категории")) == ()


def test_collect_save_inflight_not_grounded_363():
    """МУТ-guard: IdempotencyInFlight-текст (без success-префикса) → не успех."""
    assert collect_successful_writes(
        _core_msgs("Трекрезан",
                   result="Секунду, эта запись уже в обработке — повтори, если не дошло.")) == ()


def test_collect_save_wrong_prefix_not_grounded_363():
    """МУТ-guard: любой контент без saved_core:/saved_episode: → не заземляем."""
    assert collect_successful_writes(_core_msgs("Трекрезан", result="ok:saved:whatever")) == ()


# ─────────────────────────── детектор: мета-эхо НЕ заземлён, назвавший результат — заземлён ───────────────────────────

def test_detector_save_meta_echo_not_grounded_363():
    """Реальный прод-баг: хвост-эхо «буду отвечать, опираясь…» НЕ называет факт → не заземлён →
    сработает подмена."""
    acts = collect_successful_writes(_core_msgs("Трекрезан"))
    assert reply_grounds_result(_META_ECHO, acts) is False
    assert reply_grounds_result(_META_ECHO2, acts) is False


def test_detector_save_named_grounded_363():
    """НЕГАТИВ-КОНТРОЛЬ (живость #121): голос НАЗВАЛ факт → НЕ подменяем."""
    acts = collect_successful_writes(_core_msgs("Трекрезан"))
    assert reply_grounds_result("Записала Трекрезан в категорию лекарства.", acts) is True


# ─────────────────────────── fallback_reply — называет факт, чистый, без ISO-даты ───────────────────────────

def _assert_clean(txt: str) -> None:
    low = txt.lower()
    assert "—" not in txt and "okv2" not in low and "id=" not in low
    assert not any("a" <= c <= "z" for c in low), f"латиница в выводе: {txt!r}"


def test_fallback_save_names_fact_clean_363():
    fb = fallback_reply(collect_successful_writes(_core_msgs("Трекрезан")))
    assert fb.startswith("Готово") and "Трекрезан" in fb
    _assert_clean(fb)


def test_fallback_save_strips_iso_date_363():
    """Дневник еды: модель пишет контент с ISO-датой («2026-07-11: ужин — котлета…»). В ВЫВОД
    ISO-дату НЕ пускаем (правило «никаких ISO-дат»), но еду НАЗЫВАЕМ."""
    fb = fallback_reply(collect_successful_writes(
        _core_msgs("2026-07-11: ужин — котлета и овощной гарнир")))
    low = fb.lower()
    assert "котлет" in low and "ужин" in low
    assert "2026" not in fb and "07-11" not in fb
    _assert_clean(fb)


def test_fallback_save_latin_degrades_not_leaks_363():
    """Латиница в имени факта (напр. «L-Thyroxin») → имя неотображаемо → грациозная деградация к
    факту («записала»), БЕЗ утечки латиницы (owner R4-c)."""
    fb = fallback_reply(collect_successful_writes(_core_msgs("L-Thyroxin")))
    assert fb.startswith("Готово") and "записала" in fb.lower()
    _assert_clean(fb)


# ─────────────────────────── grounding_note — серверные факты, БЕЗ текста факта (вариант C) ───────────────────────────

def test_grounding_note_save_facts_only_363():
    note = grounding_note(collect_successful_writes(_core_msgs("Трекрезан")))
    assert "успешно выполнено" in note and "память" in note.lower()
    assert "Трекрезан" not in note and "трекрезан" not in note.lower()


# ─────────────────────────── e2e через handle_turn (стаб-инструмент, как #319) ───────────────────────────

class _StubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


def _save_stub(monkeypatch):
    """Стаб save_core_fact (без embedding/repo) — возвращает реальный success-префикс контракта."""
    from langchain_core.tools import StructuredTool

    from sreda.runtime import react_loop

    def _f(content: str = "", category: str | None = None, **kw) -> str:  # noqa: ANN001
        return f"saved_core:{_MEM_ID}"

    tool = StructuredTool.from_function(func=_f, name="save_core_fact",
                                        description="save_core_fact")
    monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [tool])


async def _turn(db_session, u, llm, text):
    from sreda.runtime.react_loop import handle_turn
    return await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=llm, user_text=text,
        inbound_message_id="m1", channel="max")


@pytest.mark.asyncio
async def test_e2e_save_meta_echo_substituted_363(db_session, monkeypatch):
    """P1 repro: успешный save_core_fact + хвост-эхо → финал НАЗЫВАЕТ «Трекрезан», не мета-эхо."""
    from tests.unit.conftest import seed_telegram_user
    u = seed_telegram_user(db_session)
    db_session.commit()
    _save_stub(monkeypatch)
    llm = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "save_core_fact", "args": {"content": "Трекрезан"}, "id": "c1"}]),
        AIMessage(content=_META_ECHO),
    ])
    reply = await _turn(db_session, u, llm, "Трекрезан")
    s = str(reply).lower()
    assert "буду отвечать" not in s, reply
    assert "трекрезан" in s, reply


@pytest.mark.asyncio
async def test_e2e_save_grounded_voice_not_substituted_363(db_session, monkeypatch):
    """Негатив-контроль: голос НАЗВАЛ факт → НЕ подменяем (живость сохранена)."""
    from tests.unit.conftest import seed_telegram_user
    u = seed_telegram_user(db_session)
    db_session.commit()
    _save_stub(monkeypatch)
    llm = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "save_core_fact", "args": {"content": "Трекрезан"}, "id": "c1"}]),
        AIMessage(content="Записала Трекрезан в лекарства. Что-нибудь ещё?"),
    ])
    reply = await _turn(db_session, u, llm, "Трекрезан")
    assert "что-нибудь ещё" in str(reply).lower(), reply
