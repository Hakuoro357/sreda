"""Unit tests for checklists (план 2026-04-25).

Покрывает:
  * ChecklistService — CRUD + fuzzy-резолверы + статусные переходы +
    explicit cascade + counts (для plugin home-card).
  * LLM tools (build_housewife_tools) — все 6 чек-лист тулов
    end-to-end через invoke + проверка форматов.
  * Coercer _coerce_to_list — regression на JSON-строки от LLM
    (save_recipe.tags/ingredients падал ~50 раз/неделю).
  * ReplyButtonService.create_tokens — cross-session commit
    (regression на «Выбор устарел» при кнопке «Может, позже»
    в welcome после approval).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Тестовому набору нужен ключ для EncryptedString. Прод-ключ в .env;
# тут — заведомо безопасный заглушечный.
os.environ.setdefault(
    "SREDA_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
)

from sreda.db.base import Base  # noqa: E402
from sreda.db.models.checklists import (  # noqa: E402
    Checklist,
    ChecklistItem,
)
from sreda.db.models.core import Tenant, User  # noqa: E402
from sreda.services.checklists import ChecklistService  # noqa: E402
from sreda.services.housewife_chat_tools import (  # noqa: E402
    _coerce_to_list,
    build_housewife_tools,
)
from sreda.services.reply_buttons import ReplyButtonService  # noqa: E402


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    s.add(Tenant(id="t1", name="T"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="1"))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# ChecklistService — CRUD
# ---------------------------------------------------------------------------


def test_create_list_minimal(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="План кроя")
    assert cl.id.startswith("checklist_")
    assert cl.title == "План кроя"
    assert cl.status == "active"


def test_create_list_empty_title_raises(session):
    svc = ChecklistService(session)
    with pytest.raises(ValueError):
        svc.create_list(tenant_id="t1", user_id="u1", title="   ")


def test_add_items_filters_empty_and_assigns_position(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    items = svc.add_items(
        list_id=cl.id,
        items=["a", "", "  ", "b", "c"],
    )
    assert len(items) == 3  # пустые отфильтрованы
    assert [i.position for i in items] == [0, 1, 2]
    # next batch — позиции продолжаются
    items2 = svc.add_items(list_id=cl.id, items=["d"])
    assert items2[0].position == 3


def test_mark_done_sets_status_and_timestamp(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    [item] = svc.add_items(list_id=cl.id, items=["A"])
    done = svc.mark_done(item_id=item.id)
    assert done.status == "done"
    assert done.done_at is not None


def test_undo_done_restores_pending(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    [item] = svc.add_items(list_id=cl.id, items=["A"])
    svc.mark_done(item_id=item.id)
    restored = svc.undo_done(item_id=item.id)
    assert restored.status == "pending"
    assert restored.done_at is None


# ---------------------------------------------------------------------------
# ChecklistService — fuzzy resolvers (LLM-friendly)
# ---------------------------------------------------------------------------


def test_find_list_by_title_substring(session):
    svc = ChecklistService(session)
    svc.create_list(tenant_id="t1", user_id="u1", title="План кроя на эту неделю")
    found = svc.find_list_by_title(
        tenant_id="t1", user_id="u1", needle="план кроя",
    )
    assert found is not None and "План кроя" in found.title


def test_find_list_by_title_exact_id(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    found = svc.find_list_by_title(
        tenant_id="t1", user_id="u1", needle=cl.id,
    )
    assert found and found.id == cl.id


def test_find_list_by_title_empty_returns_none(session):
    svc = ChecklistService(session)
    assert svc.find_list_by_title(
        tenant_id="t1", user_id="u1", needle="",
    ) is None


def test_find_list_by_title_other_tenant_invisible(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="Plan")
    found = svc.find_list_by_title(
        tenant_id="other", user_id="u1", needle=cl.id,
    )
    assert found is None  # cross-tenant safe


def test_find_item_by_title_pending_priority(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    [a, b, c] = svc.add_items(list_id=cl.id, items=[
        "Lavanda", "Champagne", "Lime",
    ])
    svc.mark_done(item_id=a.id)
    # «лаванда» pending нет — найдёт даже done (fallback)
    found = svc.find_item_by_title(list_id=cl.id, needle="lavanda")
    assert found and found.id == a.id


# ---------------------------------------------------------------------------
# ChecklistService — counts (для home-card в plugin)
# ---------------------------------------------------------------------------


def test_counts_after_done_and_archive(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    [a, b, c] = svc.add_items(list_id=cl.id, items=["a", "b", "c"])
    assert svc.count_open_items(tenant_id="t1", user_id="u1") == 3
    assert svc.count_active_lists(tenant_id="t1", user_id="u1") == 1

    svc.mark_done(item_id=a.id)
    assert svc.count_open_items(tenant_id="t1", user_id="u1") == 2

    svc.archive_list(tenant_id="t1", user_id="u1", list_id=cl.id)
    # Архивированные не учитываются ни в открытых, ни в активных
    assert svc.count_open_items(tenant_id="t1", user_id="u1") == 0
    assert svc.count_active_lists(tenant_id="t1", user_id="u1") == 0


def test_list_summary_returns_p_d_total(session):
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    items = svc.add_items(list_id=cl.id, items=["a", "b", "c", "d"])
    svc.mark_done(item_id=items[0].id)
    svc.mark_done(item_id=items[1].id)
    p, d, t = svc.list_summary(list_id=cl.id)
    assert (p, d, t) == (2, 2, 4)


# ---------------------------------------------------------------------------
# ChecklistService — explicit cascade
# ---------------------------------------------------------------------------


def test_delete_list_cascades_items(session):
    """SQLite по умолчанию игнорирует FK CASCADE — сервис делает
    explicit DELETE items перед DELETE checklist."""
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    svc.add_items(list_id=cl.id, items=["a", "b"])
    ok = svc.delete_list(tenant_id="t1", user_id="u1", list_id=cl.id)
    assert ok
    assert session.query(ChecklistItem).filter_by(checklist_id=cl.id).count() == 0
    assert session.query(Checklist).filter_by(id=cl.id).count() == 0


# ---------------------------------------------------------------------------
# LLM tools — end-to-end через build_housewife_tools
# ---------------------------------------------------------------------------


@pytest.fixture
def tools(session):
    return {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id="u1",
        )
    }


def test_tool_create_checklist_returns_id(tools):
    r = tools["create_checklist"].invoke({"title": "Plan kroya"})
    assert r.startswith("ok:created:checklist_")


def test_tool_add_items_implicit_create(tools):
    """Если list_id_or_title не найден — тул создаёт новый список."""
    r = tools["add_checklist_items"].invoke({
        "list_id_or_title": "Новый список",
        "items": ["a", "b"],
    })
    assert r.startswith("ok:added:2:list=checklist_")


def test_tool_add_items_resolves_existing_by_title(tools):
    tools["create_checklist"].invoke({"title": "План кроя"})
    r = tools["add_checklist_items"].invoke({
        "list_id_or_title": "план кроя",  # case-insensitive substring
        "items": ["x"],
    })
    assert r.startswith("ok:added:1:list=checklist_")


def test_tool_show_checklist_marks(tools, session):
    tools["create_checklist"].invoke({"title": "X"})
    tools["add_checklist_items"].invoke({
        "list_id_or_title": "X",
        "items": ["a", "b"],
    })
    tools["mark_checklist_item_done"].invoke({
        "list_id_or_title": "X",
        "item_title_match": "a",
    })
    r = tools["show_checklist"].invoke({"list_id_or_title": "X"})
    # Должно быть ☑ для done, ☐ для pending
    assert "\u2611" in r and "\u2610" in r


def test_tool_show_checklist_not_found(tools):
    r = tools["show_checklist"].invoke({"list_id_or_title": "несуществующее"})
    assert r.startswith("error: not_found")


def test_service_delete_item_removes_row(session):
    """Сервисный delete_item — hard delete, отличается от cancel."""
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="X")
    [a, b] = svc.add_items(list_id=cl.id, items=["a", "b"])
    ok = svc.delete_item(item_id=a.id)
    assert ok
    assert svc.delete_item(item_id="missing") is False
    remaining = [i.title for i in svc.list_items(list_id=cl.id)]
    assert remaining == ["b"]


def test_tool_delete_checklist_item_happy(tools, session):
    """Regression: 2026-04-25 — швея надиктовала список, бот неправильно
    расслышал «Собрать кучу глины» дважды. Юзер попросил удалить
    неверный пункт. Раньше LLM отвечала «к сожалению не могу» —
    теперь должна вызвать delete_checklist_item."""
    tools["create_checklist"].invoke({"title": "Стройка"})
    tools["add_checklist_items"].invoke({
        "list_id_or_title": "Стройка",
        "items": ["Покрасить дом", "Чинить забор", "Сделать забор",
                  "Собрать кучу глины", "Разобрать кучу глины"],
    })
    r = tools["delete_checklist_item"].invoke({
        "list_id_or_title": "Стройка",
        "item_title_match": "Собрать кучу глины",
    })
    assert r.startswith("ok:deleted:clitem_"), r

    # Проверим что осталось 4 пункта без «Собрать кучу глины».
    show = tools["show_checklist"].invoke({"list_id_or_title": "Стройка"})
    assert "Собрать кучу глины" not in show
    assert "Разобрать кучу глины" in show
    assert "Чинить забор" in show


def test_tool_delete_checklist_item_not_found(tools):
    tools["create_checklist"].invoke({"title": "X"})
    r = tools["delete_checklist_item"].invoke({
        "list_id_or_title": "X",
        "item_title_match": "несуществующее",
    })
    assert r.startswith("error: item_not_found")


def test_tool_archive_checklist(tools):
    tools["create_checklist"].invoke({"title": "X"})
    r = tools["archive_checklist"].invoke({"list_id_or_title": "X"})
    assert r.startswith("ok:archived:checklist_")


def test_tool_list_checklists_format(tools):
    tools["create_checklist"].invoke({"title": "A"})
    tools["create_checklist"].invoke({"title": "B"})
    r = tools["list_checklists"].invoke({})
    lines = r.split("\n")
    assert len(lines) == 2
    assert all("0 pending, 0 done, 0 total" in line for line in lines)


def test_tool_list_checklists_empty(tools):
    r = tools["list_checklists"].invoke({})
    assert r == "no checklists"


# ---------------------------------------------------------------------------
# Regression — coercer (save_recipe.tags JSON-string)
# ---------------------------------------------------------------------------


def test_coerce_str_json_to_list():
    assert _coerce_to_list('["a","b"]') == ["a", "b"]
    assert _coerce_to_list('[]') == []


def test_coerce_passes_through_native_list():
    assert _coerce_to_list(["a"]) == ["a"]


def test_coerce_none():
    assert _coerce_to_list(None) is None


def test_coerce_empty_str_becomes_none():
    assert _coerce_to_list("") is None


def test_coerce_bad_json_passes_through():
    """Если LLM прислала строку которая НЕ JSON-list — отдаём как есть.
    pydantic потом сам отдаст ValidationError, и LLM поправится."""
    assert _coerce_to_list("just a string") == "just a string"


def test_save_recipe_with_str_tags_does_not_fail(tools, session):
    """Regression: 50 фейлов/неделю на проде из-за строки в tags."""
    r = tools["save_recipe"].invoke({
        "title": "Borsh",
        "ingredients": '[{"title":"svekla","quantity_text":"2 sht"}]',
        "instructions_md": "Step 1",
        "servings": 4,
        "source": "user_dictated",
        "tags": '["sup","obed"]',
    })
    assert r.startswith("ok:saved:rec_"), f"unexpected: {r}"


# ---------------------------------------------------------------------------
# Regression — reply_buttons cross-session (welcome «Может, позже»)
# ---------------------------------------------------------------------------


def test_reply_button_token_survives_session_close(session_factory):
    """Воспроизводит баг 2026-04-25: admin_tenant_approve создавал
    токены через ReplyButtonService.create_tokens (только flush, без
    commit). FastAPI закрывал session → токены rollback'ались →
    callback handler на «Может, позже» получал None → toast «Выбор
    устарел». Фикс: create_tokens теперь commit'ит сразу.
    """
    s_a = session_factory()
    s_a.add(Tenant(id="t1", name="T"))
    s_a.add(User(id="u1", tenant_id="t1", telegram_account_id="1"))
    s_a.commit()

    svc_a = ReplyButtonService(s_a)
    pairs = svc_a.create_tokens(
        tenant_id="t1", user_id="u1",
        labels=["Может, позже", "Расскажу про семью"],
    )
    s_a.close()  # имитация закрытия FastAPI dependency

    s_b = session_factory()
    svc_b = ReplyButtonService(s_b)
    label = svc_b.resolve_token(
        tenant_id="t1", user_id="u1", token=pairs[0][0],
    )
    assert label == "Может, позже"
    s_b.close()
