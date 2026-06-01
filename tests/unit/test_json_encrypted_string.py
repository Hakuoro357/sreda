"""Unit tests for the ``JSONEncryptedString`` SQLAlchemy TypeDecorator.

End-to-end via a tiny throwaway model on SQLite in-memory so the round
trip exercises the real bind/result path SQLAlchemy uses in production,
not just the decorator in isolation.

Mirrors the structure of test_encrypted_string.py.
"""

from __future__ import annotations

import base64
import json

import pytest
from sqlalchemy import Column, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from sreda.db.types import JSONEncryptedString
from sreda.services.encryption import get_encryption_service


# ---------------------------------------------------------------------------
# Test fixture: a tiny standalone model using JSONEncryptedString.
# Using declarative_base instead of sreda.db.base.Base to avoid loading the
# whole schema for a focused test.
# ---------------------------------------------------------------------------

Base = declarative_base()


class _JsonStore(Base):
    __tablename__ = "_test_json_store"
    id = Column(String(32), primary_key=True)
    payload = Column(JSONEncryptedString(), nullable=True)


@pytest.fixture(autouse=True)
def _stable_encryption_key(monkeypatch):
    """Set a deterministic 32-byte key and reset the cached service."""
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "test")
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY_SALT", raising=False)
    monkeypatch.delenv("SREDA_ENCRYPTION_LEGACY_KEYS", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    get_encryption_service.cache_clear()
    yield
    get_settings.cache_clear()
    get_encryption_service.cache_clear()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# 1. Round-trip a dict
# ---------------------------------------------------------------------------


def test_round_trip_dict(session):
    original = {"name": "Мария", "age": 9}
    session.add(_JsonStore(id="1", payload=original))
    session.commit()

    session.expire_all()
    row = session.get(_JsonStore, "1")
    assert row.payload == original


def test_stored_dict_is_encrypted_not_plaintext(session):
    """The raw on-disk value must be a ciphertext envelope, not JSON text."""
    session.add(_JsonStore(id="2", payload={"key": "secret"}))
    session.commit()

    raw = session.execute(
        text("SELECT payload FROM _test_json_store WHERE id='2'")
    ).scalar()
    assert raw.startswith("v2:test:")
    # The plaintext JSON must NOT appear verbatim in the ciphertext.
    assert "secret" not in raw
    assert "{" not in raw


# ---------------------------------------------------------------------------
# 2. Round-trip a list
# ---------------------------------------------------------------------------


def test_round_trip_list(session):
    original = [1, "два", {"three": 3}]
    session.add(_JsonStore(id="3", payload=original))
    session.commit()

    session.expire_all()
    row = session.get(_JsonStore, "3")
    assert row.payload == original


# ---------------------------------------------------------------------------
# 3. None → None
# ---------------------------------------------------------------------------


def test_none_passes_through(session):
    session.add(_JsonStore(id="4", payload=None))
    session.commit()
    session.expire_all()
    row = session.get(_JsonStore, "4")
    assert row.payload is None


# ---------------------------------------------------------------------------
# 4. Non-serialisable input raises TypeError (not silently stored as repr)
# ---------------------------------------------------------------------------


def test_non_serializable_set_raises():
    # Call process_bind_param directly — no SQLAlchemy wrapping, plain TypeError.
    typ = JSONEncryptedString()
    with pytest.raises(TypeError):
        typ.process_bind_param({1, 2, 3}, dialect=None)


def test_non_serializable_custom_object_raises():
    class _Opaque:
        pass

    typ = JSONEncryptedString()
    with pytest.raises(TypeError):
        typ.process_bind_param(_Opaque(), dialect=None)


# ---------------------------------------------------------------------------
# 5. Legacy-plaintext read: a pre-migration row with raw JSON text
# ---------------------------------------------------------------------------


def test_legacy_plaintext_json_parsed_on_read(session):
    """Row written before migration contains raw JSON with no encryption prefix.
    process_result_value must json.loads it and return the Python object."""
    legacy_json = json.dumps({"legacy": True, "score": 42})
    session.execute(
        text("INSERT INTO _test_json_store (id, payload) VALUES (:id, :p)"),
        {"id": "legacy1", "p": legacy_json},
    )
    session.commit()
    session.expire_all()

    row = session.get(_JsonStore, "legacy1")
    assert row.payload == {"legacy": True, "score": 42}


def test_legacy_plaintext_list_parsed_on_read(session):
    legacy_json = json.dumps([10, 20, 30])
    session.execute(
        text("INSERT INTO _test_json_store (id, payload) VALUES (:id, :p)"),
        {"id": "legacy2", "p": legacy_json},
    )
    session.commit()
    session.expire_all()

    row = session.get(_JsonStore, "legacy2")
    assert row.payload == [10, 20, 30]


# ---------------------------------------------------------------------------
# 6. v2 round-trip via direct process_bind_param / process_result_value calls
# ---------------------------------------------------------------------------


def test_direct_bind_result_roundtrip():
    """Exercises the TypeDecorator methods without touching the ORM session,
    confirming the encrypt/decrypt path is wired correctly in isolation."""
    typ = JSONEncryptedString()
    original = {"x": 1, "y": [2, 3]}

    bound = typ.process_bind_param(original, dialect=None)

    # Must be an encrypted envelope, not raw JSON.
    assert bound is not None
    assert bound.startswith("v2:test:")

    result = typ.process_result_value(bound, dialect=None)
    assert result == original


def test_direct_none_roundtrip():
    typ = JSONEncryptedString()
    assert typ.process_bind_param(None, dialect=None) is None
    assert typ.process_result_value(None, dialect=None) is None


# ---------------------------------------------------------------------------
# 7. Invalid envelope fails loudly (Codex R1 MINOR — mirror EncryptedString)
# ---------------------------------------------------------------------------


def test_invalid_envelope_fails_loudly(session):
    """A v2-prefixed envelope with junk payload must raise on read — never
    silently return garbage, and never reach json.loads."""
    session.execute(
        text("INSERT INTO _test_json_store (id, payload) VALUES (:id, :p)"),
        {"id": "bad", "p": "v2:test:notvalid:alsonotvalid"},
    )
    session.commit()
    session.expire_all()

    with pytest.raises(Exception):  # noqa: B017 — don't care which; just not silent
        _ = session.get(_JsonStore, "bad").payload


# ---------------------------------------------------------------------------
# 8. Key rotation — legacy key still decodes JSON rows written before rotation
# ---------------------------------------------------------------------------


def test_legacy_key_decrypts_json_written_before_rotation(session, monkeypatch):
    """Write JSON with the old key, move old → legacy_keys, add a new primary.
    The pre-rotation row's JSON must still decode."""
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "old")
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    get_encryption_service.cache_clear()

    original = {"before": "rotation", "items": [1, 2]}
    session.add(_JsonStore(id="rot", payload=original))
    session.commit()

    raw_before = session.execute(
        text("SELECT payload FROM _test_json_store WHERE id='rot'")
    ).scalar()
    assert raw_before.startswith("v2:old:")

    new_key_b64 = base64.urlsafe_b64encode(
        b"fedcba9876543210fedcba9876543210"
    ).decode("ascii")
    old_key_b64 = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef"
    ).decode("ascii")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", new_key_b64)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "new")
    monkeypatch.setenv(
        "SREDA_ENCRYPTION_LEGACY_KEYS", json.dumps({"old": old_key_b64})
    )
    get_settings.cache_clear()
    get_encryption_service.cache_clear()

    session.expire_all()
    row = session.get(_JsonStore, "rot")
    assert row.payload == original
