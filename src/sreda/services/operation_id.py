"""Idempotency-key helpers (Sub-A10, Group 3.1 of Plan-Execute Epic).

Two distinct hash functions cover the two retry semantics described in
Group 3.1:

  - **create**: ``op_id = sha1(plan_id, step_id, action, entity_type,
    logical_key)``. ``logical_key`` is the pre-INSERT canonical form,
    typically ``normalize_for_dedup(title)``. Lets a retry of the
    same plan-step against the same canonical title produce the
    *same* op_id, which the partial unique index on
    ``(tenant_id, operation_id)`` makes idempotent via
    ``INSERT ... ON CONFLICT ... DO NOTHING``.

  - **update / delete**: ``op_id = sha1(plan_id, step_id, action,
    entity_type, entity_id)``. ``entity_id`` is known up front
    (the row already exists), so retries naturally produce identical
    op_ids without needing logical_key.

Separately, ``compute_normalized_title_hash(title)`` returns the
**HMAC-SHA256** hex of the lemmatized title, keyed by
``SREDA_ENCRYPTION_KEY`` — used for semantic-dedup lookups via
``WHERE normalized_title_hash = ?``. We use a *keyed* hash rather
than plain SHA-256 because shopping titles are low-entropy
("молоко", "хлеб") and a plain hash is dictionary-attackable —
an attacker with read access to the dedup column could rebuild
the user's shopping list from a precomputed rainbow table.
HMAC with the server secret makes the table opaque without the
key. Codex Sub-A10 R1 MAJOR #4.

We use a hash column rather than storing the lemma plaintext so:

  - Indexes don't leak content (relevant for tables where ``title``
    is encrypted at rest via ``EncryptedString``).
  - Fixed-length column type (64 chars) keeps the schema tidy.

Format note: op_ids are prefixed ``op_`` so they're immediately
recognizable in audit logs and DB columns.

Entity-specific logical_key recipes (Codex Sub-A10 R1 MAJOR #8):

  - ``shopping_list_item``: ``normalize_for_dedup(title)`` is
    sufficient. Two of the same item on different days are
    intentionally collapsed — "опять молоко" is a partial-duplicate,
    not a new row.
  - ``family_reminder``: include the trigger time. Two reminders
    "дать лекарство" today and tomorrow are distinct.
    ``sha1(normalize_for_dedup(title), trigger_iso, recurrence_rule or "")``
    is the right shape.
  - ``task_item``: include the scheduled date. Two "сходить на
    тренировку" tasks on different days are distinct.
    ``sha1(normalize_for_dedup(title), scheduled_date.isoformat() if scheduled_date else "")``.
  - ``recipe``: ``normalize_for_dedup(title)`` is sufficient. Same
    recipe re-saved is a partial-duplicate by design.
  - ``checklist``: ``normalize_for_dedup(title)`` is sufficient.

Callers compose the logical_key according to the recipe above and
pass the result into ``compute_operation_id_create``. We deliberately
don't bake the recipe into this module — keeping it caller-side lets
the tool author pick the right grain (e.g. shopping items with units
might want to include the unit too).
"""

from __future__ import annotations

import hashlib
import hmac
import os

from sreda.services.text_normalization import normalize_for_dedup


def _get_hmac_key() -> bytes:
    """Resolve the HMAC secret for normalized_title_hash.

    Preference order:
      1. ``SREDA_ENCRYPTION_KEY`` env var (the same key that's used for
         ``EncryptedString`` at-rest encryption; safe to reuse — HMAC
         and Fernet have non-overlapping domains).
      2. Empty bytes → fall back to plain SHA-256 in development /
         test environments where the key isn't set. Logs a warning
         once via the caller; we don't import logging here to keep
         the module dependency-free.

    Codex Sub-A10 R1 MAJOR #4 — Production code paths that compute
    this hash MUST have the env var configured; missing key produces
    a usable-but-weaker hash that's still safe for SQL equality
    lookups, just dictionary-attackable.
    """
    raw = os.environ.get("SREDA_ENCRYPTION_KEY", "")
    return raw.encode("utf-8")


def compute_operation_id_create(
    *,
    plan_id: str,
    step_id: str,
    action: str,
    entity_type: str,
    logical_key: str,
) -> str:
    """Compute the idempotent operation id for a *create* operation.

    The hash inputs are concatenated with ``\\x1f`` (ASCII Unit
    Separator) so that pipe / colon / space inside any field can't
    collide across boundaries — e.g. logical_key="a|b" + entity_type="c"
    and logical_key="a" + entity_type="b|c" don't produce the same
    op_id.
    """
    sep = "\x1f"
    payload = sep.join([plan_id, step_id, action, entity_type, logical_key])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"op_{digest}"


def compute_operation_id_update(
    *,
    plan_id: str,
    step_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
) -> str:
    """Compute the idempotent operation id for an *update / delete*
    operation. Distinct from ``create`` via the ``action`` field so
    a misuse (passing entity_id as logical_key, or vice versa) can't
    accidentally collide.
    """
    sep = "\x1f"
    payload = sep.join([plan_id, step_id, action, entity_type, entity_id])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"op_{digest}"


def compute_normalized_title_hash(title: str) -> str:
    """Compute the dedup-hash of a title.

    Returns HMAC-SHA256 hex of ``normalize_for_dedup(title)`` keyed
    by the ``SREDA_ENCRYPTION_KEY`` env var. If the env var is empty
    (dev / test path), falls back to plain SHA-256 so the function
    is always usable — the resulting hash is still a stable equality
    key, just dictionary-attackable.

    Empty input returns an empty string (caller treats as "no dedup
    possible — accept whatever it is").
    """
    normalized = normalize_for_dedup(title)
    if not normalized:
        return ""
    msg = normalized.encode("utf-8")
    key = _get_hmac_key()
    if key:
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    # Fallback for tests / dev — caller is expected to set the key
    # before computing hashes that will land in prod data.
    return hashlib.sha256(msg).hexdigest()


__all__ = [
    "compute_operation_id_create",
    "compute_operation_id_update",
    "compute_normalized_title_hash",
]
