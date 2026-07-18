"""Регрессионные тесты на фиксы аудита 2026-07-18 (svc-housewife).

Покрывает:
* MAJOR  — save_recipes_batch: мусорный servings не роняет batch;
           IntegrityError на одном элементе откатывает только его savepoint.
* MAJOR  — plan_week: дедуп слота (day_of_week, meal_type) во входном payload;
           update_item: tolerant к легаси-дублям (лечит вместо MultipleResultsFound).
* MINOR  — reminders.update без embedding_client не затирает embedding;
           schedule: cap title под String(500).
* MINOR  — save_pb_tour_display_name: _extract_short_name + length-cap.
* MINOR  — _guess_category: ключ «бад» (lowercase-match).
* MINOR  — add_members_batch: нет N+1 по family_members (snapshot).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import MenuPlanItem, Recipe
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.services.housewife_family import HousewifeFamilyService
from sreda.services.housewife_menu import HousewifeMenuService
from sreda.services.housewife_onboarding import save_pb_tour_display_name
from sreda.services.housewife_recipes import HousewifeRecipeService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.housewife_shopping import _guess_category


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# MAJOR: save_recipes_batch — tolerant servings
# ---------------------------------------------------------------------------


def test_save_recipes_batch_garbage_servings_falls_back_to_default(session):
    """servings='4 порции' / dict раньше убивали ВЕСЬ batch (ValueError/TypeError
    из середины цикла). Теперь — дефолт 2, остальные рецепты сохраняются."""
    svc = HousewifeRecipeService(session)
    result = svc.save_recipes_batch(
        tenant_id="t1",
        user_id="u1",
        recipes=[
            {"title": "Борщ", "source": "user_dictated", "servings": "4 порции"},
            {"title": "Плов", "source": "user_dictated", "servings": {"n": 4}},
            {"title": "Омлет", "source": "user_dictated", "servings": "3"},
        ],
    )
    assert [r.title for r in result.created] == ["Борщ", "Плов", "Омлет"]
    by_title = {r.title: r.servings for r in result.created}
    assert by_title["Борщ"] == 2  # дефолт вместо падения
    assert by_title["Плов"] == 2
    assert by_title["Омлет"] == 3  # валидная строка по-прежнему парсится
    assert session.query(Recipe).count() == 3


def test_save_recipes_batch_integrity_error_skips_only_bad_item(
    session, monkeypatch
):
    """IntegrityError на flush одного элемента: savepoint откатывает ТОЛЬКО его,
    соседние элементы батча коммитятся, сессия остаётся чистой."""
    svc = HousewifeRecipeService(session)

    orig_flush = session.flush

    def flaky_flush(*args, **kwargs):
        # Autoflush на чтениях тоже проходит через session.flush — считать
        # вызовы нельзя. Падаем только когда во flush реально идёт «Битый».
        for obj in session.new:
            if isinstance(obj, Recipe) and obj.title == "Битый":
                raise IntegrityError(
                    "INSERT INTO recipes", {},
                    Exception("UNIQUE constraint failed"),
                )
        return orig_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", flaky_flush)

    result = svc.save_recipes_batch(
        tenant_id="t1",
        user_id="u1",
        recipes=[
            {"title": "Первый", "source": "user_dictated"},
            {"title": "Битый", "source": "user_dictated"},
            {"title": "Третий", "source": "user_dictated"},
        ],
    )
    assert [r.title for r in result.created] == ["Первый", "Третий"]
    assert result.invalid == ["Битый"]
    # Сессия не отравлена: коммит прошёл, в БД ровно два рецепта.
    assert {r.title for r in session.query(Recipe).all()} == {"Первый", "Третий"}


# ---------------------------------------------------------------------------
# MAJOR: menu — дедуп слота + tolerant update_item
# ---------------------------------------------------------------------------


def test_plan_week_dedups_duplicate_slot_in_payload(session):
    """LLM прислал два обеда на один день — первая ячейка выигрывает,
    в БД одна строка на (day_of_week, meal_type)."""
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1",
        user_id="u1",
        week_start="2026-07-20",
        cells=[
            {"day_of_week": 0, "meal_type": "lunch", "free_text": "суп"},
            {"day_of_week": 0, "meal_type": "lunch", "free_text": "каша"},
        ],
    )
    lunches = [
        i for i in plan.items if i.day_of_week == 0 and i.meal_type == "lunch"
    ]
    assert len(lunches) == 1
    assert lunches[0].free_text == "суп"
    rows = (
        session.query(MenuPlanItem)
        .filter_by(menu_plan_id=plan.id, day_of_week=0, meal_type="lunch")
        .all()
    )
    assert len(rows) == 1


def test_update_item_heals_legacy_duplicate_rows(session):
    """Легаси-дубль слота (в старых БД unique-констрейнта не было): one_or_none()
    бросал MultipleResultsFound. Теперь update_item берёт первую строку и удаляет
    дубли. Симулируем легаси-схему: временно убираем uq-констрейнт из metadata."""
    table = MenuPlanItem.__table__
    uq = next(
        c for c in table.constraints
        if getattr(c, "name", None) == "uq_menu_plan_items_plan_day_meal"
    )
    legacy_engine = create_engine("sqlite:///:memory:")
    table.constraints.discard(uq)  # легаси-БД: констрейнта ещё нет
    try:
        Base.metadata.create_all(legacy_engine)
    finally:
        table.constraints.add(uq)
    legacy_session = sessionmaker(bind=legacy_engine)()
    legacy_session.add(Tenant(id="t1", name="Test"))
    legacy_session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    legacy_session.commit()

    svc = HousewifeMenuService(legacy_session)
    plan = svc.plan_week(
        tenant_id="t1",
        user_id="u1",
        week_start="2026-07-20",
        cells=[{"day_of_week": 1, "meal_type": "dinner", "free_text": "плов"}],
    )
    legacy_session.add(
        MenuPlanItem(
            id="mpi_legacy_dup",
            menu_plan_id=plan.id,
            tenant_id="t1",
            day_of_week=1,
            meal_type="dinner",
            free_text="дубль",
        )
    )
    legacy_session.commit()

    item = svc.update_item(
        tenant_id="t1",
        user_id="u1",
        plan_id=plan.id,
        day_of_week=1,
        meal_type="dinner",
        free_text="новое блюдо",
    )
    assert item is not None
    rows = (
        legacy_session.query(MenuPlanItem)
        .filter_by(menu_plan_id=plan.id, day_of_week=1, meal_type="dinner")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].free_text == "новое блюдо"
    legacy_session.close()


# ---------------------------------------------------------------------------
# MINOR: reminders — embedding не затирается без клиента; cap title
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    def embed_document(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def test_update_title_without_embedding_client_preserves_embedding(session):
    """React-путь создаёт сервис без embedding_client — правка title раньше
    затирала существующий embedding в NULL."""
    svc_with = HousewifeReminderService(session, embedding_client=_FakeEmbedder())
    rem = svc_with.schedule(
        tenant_id="t1",
        user_id="u1",
        title="молоко",
        trigger_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    emb_json_before = rem.embedding_json
    emb_model_before = rem.embedding_model
    assert emb_json_before is not None

    svc_plain = HousewifeReminderService(session)  # без клиента
    updated = svc_plain.update(
        tenant_id="t1", reminder_id=rem.id, title="кефир"
    )
    assert updated is not None
    assert updated.title == "кефир"
    assert updated.embedding_json == emb_json_before
    assert updated.embedding_model == emb_model_before


def test_schedule_caps_title_at_500(session):
    """title > 500 от LLM — тихая обрезка под String(500), не DataError."""
    svc = HousewifeReminderService(session)
    rem = svc.schedule(
        tenant_id="t1",
        user_id="u1",
        title="я" * 600,
        trigger_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert len(rem.title) == 500


# ---------------------------------------------------------------------------
# MINOR: save_pb_tour_display_name — sanitizer + cap
# ---------------------------------------------------------------------------


def test_save_pb_tour_display_name_sanitizes_llm_garbage(session):
    name = save_pb_tour_display_name(
        session,
        tenant_id="t1",
        user_id="u1",
        raw_name="Пользователя зовут Борис.",
    )
    assert name == "Борис"
    profile = UserProfileRepository(session).get_profile("t1", "u1")
    assert profile is not None
    assert profile.display_name == "Борис"


def test_save_pb_tour_display_name_caps_length(session):
    name = save_pb_tour_display_name(
        session,
        tenant_id="t1",
        user_id="u1",
        raw_name="А" * 200,
    )
    assert len(name) <= 30  # cap _extract_short_name (max_chars=30)
    profile = UserProfileRepository(session).get_profile("t1", "u1")
    assert profile.display_name == name


# ---------------------------------------------------------------------------
# MINOR: _guess_category — «бад»
# ---------------------------------------------------------------------------


def test_guess_category_bad_keyword(session):
    assert _guess_category("БАД Эвалар") == "лекарства"
    assert _guess_category("бад для суставов") == "лекарства"
    assert _guess_category("витамины для детей") == "лекарства"


# ---------------------------------------------------------------------------
# MINOR: add_members_batch — нет N+1 SELECT по family_members
# ---------------------------------------------------------------------------


def test_add_members_batch_uses_snapshot_no_nplus1(session):
    engine = session.get_bind()
    selects = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def count_selects(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and (
            "family_members" in statement
        ):
            selects["n"] += 1

    svc = HousewifeFamilyService(session)
    svc.add_member(tenant_id="t1", user_id="u1", name="Маша", role="spouse")
    selects["n"] = 0  # считаем только запросы самого батча

    result = svc.add_members_batch_detailed(
        tenant_id="t1",
        user_id="u1",
        members=[
            {"name": "Маша", "role": "spouse"},   # дубль существующей
            {"name": "Папа", "role": "spouse"},
            {"name": "Сын", "role": "child"},
        ],
    )
    assert [m.name for m in result.created] == ["Папа", "Сын"]
    assert result.duplicates_existing == ["Маша"]
    # Было: 1 (batch) + N (add_member на каждый элемент). Стало: 1 snapshot-SELECT.
    assert selects["n"] == 1
