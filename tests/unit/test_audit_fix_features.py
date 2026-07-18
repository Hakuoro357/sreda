"""Регрессионные тесты фиксов аудита 2026-07-18 (svc-features + cross-security П4).

По находкам отчётов `plans/audit-2026-07-18/svc-features-review.md` и
`cross-security-review.md`:

  * #1 [MAJOR] tasks.py — rollback-guard (FC-1) в update() / legacy-add() /
    attach_reminder: мусорная RRULE от LLM (прошедшая whitelist — BYDAY=XX)
    больше не оставляет частичное состояние, которое «чужой» commit следующего
    write-тула зафиксировал бы при ответе «error».
  * #3 tasks.list_range — TZ-корректное бакетирование RRULE-occurrences
    (локальные сутки юзера, не UTC-дата occurrence).
  * #4 tasks — whitelist-валидация RRULE FREQ/INTERVAL на service-границе
    (CPU-бомба FREQ=SECONDLY в read hot-path).
  * #5 checklists.archive_list — отвязка linked task (паритет с miniapp R-33).
  * #6 checklists.add_items_no_commit — кап длины пункта унифицирован (1000).
  * #7 web_search — фейл DDG-fallback → «временно недоступен», не «лимит».
  * #8 / cross-security П4 — сырые запросы/города/lat-lon убраны из WARNING-логов.
  * #2 [MAJOR] audio_probe.probe_audio_async — async-обёртка (asyncio.to_thread).
  * #10 audio_probe — byte-estimate cap поднят 30s → 300s (undercharge voice-квоты).
  * #11 weather — негативный кэш геокодинга + per-turn storm-cap.
  * #9 onboarding — auto-regrant free-подписки не срабатывает после ЯВНОЙ
    отмены юзером (status="cancelled"); paid→expired fallback сохранён.

Без сети и без PG: sqlite :memory:, сетевые вызовы патчатся.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.checklists import Checklist
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.tasks import Task
from sreda.services.audio_probe import ffprobe_duration, probe_audio_async
from sreda.services.checklists import ChecklistService
from sreda.services.tasks import TaskService


@pytest.fixture
def session():
    """Своя sqlite с НАСТОЯЩИМИ commit'ами — rollback-guard проверяем именно
    на персистентном состоянии (внешняя-TX фикстура db_session не подходит:
    нам нужен сценарий «чужой commit после ошибки»)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# #1 [MAJOR] FC-1 rollback-guard
# ---------------------------------------------------------------------------

# RRULE, проходящая whitelist-валидацию (#4), но падающая в rrulestr —
# именно этот класс мусора держит rollback-guard (валидация не панацея).
_GARBAGE_BUT_WHITELISTED_RRULE = "FREQ=DAILY;BYDAY=XX"


def test_update_garbage_rrule_rolls_back_partial_state(session):
    """update(): cancel(commit=False) — flush всей сессии; rrulestr падает в
    _attach_reminder_inner → guard откатывает → чужой commit ничего не фиксирует."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Разминка",
        scheduled_date=date(2026, 7, 1), time_start=time(9, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
        reminder_offset_minutes=15,
    )
    old_reminder_id = t.reminder_id
    assert old_reminder_id is not None

    with pytest.raises(ValueError):
        svc.update(
            tenant_id="t1", user_id="u1", task_id=t.id,
            time_start=time(10, 0),
            recurrence_rule=_GARBAGE_BUT_WHITELISTED_RRULE,
        )

    # «Чужой write-тул» того же хода коммитит общую сессию.
    session.commit()

    session.expire_all()
    task = session.get(Task, t.id)
    rem = session.get(FamilyReminder, old_reminder_id)
    # Частичное состояние НЕ зафиксировано: старое напоминание живо,
    # ссылка цела, поля задачи не изменились.
    assert task.reminder_id == old_reminder_id
    assert rem.status == "pending"
    assert task.time_start == time(9, 0)
    assert task.recurrence_rule == "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"


def test_legacy_add_garbage_rrule_rolls_back(session):
    """legacy-ветка add(): task flushed → _attach_reminder_inner падает →
    guard откатывает → задача без напоминания не утекает чужим commit'ом."""
    svc = TaskService(session)
    with pytest.raises(ValueError):
        svc.add(
            tenant_id="t1", user_id="u1", title="Задача",
            scheduled_date=date(2026, 7, 1), time_start=time(9, 0),
            recurrence_rule=_GARBAGE_BUT_WHITELISTED_RRULE,
            reminder_offset_minutes=15,
        )

    session.commit()  # «чужой тул»
    assert session.query(Task).count() == 0
    assert session.query(FamilyReminder).count() == 0


def test_attach_reminder_garbage_stored_rrule_rolls_back(session):
    """attach_reminder(): мусорная RRULE в legacy-данных (сохранена до
    валидации) → замена напоминания атомарна: сбой → старое живо."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Встреча",
        scheduled_date=date(2026, 7, 1), time_start=time(9, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
        reminder_offset_minutes=15,
    )
    old_reminder_id = t.reminder_id
    # «Легаси» мусор прямо в БД (до-валидационные данные).
    t.recurrence_rule = _GARBAGE_BUT_WHITELISTED_RRULE
    session.commit()

    with pytest.raises(ValueError):
        svc.attach_reminder(
            tenant_id="t1", user_id="u1", task_id=t.id, offset_minutes=30,
        )

    session.commit()  # «чужой тул»
    session.expire_all()
    task = session.get(Task, t.id)
    rem = session.get(FamilyReminder, old_reminder_id)
    assert task.reminder_id == old_reminder_id
    assert rem.status == "pending"


# ---------------------------------------------------------------------------
# #4 RRULE whitelist-валидация на service-границе
# ---------------------------------------------------------------------------


def test_rrule_secondly_rejected_at_add(session):
    svc = TaskService(session)
    with pytest.raises(ValueError, match="FREQ"):
        svc.add(
            tenant_id="t1", user_id="u1", title="CPU-бомба",
            scheduled_date=date(2026, 7, 1), time_start=time(10, 0),
            recurrence_rule="FREQ=SECONDLY",
        )
    assert session.query(Task).count() == 0  # отказ ДО вставки


def test_rrule_interval_capped(session):
    svc = TaskService(session)
    with pytest.raises(ValueError, match="INTERVAL"):
        svc.add(
            tenant_id="t1", user_id="u1", title="слишком редко",
            scheduled_date=date(2026, 7, 1), time_start=time(10, 0),
            recurrence_rule="FREQ=DAILY;INTERVAL=9999",
        )


def test_rrule_icalendar_prefix_form_accepted(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="нормальная",
        scheduled_date=date(2026, 7, 1), time_start=time(10, 0),
        recurrence_rule="RRULE:FREQ=WEEKLY;BYDAY=MO",
    )
    assert t.recurrence_rule == "RRULE:FREQ=WEEKLY;BYDAY=MO"


def test_update_rejects_garbage_freq_before_mutation(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Задача",
        scheduled_date=date(2026, 7, 1), time_start=time(9, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=6",
    )
    with pytest.raises(ValueError, match="FREQ"):
        svc.update(
            tenant_id="t1", user_id="u1", task_id=t.id,
            recurrence_rule="FREQ=MINUTELY",
        )
    session.expire_all()
    task = session.get(Task, t.id)
    assert task.recurrence_rule == "FREQ=DAILY;BYHOUR=6"  # не изменилось


def test_update_empty_rrule_still_clears_recurrence(session):
    """Контракт #166: recurrence_rule="" сбрасывает повторение — валидация
    не должна его ломать."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Задача",
        scheduled_date=date(2026, 7, 1), time_start=time(9, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=6",
    )
    updated = svc.update(
        tenant_id="t1", user_id="u1", task_id=t.id, recurrence_rule="",
    )
    assert updated.recurrence_rule is None


# ---------------------------------------------------------------------------
# #3 list_range — TZ-корректное бакетирование (локальные сутки юзера)
# ---------------------------------------------------------------------------


def test_list_range_buckets_occurrence_on_local_day(session):
    """Задача «еженедельно в 00:30 МСК» (BYHOUR=21 UTC, суббота 21:30 UTC =
    воскресенье 00:30 МСК). Старое бакетирование по UTC-дате показывало её в
    субботу; новое — в воскресенье (день фактического firing'а)."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Полив цветов",
        scheduled_date=date(2026, 6, 20),  # Saturday
        recurrence_rule="FREQ=WEEKLY;BYHOUR=21;BYMINUTE=30",
    )
    res = svc.list_range(
        tenant_id="t1", user_id="u1",
        from_date=date(2026, 6, 21), to_date=date(2026, 6, 27),
    )
    sunday = date(2026, 6, 21)   # 00:30 МСК — фактический firing
    saturday = date(2026, 6, 27)  # сюда клал старый UTC-бакет (21:30 UTC сб)
    assert any(x.id == t.id for x in res[sunday])
    assert not any(x.id == t.id for x in res[saturday])


def test_list_today_shows_early_msk_task_on_fire_day(session):
    """list_today на день firing'а (00:30 МСК) — задача видна; старый код
    её терял (occurrence предыдущих UTC-суток не попадал в окно)."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Лекарство",
        scheduled_date=date(2026, 6, 20),
        recurrence_rule="FREQ=WEEKLY;BYHOUR=21;BYMINUTE=30",
    )
    today_items = svc.list_today(
        tenant_id="t1", user_id="u1", today=date(2026, 6, 21),
    )
    assert any(x.id == t.id for x in today_items)


# ---------------------------------------------------------------------------
# #5 archive_list отвязывает linked task (паритет miniapp R-33)
# ---------------------------------------------------------------------------


def test_archive_list_unlinks_linked_task(session):
    task_svc = TaskService(session)
    t = task_svc.add(tenant_id="t1", user_id="u1", title="Отпуск")
    cl_svc = ChecklistService(session)
    cl = cl_svc.create_list(tenant_id="t1", user_id="u1", title="Сборы")
    t.checklist_id = cl.id
    session.commit()

    archived = cl_svc.archive_list(tenant_id="t1", user_id="u1", list_id=cl.id)
    assert archived is not None

    session.expire_all()
    task = session.get(Task, t.id)
    assert task.checklist_id is None
    assert session.get(Checklist, cl.id).status == "archived"


# ---------------------------------------------------------------------------
# #6 кап длины пункта унифицирован (1000 в обоих путях)
# ---------------------------------------------------------------------------


def test_add_items_no_commit_cap_unified_1000(session):
    cl_svc = ChecklistService(session)
    cl = cl_svc.create_list(tenant_id="t1", user_id="u1", title="Список")
    long_item = "а" * 500
    created, skipped = cl_svc.add_items_no_commit(list_id=cl.id, items=[long_item])
    session.commit()
    assert skipped == []
    assert len(created) == 1
    assert created[0].title == long_item  # composite-путь раньше резал до 200


# ---------------------------------------------------------------------------
# #7 DDG-фейл → транзиентная недоступность (не «лимит исчерпан»)
# ---------------------------------------------------------------------------


def test_ddg_fallback_failure_is_transient_not_quota():
    from sreda.services.web_search_tool import _ddg_fallback

    with patch(
        "sreda.services.web_search_tool._call_ddg_fallback", return_value=None,
    ):
        msg = _ddg_fallback("запрос", None, None, None)
    assert msg == "error: веб-поиск временно недоступен, попробуйте позже"
    assert "лимит" not in msg


# ---------------------------------------------------------------------------
# #8 / cross-security П4 — PII в WARNING-логах
# ---------------------------------------------------------------------------


def test_tavily_failure_log_has_no_raw_query():
    from sreda.services.web_search_tool import _call_tavily

    with (
        patch("sreda.services.web_search_tool.httpx.post") as mock_post,
        patch("sreda.services.web_search_tool.logger") as mock_log,
    ):
        import httpx

        mock_post.side_effect = httpx.ConnectError("down")
        assert _call_tavily("лекарства по секретному рецепту", "key") is None

    logged = " ".join(str(c) for c in mock_log.warning.call_args_list)
    assert "лекарства" not in logged
    assert "секретному" not in logged


def test_ddg_failure_log_has_no_raw_query():
    from sreda.services.web_search_tool import _call_ddg_fallback

    with (
        patch("duckduckgo_search.DDGS", side_effect=ConnectionError("down")),
        patch("sreda.services.web_search_tool.logger") as mock_log,
    ):
        assert _call_ddg_fallback("адрес моей квартиры") is None

    logged = " ".join(str(c) for c in mock_log.warning.call_args_list)
    assert "квартиры" not in logged


def test_geocode_failure_log_has_no_city(geo_caches_cleared):
    from sreda.services.weather_tool import _geocode

    with (
        patch("sreda.services.weather_tool.httpx.get") as mock_get,
        patch("sreda.services.weather_tool.logger") as mock_log,
    ):
        import httpx

        mock_get.side_effect = httpx.ConnectError("down")
        assert _geocode("Секретград") is None

    logged = " ".join(str(c) for c in mock_log.warning.call_args_list)
    assert "Секретград" not in logged


def test_forecast_failure_log_has_no_latlon():
    from sreda.services.weather_tool import _fetch_forecast

    with (
        patch("sreda.services.weather_tool.httpx.get") as mock_get,
        patch("sreda.services.weather_tool.logger") as mock_log,
    ):
        import httpx

        mock_get.side_effect = httpx.ConnectError("down")
        assert _fetch_forecast(
            lat=55.7558, lon=37.6173,
            request_tz_name="Europe/Moscow",
            granularity="daily", forecast_days=1,
        ) is None

    logged = " ".join(str(c) for c in mock_log.warning.call_args_list)
    assert "55.7" not in logged
    assert "37.6" not in logged


# ---------------------------------------------------------------------------
# #11 weather — негативный кэш геокодинга + per-turn storm-cap
# ---------------------------------------------------------------------------


@pytest.fixture
def geo_caches_cleared():
    from sreda.services.weather_tool import _GEO_CACHE, _GEO_NEG_CACHE

    _GEO_CACHE.clear()
    _GEO_NEG_CACHE.clear()
    yield
    _GEO_CACHE.clear()
    _GEO_NEG_CACHE.clear()


def test_geocode_failure_is_negative_cached(geo_caches_cleared):
    """Второй вызов _geocode для того же города в пределах TTL НЕ идёт в сеть."""
    from sreda.services.weather_tool import _geocode

    with patch("sreda.services.weather_tool.httpx.get") as mock_get:
        import httpx

        mock_get.side_effect = httpx.ConnectError("down")
        assert _geocode("Несуществующий-город-xyz") is None
        assert _geocode("Несуществующий-город-xyz") is None
    assert mock_get.call_count == 1


def test_weather_per_turn_storm_cap():
    from sreda.services.weather_tool import build_weather_tool

    tool = build_weather_tool(per_turn_cap=2)
    with patch("sreda.services.weather_tool._geocode", return_value=None):
        r1 = tool.invoke({"location": "Несуществ"})
        r2 = tool.invoke({"location": "Несуществ"})
        r3 = tool.invoke({"location": "Несуществ"})
    assert r1.startswith("error: не нашла город")
    assert r2.startswith("error: не нашла город")
    assert r3.startswith("error:weather_turn_limit:")


# ---------------------------------------------------------------------------
# #2 [MAJOR] probe_audio_async + #10 byte-estimate cap
# ---------------------------------------------------------------------------


def _mock_subprocess_run(stdout: str):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout.encode("utf-8"), stderr=b"",
    )


def test_probe_audio_async_is_coroutine_and_returns_duration():
    assert asyncio.iscoroutinefunction(probe_audio_async)
    fake_out = json.dumps({"format": {"duration": "12.5"}, "streams": []})
    with patch("subprocess.run", return_value=_mock_subprocess_run(fake_out)):
        result = asyncio.run(probe_audio_async(b"x" * 1000))
    assert result == pytest.approx(12.5)


def test_byte_estimate_cap_raised_to_free_daily_ceiling():
    """#10: голосовое 60s (90_000 байт / 1500 B/s) — старый кап 30s занижал
    квоту вдвое; новый кап = дневной free-лимит 300s."""
    fake_out = json.dumps({"format": {}, "streams": []})
    with patch("subprocess.run", return_value=_mock_subprocess_run(fake_out)):
        assert ffprobe_duration(b"x" * 90_000) == pytest.approx(60.0)
        assert ffprobe_duration(b"x" * 1_500_000) == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# #9 onboarding — нет молчаливого ре-гранта после явной отмены
# ---------------------------------------------------------------------------


@pytest.fixture()
def _seam_sessions_to_db(session, monkeypatch):
    """Стаб seam-сессий на тестовую sqlite (паттерн test_341): onboarding
    открывает tenant_session/privileged_session сам, функционально."""
    from contextlib import contextmanager

    import sreda.db.session as dbs

    @contextmanager
    def _stub(arg):
        yield session

    monkeypatch.setattr(dbs, "privileged_session", _stub)
    monkeypatch.setattr(dbs, "tenant_session", _stub)


def _seed_free_plan(session) -> None:
    session.add(SubscriptionPlan(
        id="plan_free", plan_key="sreda_free",
        feature_key="housewife_assistant",
        title="Среда Free", description="free tier", price_rub=0,
    ))


def _seed_sub(session, *, status: str) -> None:
    now = datetime.now(timezone.utc)
    session.add(TenantSubscription(
        id=f"sub_{status}", tenant_id="t1", plan_id="plan_free",
        feature_key="housewife_assistant", status=status,
        starts_at=now, active_until=None, cancel_at_period_end=True,
        quantity=0, next_cycle_quantity=0, created_at=now, updated_at=now,
    ))


def _active_subs_count(session) -> int:
    return (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == "t1",
            TenantSubscription.status == "active",
        )
        .count()
    )


def test_no_silent_regrant_after_explicit_cancel(session, _seam_sessions_to_db):
    """Юзер ЯВНО отменил подписку (billing cancel → status="cancelled") —
    следующий inbound НЕ пересоздаёт free-подписку молча."""
    from sreda.services.onboarding import _serve_existing_bundle_reads

    _seed_free_plan(session)
    _seed_sub(session, status="cancelled")
    session.commit()

    resolved = SimpleNamespace(
        tenant_id="t1", user_id="u1",
        approved_at=datetime.now(timezone.utc),
    )
    _serve_existing_bundle_reads(resolved)

    session.expire_all()
    assert _active_subs_count(session) == 0  # воля юзера уважена


def test_expired_sub_still_gets_free_fallback(session, _seam_sessions_to_db):
    """paid→expired (status="expired") — желаемый fallback сохранён:
    ре-грант free-подписки происходит."""
    from sreda.services.onboarding import _serve_existing_bundle_reads

    _seed_free_plan(session)
    _seed_sub(session, status="expired")
    session.commit()

    resolved = SimpleNamespace(
        tenant_id="t1", user_id="u1",
        approved_at=datetime.now(timezone.utc),
    )
    _serve_existing_bundle_reads(resolved)

    session.expire_all()
    assert _active_subs_count(session) == 1
