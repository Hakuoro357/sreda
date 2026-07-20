"""#392: добавление пунктов в чек-лист на едином пути — БЕЗ candidate-confirm.

Тот же паттерн, что #389 (диктовка порциями), но для семьи чек-листов. Владелец
отметил живьём 20.07: «Ещё добавь уголь» в список → лишнее «Подтверждаешь?».
Продолжение диктовки («Ещё добавь уголь») не несёт императив+домен-слово в ОДНОМ
сообщении → compute_unified_policy даёт allowed_write=∅ → add_checklist_items биндился
КАНДИДАТОМ под generic-confirm на каждом пункте.

Фикс (#392): add_checklist_items — аддитивная, видимая, обратимая операция → прямой
бинд при ОТСУТСТВИИ конкурирующего write-домена роутера (aw ⊆ write-доменов
инструмента = {checklists}). Тот же ПРОВЕРЕННЫЙ механизм #389: реестр
`_UNIFIED_AUTOEXEC_WRITE_TOOLS` + owner-allowlist + гейт конкурирующего домена
`not (aw - tool_write_domains(name))` + import-time гвард. Деструктив чек-листов
(delete_checklist_item/archive_checklist) исключение НЕ получает — остаётся confirm.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from sreda.runtime.react_loop import (
    _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST,
    _UNIFIED_AUTOEXEC_WRITE_TOOLS,
    _apply_unified_policy,
    handle_turn,
)
from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import route_domains
from sreda.services.checklists import ChecklistService
from tests.unit.conftest import seed_telegram_user


def _tool(name):
    def _f(q: str = "", **kw):
        return f"{name}-done"
    return StructuredTool.from_function(func=_f, name=name, description=name)


class _StubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


def _policy(text):
    return compute_unified_policy(text, route_domains(text))


# ─────────── прод-репро (root cause #392): продолжение диктовки в чек-лист ───────────

def test_continuation_dictation_direct_add_392():
    """«Ещё добавь уголь» (владелец 20.07): ни домен-слова, ни явного «список» →
    гейт write промахивается (allowed_write=∅), НО add_checklist_items обязан
    биндиться ПРЯМЫМ (identity), а не кандидатом под confirm."""
    pol = _policy("Ещё добавь уголь")
    assert "checklists" not in pol["allowed_write"]  # причина: гейт не даёт checklists
    t = _tool("add_checklist_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert out == [t]  # identity: без confirm-обёртки


def test_full_checklist_phrase_still_direct_392():
    """Полная фраза «добавь пункт в список» шла прямым ярусом (а) до фикса —
    регрессия: остаётся прямой (роутер дал write=checklists)."""
    pol = _policy("добавь пункт в список")
    assert "checklists" in pol["allowed_write"]  # штатный ярус (а)
    t = _tool("add_checklist_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert out == [t]


# ─────────── границы: деструктив чек-листов НЕ трогаем ───────────

def test_checklist_destructive_still_candidates_392():
    """Деструктив/правки чек-листов исключение #392 НЕ получают: при allowed_write=∅ —
    кандидаты под confirm (чеклист приёмки #392: delete остаётся confirm)."""
    for name in ("delete_checklist_item", "mark_checklist_item_done",
                 "update_checklist_item"):
        t = _tool(name)
        out = _apply_unified_policy([t], ["checklists", "web"], [])
        assert len(out) == 1 and out[0] is not t, name  # обёрнут кандидатом
        assert out[0].name == name


def test_archive_checklist_not_in_autoexec_392():
    """archive_checklist (soft-delete списка) — деструктив: НЕ в autoexec-реестре;
    при allowed_write=∅ — кандидат под confirm (плюс собственный беспоук-confirm на
    этапе сборки тулов)."""
    assert "archive_checklist" not in _UNIFIED_AUTOEXEC_WRITE_TOOLS
    assert "delete_checklist_item" not in _UNIFIED_AUTOEXEC_WRITE_TOOLS
    t = _tool("archive_checklist")
    out = _apply_unified_policy([t], ["checklists", "web"], [])
    assert len(out) == 1 and out[0] is not t


# ─────────── конкурирующий write-домен роутера (гейт #389, зеркало для чек-листов) ───────────

def test_competing_router_write_domain_restores_confirm_392():
    """Роутер дал ПОЛОЖИТЕЛЬНЫЙ write-грант ДРУГОГО домена (shopping): «добавь молоко
    в список покупок». Прямой autoexec add_checklist_items тут молча писал бы в
    checklists вопреки роутеру → кандидат под confirm примиряет расхождение."""
    txt = "добавь молоко в список покупок"
    pol = _policy(txt)
    assert "shopping" in pol["allowed_write"]        # конкурирующий грант роутера
    assert "checklists" not in pol["allowed_write"]
    t = _tool("add_checklist_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert len(out) == 1 and out[0] is not t         # confirm-backstop восстановлен
    assert out[0].name == "add_checklist_items"


def test_competing_write_domain_synthetic_matrix_392():
    """Матрица условия autoexec для add_checklist_items: aw=∅ → прямой (прод-репро);
    aw={checklists} → прямой; aw={shopping} → кандидат (конкурирующий домен)."""
    t = _tool("add_checklist_items")
    out_empty = _apply_unified_policy([t], ["web"], [])
    assert out_empty == [t]
    out_own = _apply_unified_policy([t], ["checklists", "web"], ["checklists"])
    assert out_own == [t]
    out_comp = _apply_unified_policy([t], ["shopping", "web"], ["shopping"])
    assert len(out_comp) == 1 and out_comp[0] is not t


# ─────────── реестр + owner-allowlist (двухключевой гвард #389) ───────────

def test_add_checklist_items_in_registry_and_allowlist_392():
    """#392 — add_checklist_items в ОБОИХ ключах (реестр + owner-allowlist);
    расширение реестра требует одновременной правки allowlist (осознанное owner-решение)."""
    assert "add_checklist_items" in _UNIFIED_AUTOEXEC_WRITE_TOOLS
    assert "add_checklist_items" in _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST
    # #389 не потерян
    assert "add_shopping_items" in _UNIFIED_AUTOEXEC_WRITE_TOOLS


def test_registry_guard_accepts_add_checklist_items_392():
    """Import-time гвард (#389) валидирует новый член: манифест + write-класс + add_* +
    owner-allowlist. Текущий реестр (с add_checklist_items) валиден."""
    from sreda.runtime.react_loop import _validate_unified_autoexec_registry

    _validate_unified_autoexec_registry()  # не бросает
    _validate_unified_autoexec_registry(frozenset({"add_checklist_items"}))  # член ок в изоляции


# ─────────── e2e через handle_turn (самопроверка «проверяю ТЕКСТ ответа», #376) ───────────

@pytest.mark.asyncio
async def test_e2e_continuation_add_no_confirm_392(db_session):
    """Прод-репро #392 end-to-end: продолжение диктовки «Ещё добавь уголь» (aw=∅) →
    add_checklist_items исполняется ПРЯМО, БЕЗ confirm-паузы, ответ называет добавленное.
    До фикса тут была пауза «Подтверждаешь?» (владелец 20.07)."""
    u = seed_telegram_user(db_session)
    ChecklistService(db_session).create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Дача")
    db_session.commit()
    llm = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["уголь"]}, "id": "c1"}]),
        AIMessage(content="Готово."),
    ])
    res = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=llm, user_text="Ещё добавь уголь",
        inbound_message_id="m1", channel="max")
    assert getattr(res, "awaiting_confirm", False) is not True, str(res)  # НЕТ паузы
    assert "угол" in str(res).lower(), res                                # ответ назвал добавленное
