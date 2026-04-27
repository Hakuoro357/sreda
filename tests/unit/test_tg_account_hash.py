"""Тесты для services.tg_account_hash + double-column схемы User.

Покрывает 152-ФЗ обезличивание Часть 1 (2026-04-27):
  * `hash_tg_account` — детерминированный, salted HMAC-SHA256.
  * `User` event-листенер — auto-fill `tg_account_hash` при записи
    `telegram_account_id`.
  * `find_user_by_chat_id` — резолв через hash, не plaintext.
  * Поведение при отсутствии salt'а — RuntimeError, не silent fail.
"""

from __future__ import annotations

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.tg_account_hash import hash_tg_account


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    return Session()


def test_hash_tg_account_deterministic():
    """Один и тот же chat_id под тем же salt'ом всегда даёт один hash.
    Это критично для lookup'а — иначе юзер «потеряется» после рестарта.
    """
    a = hash_tg_account("352612382")
    b = hash_tg_account("352612382")
    assert a == b
    assert len(a) == 64  # SHA-256 hex


def test_hash_tg_account_int_or_str():
    """chat_id может прилететь как int (из payload) или как str (из БД)."""
    assert hash_tg_account(352612382) == hash_tg_account("352612382")


def test_hash_tg_account_different_inputs_different_hashes():
    a = hash_tg_account("100")
    b = hash_tg_account("101")
    assert a != b


def test_hash_tg_account_different_salts_different_hashes(monkeypatch):
    """При смене salt'а hash меняется — поэтому salt нельзя ротировать
    без полного backfill."""
    from sreda.config.settings import get_settings

    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "salt-A")
    get_settings.cache_clear()
    h_a = hash_tg_account("352612382")

    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "salt-B")
    get_settings.cache_clear()
    h_b = hash_tg_account("352612382")

    assert h_a != h_b


def test_hash_tg_account_missing_salt_raises(monkeypatch):
    """Без salt'а должен валиться RuntimeError, а не возвращать пустой
    hash — иначе все юзеры будут матчиться на одну строку."""
    from sreda.config.settings import get_settings

    monkeypatch.delenv("SREDA_TG_ACCOUNT_SALT", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SREDA_TG_ACCOUNT_SALT"):
        hash_tg_account("352612382")


def test_user_event_listener_fills_hash_on_set():
    """При создании User с telegram_account_id хеш заполняется
    автоматически через event-листенер (см. db/models/core.py)."""
    session = _make_session()
    session.add(Tenant(id="t1", name="Test"))
    session.commit()

    user = User(
        id="user_1", tenant_id="t1", telegram_account_id="352612382",
    )
    session.add(user)
    session.commit()

    assert user.tg_account_hash is not None
    assert user.tg_account_hash == hash_tg_account("352612382")


def test_user_event_listener_clears_hash_on_none():
    session = _make_session()
    session.add(Tenant(id="t1", name="Test"))
    session.commit()

    user = User(
        id="user_1", tenant_id="t1", telegram_account_id="352612382",
    )
    session.add(user)
    session.commit()
    assert user.tg_account_hash is not None

    user.telegram_account_id = None
    session.commit()
    assert user.tg_account_hash is None


def test_user_telegram_account_id_round_trip_decrypt():
    """telegram_account_id хранится зашифрованным (EncryptedString),
    но ORM на read возвращает plaintext."""
    session = _make_session()
    session.add(Tenant(id="t1", name="Test"))
    session.commit()

    plain = "352612382"
    user = User(id="user_1", tenant_id="t1", telegram_account_id=plain)
    session.add(user)
    session.commit()

    fresh = session.get(User, "user_1")
    assert fresh.telegram_account_id == plain  # decrypt прозрачен


def test_find_user_by_chat_id_via_hash():
    """find_user_by_chat_id находит юзера через hash — не через plain."""
    from sreda.services.onboarding import find_user_by_chat_id

    session = _make_session()
    session.add(Tenant(id="t1", name="Test"))
    session.add(User(
        id="user_1", tenant_id="t1", telegram_account_id="352612382",
    ))
    session.commit()

    found = find_user_by_chat_id(session, "352612382")
    assert found is not None
    assert found.id == "user_1"

    # int тоже должен работать (payload иногда даёт int).
    found_int = find_user_by_chat_id(session, 352612382)
    assert found_int is not None
    assert found_int.id == "user_1"


def test_find_user_by_chat_id_missing_returns_none():
    from sreda.services.onboarding import find_user_by_chat_id

    session = _make_session()
    session.add(Tenant(id="t1", name="Test"))
    session.commit()

    assert find_user_by_chat_id(session, "999") is None
    assert find_user_by_chat_id(session, "") is None
    assert find_user_by_chat_id(session, None) is None
