"""#333: Фредди отказывал в повторяющихся напоминаниях («каждый час»), хотя
schedule_reminder умеет RRULE. Red-тесты на два слоя фикса:

1. ``_recurrence_hint(text)`` (react_preflight) - детерминированная директива
   при «кажд»-корне + напоминание-сигнале (паттерн #215: не доверяем промпту,
   ловим по коду - урок #180).
2. Спека schedule_reminder (specs_reminders) - description/examples явно
   говорят про recurrence_rule c HOURLY/DAILY (в проде модель отказала «могу
   только однократное» при 0 tool calls - прод-инцидент 2026-07-10,
   user_tg_755682022; probe: 5 прогонов, 0× FREQ=HOURLY).
"""
from __future__ import annotations

from sreda.runtime.react_preflight import _recurrence_hint
from sreda.services.tool_schemas.specs_reminders import SCHEDULE_REMINDER_SPEC


# ── слой 1: детерминированный хинт ──────────────────────────────────────────

def test_hint_fires_on_incident_phrase_333():
    """Прод-фраза инцидента: «кажд» + «напоминание» → директива."""
    hint = _recurrence_hint(
        "Поставь напоминание с 12:30 каждый час - собрать образцы тканей")
    assert hint is not None
    # Директива обязана: назвать recurrence_rule, дать почасовой пример,
    # запретить ложный отказ (класс честности #279).
    assert "recurrence_rule" in hint
    assert "FREQ=HOURLY" in hint
    assert "однократн" in hint  # «НЕ отказывай ... однократное»


def test_hint_requires_end_clarification_333():
    """Решение владельца 2026-07-10: конец повторения не назван → ОБЯЗАТЕЛЬНО
    уточнить (до какого времени / сколько раз), затем COUNT=/UNTIL=."""
    hint = _recurrence_hint("поставь напоминание каждый час пить воду")
    assert hint is not None
    assert "уточни" in hint
    assert "COUNT=" in hint
    assert "UNTIL=" in hint


def test_hint_fires_on_daily_form_333():
    hint = _recurrence_hint("напомни каждый день в 9 выпить таблетки")
    assert hint is not None
    assert "FREQ=DAILY" in hint


def test_hint_fires_on_weekly_feminine_form_333():
    """«каждую неделю» - женская форма корня «кажд»."""
    assert _recurrence_hint("ставь напоминание каждую пятницу в 16:00") is not None


def test_no_hint_without_recurrence_word_333():
    """Однократное «напомни завтра в 9» - без директивы (анти-шум)."""
    assert _recurrence_hint("напомни завтра в 9 позвонить врачу") is None


def test_no_hint_without_reminder_signal_333():
    """«кажд» без напоминание-сигнала (чек-листы/покупки) - без директивы:
    хинт не должен утаскивать не-reminder ходы в schedule_reminder."""
    assert _recurrence_hint("добавь молоко в каждый список покупок") is None


def test_no_hint_on_empty_and_none_333():
    assert _recurrence_hint("") is None
    assert _recurrence_hint(None) is None


# ── слой 2: спека инструмента ───────────────────────────────────────────────

def test_spec_description_mentions_recurrence_333():
    """Описание, которое видит модель, обязано говорить что повторы ЕСТЬ.
    Ядро: recurrence_rule + образец FREQ=HOURLY + запрет отказа. Полный
    список форм (DAILY/WEEKLY, COUNT/UNTIL) - в директиве _HINT_RECURRENCE
    (спека ужата под кеш-гейт #128, prod-like префикс ≤70500)."""
    desc = SCHEDULE_REMINDER_SPEC.description
    assert "recurrence_rule" in desc
    assert "FREQ=HOURLY" in desc
    assert "отказыва" in desc.lower()  # «НЕ отказывай»


def test_spec_examples_include_hourly_333():
    """Примеры-триггеры содержат почасовой кейс (инцидентная форма)."""
    examples = " ".join(SCHEDULE_REMINDER_SPEC.trigger_examples)
    assert "каждый час" in examples


# ── R1 оба Codex (блокер): bespoke ReAct-инструмент умеет recurrence_rule ────

def _schedule_tool(db_session, u):
    from sreda.runtime.react_loop import build_slice_tools
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)
    return next(t for t in tools if t.name == "schedule_reminder")


def test_bespoke_schedule_reminder_has_recurrence_in_schema_333(db_session):
    """Схема инструмента (то, что видит модель) содержит recurrence_rule -
    до фикса хинт велел передавать аргумент ВНЕ схемы (блокер R1)."""
    from tests.unit.conftest import seed_telegram_user
    u = seed_telegram_user(db_session)
    db_session.commit()
    tool = _schedule_tool(db_session, u)
    assert "recurrence_rule" in tool.args


def test_bespoke_schedule_reminder_persists_rrule_333(db_session):
    """Интеграция (R1 medium M1): вызов с FREQ=HOURLY → строка в БД с recurrence_rule."""
    from tests.unit.conftest import seed_telegram_user
    from sreda.db.models.housewife import FamilyReminder
    u = seed_telegram_user(db_session)
    db_session.commit()
    tool = _schedule_tool(db_session, u)
    res = tool.invoke({"title": "собрать образцы тканей",
                       "trigger_iso": "2030-01-01T12:30:00+03:00",
                       "recurrence_rule": "FREQ=HOURLY"})
    assert str(res).startswith("ok:scheduled:")
    assert "повтор" in str(res)
    row = (db_session.query(FamilyReminder)
           .filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one())
    assert row.recurrence_rule == "FREQ=HOURLY"


def test_bespoke_schedule_reminder_rejects_bad_rrule_333(db_session):
    """Кривое правило → честная ошибка (fail-closed), НЕ молчаливое разовое."""
    from tests.unit.conftest import seed_telegram_user
    from sreda.db.models.housewife import FamilyReminder
    u = seed_telegram_user(db_session)
    db_session.commit()
    tool = _schedule_tool(db_session, u)
    res = tool.invoke({"title": "x", "trigger_iso": "2030-01-01T12:30:00+03:00",
                       "recurrence_rule": "каждый час"})
    assert "Не разобрала правило повтора" in str(res)
    assert db_session.query(FamilyReminder).count() == 0


def test_bespoke_past_start_recurrence_shows_real_time_333(db_session):
    """R2 Claude MAJOR: прошедший старт повтора → в ответе РЕАЛЬНОЕ следующее
    срабатывание (next_trigger_at), не прошедший trigger_at («ложный успех»)."""
    from tests.unit.conftest import seed_telegram_user
    u = seed_telegram_user(db_session)
    db_session.commit()
    tool = _schedule_tool(db_session, u)
    res = str(tool.invoke({"title": "пить воду",
                           "trigger_iso": "2020-01-01T12:30:00+03:00",
                           "recurrence_rule": "FREQ=HOURLY"}))
    assert res.startswith("ok:scheduled:")
    assert "2020" not in res and "января" not in res  # прошедший старт не пересказан


def test_bespoke_exhausted_series_honest_error_333(db_session):
    """R2 Claude MINOR: серия целиком в прошлом → честный текст, не «нужен формат»."""
    from tests.unit.conftest import seed_telegram_user
    from sreda.db.models.housewife import FamilyReminder
    u = seed_telegram_user(db_session)
    db_session.commit()
    tool = _schedule_tool(db_session, u)
    res = str(tool.invoke({"title": "x", "trigger_iso": "2020-01-01T12:30:00+03:00",
                           "recurrence_rule": "FREQ=HOURLY;COUNT=3"}))
    assert "уже в прошлом" in res
    assert "Нужен формат" not in res
    assert db_session.query(FamilyReminder).count() == 0
