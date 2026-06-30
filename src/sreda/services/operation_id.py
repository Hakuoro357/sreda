"""Idempotency-key helpers (Sub-A10, Group 3.1 of Plan-Execute Epic).

Two distinct hash functions cover the two retry semantics described in
Group 3.1:

  - **create**: ``op_id = sha1(plan_id, step_id, action, entity_type,
    logical_key)``. ``logical_key`` is the pre-INSERT canonical form,
    typically ``normalize_for_dedup(title)``. Lets a retry of the
    same plan-step against the same canonical title produce the
    *same* op_id, which the unique index on
    ``(tenant_id, user_id, operation_id)`` makes idempotent via
    ``INSERT ... ON CONFLICT (tenant_id, user_id, operation_id)
    DO NOTHING``. Codex Sub-A10 R1 CRITICAL #1 — full UNIQUE
    (NOT partial), so the conflict target needs no ``index_where``.
    Codex R1 MAJOR #3 — scope includes ``user_id``.

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
  - ``task``: include the scheduled date (+time). Two "сходить на
    тренировку" tasks on different days are distinct.
    ``sha1(normalize_for_dedup(title), scheduled_date.isoformat() if scheduled_date else "", time)``.
  - ``recipe``: ``normalize_for_dedup(title)`` is sufficient. Same
    recipe re-saved is a partial-duplicate by design.
  - ``checklist``: ``normalize_for_dedup(title)`` is sufficient.

Callers compose the logical_key according to the recipe above and
pass the result into ``compute_operation_id_create``. We deliberately
don't bake the recipe into this module — keeping it caller-side lets
the tool author pick the right grain (e.g. shopping items with units
might want to include the unit too).

Known scope gaps (acceptable for Sub-A10 MVP, tracked for post-MVP):

  - **Update / delete history loss** (Codex R2 MAJOR #2): the
    ``operation_id`` column on the target row is overwritten by later
    updates and erased by hard deletes. A retry of an OLD update
    after newer state changes isn't catchable. A separate
    ``tool_operations`` history table (keyed by op_id) would close
    this — deferred to the Audit Feed work in Group 6.4 / Sub-A11.
  - **Nullable ``user_id`` in family_reminders** (Codex R2 MAJOR #4):
    standard SQL treats NULL as distinct in UNIQUE, so two reminders
    with ``user_id=NULL`` and same op_id won't collide. Planner-flow
    code MUST pass non-null user_id for idempotent writes. Enforced
    at the tool-call site, not the schema.
  - **Backfill for legacy rows** (Codex R2 MAJOR #5): existing rows
    have both columns NULL; semantic dedup won't catch pre-migration
    duplicates. Intentional — we'd rather not decrypt + rewrite
    months of historical data. New rows fully participate.
  - **pymorphy3 hash drift** (Codex R2 MAJOR #6): NORMALIZATION_VERSION
    is folded into the hash payload above, so a pin bump produces
    new hashes for the same input — old hashes stay valid for their
    rows, new lookups use the new key. No backfill needed; the
    semantic-dedup index just has lower hit rate for a while.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from sreda.services.text_normalization import (
    NORMALIZATION_VERSION,
    normalize_for_dedup,
)


_logger = logging.getLogger(__name__)
# Codex Sub-A10 R3 MAJOR #3 — warn once per process when fallback
# kicks in. Subsequent calls stay quiet so we don't flood the log.
_fallback_warned = False


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


def compute_normalized_title_hash(
    title: str,
    *,
    entity_type: str = "",
    tenant_id: str = "",
    user_id: str = "",
    extra: str = "",
) -> str:
    """Compute the dedup-hash of a title.

    Returns HMAC-SHA256 hex of a payload that includes:

      - ``NORMALIZATION_VERSION`` (cross-version stability marker)
      - ``entity_type`` (domain separation: a "молоко" shopping item
        doesn't hash-collide with a "молоко" task)
      - ``tenant_id`` (per-tenant scope: leak across tenants is broken)
      - ``user_id`` (per-user scope: same)
      - ``normalize_for_dedup(title)``

    The HMAC key is ``SREDA_ENCRYPTION_KEY``; falls back to plain
    SHA-256 over the same payload when the key is empty (dev/test
    convenience). Codex Sub-A10 R1 MAJOR #4 + R2 MAJOR #3/#5.

    Empty title input → empty string (caller treats as "no dedup
    possible — accept whatever it is"). Other scope args may be
    blank (legacy callers / dev), but production callers SHOULD
    pass entity_type, tenant_id, user_id so cross-domain /
    cross-tenant equality is impossible.
    """
    normalized = normalize_for_dedup(title)
    if not normalized:
        return ""
    # Use \x1f (Unit Separator) between fields so values containing
    # ``|`` / ``:`` / spaces can't cross boundaries.
    sep = "\x1f"
    fields = [
        f"v{NORMALIZATION_VERSION}",
        entity_type,
        tenant_id,
        user_id,
        normalized,
    ]
    # #163 Фаза 2а: time-aware семантический ключ (для напоминаний trigger+rrule, для задач
    # date+time). Аппендим ТОЛЬКО если extra непуст → хеш title-only вызывающих (shopping) НЕ
    # меняется (обратная совместимость; иначе сдвинулись бы существующие dedup-хеши).
    if extra:
        fields.append(extra)
    payload = sep.join(fields)
    msg = payload.encode("utf-8")
    key = _get_hmac_key()
    if key:
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    # Codex R3 MAJOR #3 — log once per process so production
    # misconfiguration is visible. We don't raise because that
    # would break dev/test paths that don't set the key.
    global _fallback_warned
    if not _fallback_warned:
        _logger.warning(
            "compute_normalized_title_hash falling back to plain "
            "SHA-256 — SREDA_ENCRYPTION_KEY is empty. Dedup hashes "
            "computed in this state are dictionary-attackable. "
            "Set the env var in production."
        )
        _fallback_warned = True
    return hashlib.sha256(msg).hexdigest()


__all__ = [
    "compute_operation_id_create",
    "compute_operation_id_update",
    "compute_normalized_title_hash",
]
