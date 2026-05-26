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
SHA-256 hex of the lemmatized title — used for semantic-dedup
lookups via ``WHERE normalized_title_hash = ?``. We use a hash
column rather than storing the lemma plaintext so:

  - Indexes don't leak content (relevant for tables where ``title``
    is encrypted at rest via ``EncryptedString``).
  - Fixed-length column type (64 chars) keeps the schema tidy.

Format note: op_ids are prefixed ``op_`` so they're immediately
recognizable in audit logs and DB columns.
"""

from __future__ import annotations

import hashlib

from sreda.services.text_normalization import normalize_for_dedup


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

    Returns the SHA-256 hex of ``normalize_for_dedup(title)``. Empty
    input returns an empty string (caller treats as "no dedup
    possible — accept whatever it is").
    """
    normalized = normalize_for_dedup(title)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = [
    "compute_operation_id_create",
    "compute_operation_id_update",
    "compute_normalized_title_hash",
]
