"""Shared SQLAlchemy column types.

Transparent at-rest encryption for columns that hold sensitive personal
data. Design goals:

* **Zero caller changes** — ORM attribute access returns plaintext as
  before; writes take plaintext and the type encrypts transparently.
* **Backwards compatible with existing plaintext rows** — during the
  rollout there will be rows that were written before this type was
  applied. On read the decorator detects the envelope (``v1:`` / ``v2:``
  prefix from ``services.encryption``) and only decrypts values that
  carry it. Anything else is returned as-is so legacy rows keep
  working until a one-shot migration re-encrypts them.
* **Never hide failures silently** — if a value CLEARLY looks encrypted
  (has the envelope prefix) but decryption fails, the exception
  propagates. That's a key-management problem, not a transient; better
  to break loudly than return garbage to the LLM.

The encryption primitive is ``services.encryption.encrypt_value`` /
``decrypt_value`` (AES-256-GCM with per-record nonce + versioned
envelope + rotation via legacy keys). See that module for the wire
format and the rotation story.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


_ENCRYPTED_PREFIXES = ("v1:", "v2:")


class EncryptedString(TypeDecorator):
    """``Text``-compatible column that encrypts at rest.

    Usage::

        from sreda.db.types import EncryptedString

        class Thing(Base):
            secret: Mapped[str] = mapped_column(EncryptedString(Text))

    No change in how ``thing.secret`` is read or written from Python —
    it's a plain string both ways. The ciphertext lives only in the
    database row.

    Limits:
      * No searching by ``secret == 'x'`` or ``LIKE``. Callers that need
        to find rows by content must index on a plaintext fingerprint
        or do exact-match by id. We're encrypting columns that are
        read by PK / by small index (tenant+user), so this is fine.
      * Slightly bigger storage: AES-GCM + base64 + envelope adds ~60
        bytes and ~40% size for short strings. Negligible at our scale.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: Any, dialect: Any
    ) -> str | None:  # noqa: ARG002 — dialect unused
        if value is None:
            return None
        if not isinstance(value, str):
            # Pydantic or dict bleed — ORM should surface this, not us.
            value = str(value)
        # Lazy import — avoid a circular with config/settings → db.
        from sreda.services.encryption import encrypt_value

        return encrypt_value(value)

    def process_result_value(
        self, value: Any, dialect: Any
    ) -> str | None:  # noqa: ARG002
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        # Legacy plaintext tolerance: rows written before this decorator
        # was applied don't have the envelope prefix. Return as-is so
        # the rollout doesn't require a blocking migration step.
        if not value.startswith(_ENCRYPTED_PREFIXES):
            return value
        from sreda.services.encryption import decrypt_value

        return decrypt_value(value)
