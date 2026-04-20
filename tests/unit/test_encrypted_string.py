"""Unit tests for the ``EncryptedString`` SQLAlchemy TypeDecorator.

End-to-end via a tiny throwaway model on SQLite in-memory so the round
trip exercises the real bind/result path SQLAlchemy uses in production,
not just the decorator in isolation.
"""

from __future__ import annotations

import base64
import json

import pytest
from sqlalchemy import Column, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from sreda.db.types import EncryptedString
from sreda.services.encryption import encrypt_value, get_encryption_service


# ---------------------------------------------------------------------------
# Test fixture: a tiny standalone model using EncryptedString.
# Using declarative_base instead of sreda.db.base.Base to avoid loading the
# whole schema for a focused test.
# ---------------------------------------------------------------------------

Base = declarative_base()


class _Secret(Base):
    __tablename__ = "_test_secrets"
    id = Column(String(32), primary_key=True)
    payload = Column(EncryptedString(), nullable=True)


@pytest.fixture(autouse=True)
def _stable_encryption_key(monkeypatch):
    """Set a deterministic 32-byte key and reset the cached service so
    each test starts from a clean encryption state."""
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
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_returns_plaintext(session):
    session.add(_Secret(id="1", payload="дочь Маша 9 лет"))
    session.commit()

    session.expire_all()  # force DB read
    row = session.get(_Secret, "1")
    assert row.payload == "дочь Маша 9 лет"


def test_stored_value_is_encrypted_envelope(session):
    session.add(_Secret(id="2", payload="hello"))
    session.commit()

    # Bypass the ORM TypeDecorator — talk to raw SQL so we see the
    # on-disk ciphertext. Required proof that plaintext never landed.
    raw = session.execute(
        text("SELECT payload FROM _test_secrets WHERE id='2'")
    ).scalar()
    assert raw.startswith("v2:test:")
    assert "hello" not in raw


def test_none_passes_through(session):
    session.add(_Secret(id="3", payload=None))
    session.commit()
    session.expire_all()
    row = session.get(_Secret, "3")
    assert row.payload is None


def test_empty_string_round_trip(session):
    session.add(_Secret(id="4", payload=""))
    session.commit()
    session.expire_all()
    row = session.get(_Secret, "4")
    assert row.payload == ""


def test_each_write_has_unique_nonce(session):
    """Two rows with identical plaintext must produce different ciphertexts
    (GCM nonce must be per-record, not deterministic)."""
    session.add(_Secret(id="5a", payload="same"))
    session.add(_Secret(id="5b", payload="same"))
    session.commit()

    raws = session.execute(
        text("SELECT payload FROM _test_secrets WHERE id IN ('5a', '5b')")
    ).scalars().all()
    assert len(raws) == 2
    assert raws[0] != raws[1]  # different ciphertexts
    assert all(r.startswith("v2:test:") for r in raws)


# ---------------------------------------------------------------------------
# Legacy plaintext tolerance — critical for the rollout path
# ---------------------------------------------------------------------------


def test_legacy_plaintext_row_returned_as_is(session):
    """Row written BEFORE the column was marked EncryptedString must
    continue reading as plaintext (no envelope prefix → skip decrypt)."""
    session.execute(
        text("INSERT INTO _test_secrets (id, payload) VALUES (:id, :p)"),
        {"id": "legacy1", "p": "legacy plaintext value"},
    )
    session.commit()
    session.expire_all()

    row = session.get(_Secret, "legacy1")
    assert row.payload == "legacy plaintext value"


def test_next_write_to_legacy_row_encrypts_it(session):
    """The migration path: read legacy plaintext, force a rewrite of the
    same value — value should land encrypted on disk.

    ``flag_modified`` is how a migration script would force SQLAlchemy
    to flush a value it otherwise deduplicates as "unchanged".
    """
    from sqlalchemy.orm.attributes import flag_modified

    session.execute(
        text("INSERT INTO _test_secrets (id, payload) VALUES (:id, :p)"),
        {"id": "legacy2", "p": "will-be-encrypted"},
    )
    session.commit()

    row = session.get(_Secret, "legacy2")
    assert row.payload == "will-be-encrypted"  # sanity: read via decorator
    flag_modified(row, "payload")
    session.commit()

    raw = session.execute(
        text("SELECT payload FROM _test_secrets WHERE id='legacy2'")
    ).scalar()
    assert raw.startswith("v2:test:")


def test_invalid_envelope_fails_loudly(session):
    """An envelope prefix with junk payload must raise — we never want
    to silently return garbage to the LLM."""
    session.execute(
        text("INSERT INTO _test_secrets (id, payload) VALUES (:id, :p)"),
        {"id": "bad", "p": "v2:test:notvalid:alsonotvalid"},
    )
    session.commit()
    session.expire_all()

    with pytest.raises(Exception):  # noqa: B017 — we don't care which; just not silent
        _ = session.get(_Secret, "bad").payload


# ---------------------------------------------------------------------------
# Key rotation — legacy key still decodes older rows
# ---------------------------------------------------------------------------


def test_legacy_key_decrypts_row_written_before_rotation(session, monkeypatch):
    """Reproduce the rotation scenario: write with old key, move old to
    legacy_keys, add new primary. Old row must still decode."""
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "old")
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    get_encryption_service.cache_clear()

    session.add(_Secret(id="rot", payload="before rotation"))
    session.commit()

    raw_before = session.execute(
        text("SELECT payload FROM _test_secrets WHERE id='rot'")
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
    row = session.get(_Secret, "rot")
    assert row.payload == "before rotation"


# ---------------------------------------------------------------------------
# Sanity: encrypt_value and the decorator agree on envelope format
# ---------------------------------------------------------------------------


def test_decorator_accepts_envelope_produced_by_encrypt_value(session):
    ciphertext = encrypt_value("direct call")

    session.execute(
        text("INSERT INTO _test_secrets (id, payload) VALUES (:id, :p)"),
        {"id": "direct", "p": ciphertext},
    )
    session.commit()
    session.expire_all()

    row = session.get(_Secret, "direct")
    assert row.payload == "direct call"
