"""#187 Phase 5 — A16: RuntimeError из резолва ≠ «удалён» (error-path).

Чеклист приёмки #187, пункт A16:
  «RuntimeError ≠ удалён (error-path). Given find_user_by_chat_id/resolve бросает
  RuntimeError (соль ``SREDA_TG_ACCOUNT_SALT`` не сконфигурена), when резолв на
  входе, then трактуется как „не резолвится", НЕ как „удалён"; deleted/
  suspended-copy не шлётся. Verified: test_resolve_runtimeerror_not_deleted.»

Реализация (НЕ трогаем — тестируем):
- ``telegram_inbound.handle_telegram_update`` (694-701): ingress-гейт
  ``try: _resolved = resolve_tenant_from_telegram_id(...); except RuntimeError:
  _resolved = None``. При RuntimeError ``_resolved=None`` → гард пропускается,
  ``is_tenant_active`` даже не зовётся, путь идёт в обычную регистрацию.
- ``max_inbound.handle_max_update`` (139-142): симметрично с
  ``resolve_tenant_from_max_account_id``.

Точка мока (важно для НЕ-вакуумности):
  Оба ``handle_*`` импортируют резолв-функцию ЛОКАЛЬНО внутри тела
  (``from sreda.services.telegram_auth import resolve_tenant_from_telegram_id``;
  ``from sreda.services.max_auth import resolve_tenant_from_max_account_id``).
  Имя резолвится из МОДУЛЯ-ИСТОЧНИКА при каждом вызове, поэтому monkeypatch
  ставим на ``telegram_auth.resolve_tenant_from_telegram_id`` /
  ``max_auth.resolve_tenant_from_max_account_id`` (а не на ``ti``/``mi`` — туда
  локальный import не смотрит).

  Мокаем на уровне САМОЙ резолв-функции — это верно воспроизводит реальный
  error-path: ``hash_tg_account`` бросает RuntimeError (соль не задана), он
  пропагирует через ``find_user_by_chat_id`` (она его НЕ ловит — докстринг
  явно: «RuntimeError ловится выше») и ``resolve_tenant_from_telegram_id`` прямо
  в гейт. ЕДИНСТВЕННОЕ место гашения — ``except RuntimeError`` в самом гейте.
  Поэтому: убери из гейта ``except RuntimeError`` — исключение пропагирует наружу,
  ``ensure_*`` не достигается, и тест станет красным. Контраст-проба
  ``test_contrast_*`` явно фиксирует, что стаб реально бросает (не вакуумно).

Зеркало харнесса — ``test_187_phase1_doors.py`` (фикстура ``fresh_db``,
seed-хелперы, payload-билдеры, форма стабов ``ensure_*`` / клиента).
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.core import InboundMessage
from sreda.db.session import get_engine, get_session_factory

EXISTING_TG_CHAT_ID = "100000003"
EXISTING_MAX_ACCOUNT_ID = "40921122"
EXISTING_MAX_CHAT_ID = "320955459"


# ---- Fixtures (mirror of test_187_phase1_doors.fresh_db) ----------------


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """Empty SQLite DB wired through the same caches production reads."""
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


def _tg_payload(chat_id: str, *, update_id: int = 7501, text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text,
        },
    }


def _max_payload_message(
    *,
    sender_user_id: int = int(EXISTING_MAX_ACCOUNT_ID),
    chat_id: int = int(EXISTING_MAX_CHAT_ID),
    text: str = "привет",
) -> dict:
    return {
        "update_type": "message_created",
        "timestamp": 1777907183208,
        "message": {
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "body": {"mid": "mid.test", "seq": 1, "text": text},
            "sender": {
                "user_id": sender_user_id,
                "first_name": "X",
                "name": "X",
                "is_bot": False,
            },
        },
    }


def _raise_salt_runtimeerror(*_a, **_k):
    """Стаб резолва: соль ``SREDA_TG_ACCOUNT_SALT`` не сконфигурена."""
    raise RuntimeError("SREDA_TG_ACCOUNT_SALT не задан — salt not configured")


# ---- A16 (TG): RuntimeError из резолва трактуется как «не резолвится» ----


@pytest.mark.asyncio
async def test_resolve_runtimeerror_not_deleted_telegram(fresh_db, monkeypatch):
    """A16 (TG). resolve_tenant_from_telegram_id бросает RuntimeError (соль не
    задана) → НЕ трактуется как удалённый: гард пропущен (is_tenant_active НЕ
    зовётся), путь идёт в обычную регистрацию (ensure_* вызван), deleted/
    suspended-copy НЕ шлётся, необработанный RuntimeError на гейте НЕ всплывает.

    «Не удалён» доказывается тем, что управление дошло до ``ensure_*`` — для
    удалённого тенанта (A1) ``ensure`` НЕ вызывается вовсе.
    """
    from sreda.services import telegram_auth
    from sreda.services import tenant_lifecycle
    from sreda.services import telegram_inbound as ti

    # Резолв бросает RuntimeError на УРОВНЕ резолв-функции (см. docstring модуля).
    monkeypatch.setattr(
        telegram_auth, "resolve_tenant_from_telegram_id", _raise_salt_runtimeerror
    )

    # is_tenant_active НЕ должен вызываться при _resolved=None.
    active_spy = MagicMock(
        side_effect=AssertionError("is_tenant_active must not run when resolve fails")
    )
    monkeypatch.setattr(tenant_lifecycle, "is_tenant_active", active_spy)

    # send_message не должен слать ни deleted, ни suspended-copy.
    send_mock = AsyncMock()
    fake_client = MagicMock()
    fake_client.send_message = send_mock
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: fake_client)

    # ensure_* — сигнал «пошли в обычную регистрацию». Останавливаем сразу после
    # вызова, чтобы не тащить полный онбординг (как test_new_user_not_dropped).
    ensure_called = {"n": 0}

    class _StopAfterEnsure(Exception):
        pass

    def fake_ensure(session, payload, *, bot_key="sreda"):
        ensure_called["n"] += 1
        raise _StopAfterEnsure()

    monkeypatch.setattr(ti, "ensure_telegram_user_bundle", fake_ensure)

    # Гейт НЕ должен бросить необработанный RuntimeError — путь доходит до ensure.
    with pytest.raises(_StopAfterEnsure):
        await ti.handle_telegram_update(_tg_payload(EXISTING_TG_CHAT_ID))

    assert ensure_called["n"] == 1  # дошли до обычной регистрации → «не удалён»
    active_spy.assert_not_called()  # гард пропущен (resolve=None), удаление не проверялось
    send_mock.assert_not_called()  # ни deleted-, ни suspended-copy

    with get_session_factory()() as session:
        assert session.query(InboundMessage).all() == []  # без persist на этом пути


# ---- A16 (MAX): симметрично ---------------------------------------------


@pytest.mark.asyncio
async def test_resolve_runtimeerror_not_deleted_max(fresh_db, monkeypatch):
    """A16 (MAX). resolve_tenant_from_max_account_id бросает RuntimeError → НЕ
    «удалён»: гард пропущен (is_tenant_active НЕ зовётся), путь идёт в обычную
    регистрацию (ensure_max_user_bundle вызван), deleted/suspended-copy НЕ
    шлётся, необработанный RuntimeError на гейте НЕ всплывает.
    """
    from sreda.services import max_auth
    from sreda.services import tenant_lifecycle
    from sreda.services import max_inbound as mi

    monkeypatch.setattr(
        max_auth, "resolve_tenant_from_max_account_id", _raise_salt_runtimeerror
    )

    active_spy = MagicMock(
        side_effect=AssertionError("is_tenant_active must not run when resolve fails")
    )
    monkeypatch.setattr(tenant_lifecycle, "is_tenant_active", active_spy)

    send_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            return await send_mock(**kwargs)

    monkeypatch.setattr(mi, "MaxClient", _Client)

    ensure_called = {"n": 0}

    class _StopAfterEnsure(Exception):
        pass

    def fake_ensure(session, *, max_account_id, max_chat_id, display_name):
        ensure_called["n"] += 1
        raise _StopAfterEnsure()

    monkeypatch.setattr(mi, "ensure_max_user_bundle", fake_ensure)

    with pytest.raises(_StopAfterEnsure):
        await mi.handle_max_update(_max_payload_message())

    assert ensure_called["n"] == 1  # дошли до обычной регистрации → «не удалён»
    active_spy.assert_not_called()
    send_mock.assert_not_called()

    with get_session_factory()() as session:
        assert session.query(InboundMessage).all() == []


# ---- Контраст-проба (НЕ-вакуумность) ------------------------------------
# Доказываем, что тест ловит регрессию «убрали except RuntimeError из гейта».
# Эмулируем гейт БЕЗ обёртки except: резолв бросает RuntimeError, который при
# отсутствии `except RuntimeError: _resolved = None` пропагировал бы наружу.
# Если бы реализация трактовала это как «удалён» / роняла гейт — основные тесты
# выше стали бы красными (ensure не был бы достигнут / всплыл бы RuntimeError).


def test_contrast_runtimeerror_would_propagate_without_except():
    """НЕ-вакуумность. Без ``except RuntimeError`` в гейте резолв-RuntimeError
    пропагирует — тогда ``handle_*`` не дошёл бы до ensure и упал с RuntimeError.
    Эта проба фиксирует, что наши стабы РЕАЛЬНО бросают RuntimeError (а не
    тихо отдают None), т.е. основные тесты не вакуумны."""
    with pytest.raises(RuntimeError, match="salt not configured"):
        # Тот же стаб, что подменяет резолв в основных тестах. Реальный гейт
        # оборачивает этот вызов в try/except RuntimeError → _resolved=None.
        _raise_salt_runtimeerror(object(), "100000003")
