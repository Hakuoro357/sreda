"""#187 Phase 2b — A3 re-check ПОД advisory-локом (delete-first гонка).

КОНТЕКСТ (adversarial-приёмка #187, спец-дрейф A3): план «дверь #6» требовал
ДВУХ вещей внутри хода — (1) advisory-lock на весь ход И (2) **re-check**
``is_tenant_active`` СРАЗУ ПОСЛЕ захвата лока. Отгрузили сперва только лок;
``test_187_phase2b_barrier`` проверял лишь наличие подстроки ``tenant_advisory_lock``
через ``inspect.getsource`` → НЕ ловил отсутствие re-check. Этот файл — OUTCOME-тест
самого re-check: delete-first сценарий, где тенант АКТИВЕН на ingress (ход уже
заспавнен), но УДАЛЁН к моменту захвата лока в ходе → ход обязан АБОРТИТЬ.

ПОЧЕМУ SQLite достаточно: re-check читает durable ``deleted_at`` свежим SELECT'ом
(``is_tenant_active``); advisory-лок на SQLite — корректный no-op (см. модульный
докстринг ``test_187_phase2b_barrier``). Реальная cross-process сериализация —
свойство PG-runtime, тут НЕ проверяется. Проверяется ровно гейт re-check'а.

НЕ-ВАКУУМНОСТЬ (🔴): тот же вызов с ``is_tenant_active``→True (мок) ПРОХОДИТ
re-check и доходит до обработки (TG → ``handle_telegram_interaction`` вызван +
``processing_started`` выставлен; MAX → ``dispatch_max_action`` вызван). Это
доказывает, что re-check нагружен: убери его — удалённый тенант мутировал бы.
Нейтрализация фикса (удалить ``if not is_tenant_active(...): return``) → красный.

Харнесс заимствован из ``test_187_phase1_doors`` (``fresh_db`` on-disk SQLite +
``seed_telegram_user`` / ``_seed_max_user``).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.core import Tenant
from sreda.db.session import get_engine, get_session_factory
from tests.unit.conftest import seed_telegram_user

EXISTING_TG_CHAT_ID = "100000003"
EXISTING_MAX_ACCOUNT_ID = "40921122"
EXISTING_MAX_CHAT_ID = "320955459"


# ---- Fixtures (mirror of test_187_phase1_doors.fresh_db) ----------------


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """Empty on-disk SQLite DB wired through the same caches prod reads.

    Both turn functions call ``get_session_factory()`` internally, so a real
    on-disk DB is enough — the seeded tenant + its ``deleted_at`` are visible
    to the fresh ``bg_session`` the turn opens.
    """
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef"
    ).decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "max-token")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _seed_max_user(
    session,
    *,
    tenant_id: str,
    max_account_id: str = EXISTING_MAX_ACCOUNT_ID,
    max_chat_id: str = EXISTING_MAX_CHAT_ID,
) -> None:
    """Seed a MAX-channel Tenant + Workspace + User (active)."""
    from sreda.db.models.core import User, Workspace

    session.add(
        Tenant(id=tenant_id, name="Max Tenant", approved_at=datetime.now(timezone.utc))
    )
    session.add(Workspace(id=f"ws_{tenant_id}", tenant_id=tenant_id, name="Home"))
    session.add(
        User(
            id=f"user_{tenant_id}",
            tenant_id=tenant_id,
            max_account_id=max_account_id,
            max_chat_id=max_chat_id,
        )
    )


def _mark_deleted(session, tenant_id: str) -> None:
    """soft_delete (delete-first): flag durably committed BEFORE the turn runs."""
    tenant = session.get(Tenant, tenant_id)
    tenant.deleted_at = datetime.now(timezone.utc)
    session.commit()


def _tg_onboarding(*, tenant_id: str) -> SimpleNamespace:
    """Minimal TelegramOnboardingResult-shaped stub the turn body reads.

    Only the attributes the turn touches before/at the re-check are set.
    ``is_new_user=False`` + tenant NOT in ``react_loop_enabled_tenants`` (empty
    by default) → on the active path the turn takes the legacy branch and calls
    ``handle_telegram_interaction`` (which we spy), not the ReAct loop.
    """
    return SimpleNamespace(
        is_new_user=False,
        chat_id=EXISTING_TG_CHAT_ID,
        tenant_id=tenant_id,
        workspace_id=f"ws_{tenant_id}",
        user_id=f"user_{tenant_id}",
        assistant_id=None,
    )


def _max_onboarding(*, tenant_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        is_new_user=False,
        max_account_id=EXISTING_MAX_ACCOUNT_ID,
        max_chat_id=EXISTING_MAX_CHAT_ID,
        tenant_id=tenant_id,
        workspace_id=f"ws_{tenant_id}",
        user_id=f"user_{tenant_id}",
        assistant_id=None,
    )


def _tg_text_payload(text: str = "привет") -> dict:
    return {
        "update_id": 7101,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_TG_CHAT_ID), "type": "private"},
            "text": text,
        },
    }


def _max_text_payload(text: str = "привет") -> dict:
    return {
        "update_type": "message_created",
        "timestamp": 1777907183208,
        "message": {
            "recipient": {"chat_id": int(EXISTING_MAX_CHAT_ID), "chat_type": "dialog"},
            "body": {"mid": "mid.test", "seq": 1, "text": text},
            "sender": {
                "user_id": int(EXISTING_MAX_ACCOUNT_ID),
                "first_name": "X",
                "name": "X",
                "is_bot": False,
            },
        },
    }


# ---- TG: A3 re-check aborts the delete-first turn -----------------------


@pytest.mark.asyncio
async def test_tg_recheck_aborts_deleted_turn(fresh_db, monkeypatch):
    """A3 (TG) ABORT. Тенант помечен удалённым ДО хода (delete-first); вызов
    ``_process_approved_turn_locked`` напрямую → re-check под локом срабатывает,
    ход возвращается рано: ``processing_started`` НЕ выставлен И оркестратор
    (``handle_telegram_interaction``) НЕ вызван (ноль доменных мутаций)."""
    from sreda.services import telegram_inbound as ti

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        seeded = seed_telegram_user(
            session, chat_id=EXISTING_TG_CHAT_ID, tenant_id="tg_race",
            workspace_id="ws_tg_race", user_id="user_tg_race",
        )
        session.commit()
        _mark_deleted(session, seeded.tenant_id)  # delete успел ПЕРВЫМ

    # Spy на гейт обработки + оркестратор. Любой их вызов на удалённом = провал.
    status_spy = MagicMock()
    monkeypatch.setattr(ti, "_set_processing_status", status_spy)
    interaction_spy = AsyncMock(
        side_effect=AssertionError("orchestrator must not run for deleted tenant")
    )
    monkeypatch.setattr(ti, "handle_telegram_interaction", interaction_spy)
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: MagicMock())

    await ti._process_approved_turn_locked(
        bot_key="sreda",
        payload=_tg_text_payload(),
        onboarding=_tg_onboarding(tenant_id="tg_race"),
        inbound_message_id="inbound_tg_race",
    )

    interaction_spy.assert_not_called()
    # processing_started НЕ должен быть выставлен (re-check стоит ДО него в TG).
    started_calls = [
        c for c in status_spy.call_args_list if "processing_started" in c.args
    ]
    assert started_calls == [], (
        f"re-check must abort BEFORE processing_started; got {status_spy.call_args_list}"
    )
    # Codex MINOR: aborted-deleted ход обязан пометить inbound терминально ('ignored'),
    # иначе монитор unprocessed_inbound (NOT IN processed/ignored) ложно сочтёт зависшим.
    ignored_calls = [c for c in status_spy.call_args_list if "ignored" in c.args]
    assert ignored_calls, (
        f"aborted turn must mark inbound 'ignored' (terminal); got {status_spy.call_args_list}"
    )


@pytest.mark.asyncio
async def test_tg_recheck_active_tenant_proceeds(fresh_db, monkeypatch):
    """A3 (TG) НЕ-ВАКУУМНОСТЬ. Тот же вход, но тенант АКТИВЕН (мок
    ``is_tenant_active``→True) → re-check пропускает, ход доходит до обработки:
    ``processing_started`` выставлен И ``handle_telegram_interaction`` вызван.
    Без re-check'а ход вёл бы себя так же на УДАЛЁННОМ тенанте — что и есть баг."""
    from sreda.services import telegram_inbound as ti
    from sreda.services import tenant_lifecycle

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        seeded = seed_telegram_user(
            session, chat_id=EXISTING_TG_CHAT_ID, tenant_id="tg_race",
            workspace_id="ws_tg_race", user_id="user_tg_race",
        )
        session.commit()
        _mark_deleted(session, seeded.tenant_id)  # реально удалён в БД

    # Мокаем re-check → True: имитируем «тенант ещё активен» — единственное
    # отличие от abort-теста. Локальный импорт в ходе резолвит атрибут из
    # модуля tenant_lifecycle в момент вызова → патчим там.
    monkeypatch.setattr(tenant_lifecycle, "is_tenant_active", lambda *a, **k: True)

    status_spy = MagicMock()
    monkeypatch.setattr(ti, "_set_processing_status", status_spy)
    interaction_spy = AsyncMock()
    monkeypatch.setattr(ti, "handle_telegram_interaction", interaction_spy)
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: MagicMock())

    await ti._process_approved_turn_locked(
        bot_key="sreda",
        payload=_tg_text_payload(),
        onboarding=_tg_onboarding(tenant_id="tg_race"),
        inbound_message_id="inbound_tg_race",
    )

    interaction_spy.assert_called_once()
    started_calls = [
        c for c in status_spy.call_args_list if "processing_started" in c.args
    ]
    assert started_calls, (
        "active tenant must pass re-check and reach processing_started "
        f"(re-check is load-bearing); got {status_spy.call_args_list}"
    )


# ---- MAX: A3 re-check aborts the delete-first turn ----------------------
#
# В MAX ``processing_started`` выставляется ДО захвата лока/re-check (зеркало
# не идеальное), поэтому здесь сигнал аборта — что НИЧЕГО ПОСЛЕ re-check не
# выполнилось: ни STT (``_maybe_transcribe_max_voice``), ни оркестратор
# (``dispatch_max_action``), и финальный ``processed`` не выставлен.


@pytest.mark.asyncio
async def test_max_recheck_aborts_deleted_turn(fresh_db, monkeypatch):
    """A3 (MAX) ABORT. Тенант удалён ДО хода; ``_process_approved_max_turn``
    напрямую → re-check под advisory-локом срабатывает: ни ``dispatch_max_action``,
    ни STT не вызваны, ``processed`` не выставлен (ход вернулся рано)."""
    from sreda.runtime import dispatcher
    from sreda.services import max_inbound as mi

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_max_user(session, tenant_id="max_race")
        session.commit()
        _mark_deleted(session, "max_race")  # delete успел ПЕРВЫМ

    dispatch_spy = MagicMock(
        side_effect=AssertionError("dispatch must not run for deleted tenant")
    )
    monkeypatch.setattr(dispatcher, "dispatch_max_action", dispatch_spy)
    stt_spy = AsyncMock(
        side_effect=AssertionError("STT must not run for deleted tenant")
    )
    monkeypatch.setattr(mi, "_maybe_transcribe_max_voice", stt_spy)
    status_spy = MagicMock()
    monkeypatch.setattr(mi, "_set_processing_status", status_spy)
    monkeypatch.setattr(mi, "MaxClient", lambda *a, **k: MagicMock())

    await mi._process_approved_max_turn(
        bot_key="sreda",
        payload=_max_text_payload(),
        onboarding=_max_onboarding(tenant_id="max_race"),
        inbound_message_id="inbound_max_race",
    )

    dispatch_spy.assert_not_called()
    stt_spy.assert_not_called()
    # Финальный 'processed' НЕ должен выставляться — ход не дошёл до конца.
    processed_calls = [
        c for c in status_spy.call_args_list if "processed" in c.args
    ]
    assert processed_calls == [], (
        f"aborted turn must not mark 'processed'; got {status_spy.call_args_list}"
    )
    # Codex MINOR (зеркало TG): MAX выставил 'processing_started' до лока → abort
    # обязан перекрыть на терминальный 'ignored', иначе монитор сочтёт ход зависшим.
    ignored_calls = [c for c in status_spy.call_args_list if "ignored" in c.args]
    assert ignored_calls, (
        f"aborted MAX turn must mark inbound 'ignored' (terminal); got {status_spy.call_args_list}"
    )


@pytest.mark.asyncio
async def test_max_recheck_active_tenant_proceeds(fresh_db, monkeypatch):
    """A3 (MAX) НЕ-ВАКУУМНОСТЬ. Тот же вход, но ``is_tenant_active``→True →
    re-check пропускает, ход доходит до оркестратора: ``dispatch_max_action``
    вызван. (``dispatch`` замокан вернуть None → ход чисто завершается 'ignored',
    без реального runtime/LLM.) Доказывает, что re-check нагружен."""
    from sreda.runtime import dispatcher
    from sreda.services import max_inbound as mi
    from sreda.services import tenant_lifecycle

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_max_user(session, tenant_id="max_race")
        session.commit()
        _mark_deleted(session, "max_race")  # реально удалён в БД

    monkeypatch.setattr(tenant_lifecycle, "is_tenant_active", lambda *a, **k: True)

    # dispatch → None: re-check пройден, оркестратор вызван, ход уходит в
    # 'ignored' без запуска runtime. STT тоже не нужен для text-пути, но
    # пусть будет no-op-spy на случай если код дойдёт до него.
    dispatch_spy = MagicMock(return_value=None)
    monkeypatch.setattr(dispatcher, "dispatch_max_action", dispatch_spy)
    stt_spy = AsyncMock(side_effect=lambda payload, **k: payload)
    monkeypatch.setattr(mi, "_maybe_transcribe_max_voice", stt_spy)
    monkeypatch.setattr(mi, "_set_processing_status", MagicMock())
    monkeypatch.setattr(mi, "MaxClient", lambda *a, **k: MagicMock())

    await mi._process_approved_max_turn(
        bot_key="sreda",
        payload=_max_text_payload(),
        onboarding=_max_onboarding(tenant_id="max_race"),
        inbound_message_id="inbound_max_race",
    )

    dispatch_spy.assert_called_once()
