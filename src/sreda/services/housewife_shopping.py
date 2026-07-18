"""Housewife shopping-list service — CRUD over ShoppingListItem.

Per-user single global list. Items always have a ``category`` from a
fixed taxonomy (see ``SHOPPING_CATEGORIES``); the LLM classifies on add,
the Mini App groups visually. Status lifecycle:

  pending  → bought     (user checked it off)
           → cancelled  (user removed without buying)

Cancelled/bought rows stick around for history — the ``list_pending``
path is what the UI and LLM normally see.

Follows the pattern of ``housewife_reminders.py``: service owns its
session commits (per-mutation), no batch transactions — keeps LLM tool
call semantics predictable when several tool invocations chain in one
turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.housewife_food import (
    SHOPPING_CATEGORIES,
    SHOPPING_STATUSES,
    ShoppingListItem,
)
from sreda.runtime.planner.tool_runtime import current_tool_runtime
from sreda.services.audit_feed import emit_event
from sreda.services.operation_id import (
    compute_normalized_title_hash,
    compute_operation_id_create,
)
from sreda.services.text_normalization import normalize_for_dedup

logger = logging.getLogger(__name__)


DEFAULT_CATEGORY = "другое"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ShoppingItemInput:
    """Normalized input payload for ``add_items``."""

    title: str
    quantity_text: str | None = None
    category: str | None = None
    source_recipe_id: str | None = None


def _normalise_title(title: str | None) -> str:
    """Canonical form for exact-title dedup in the shopping list.

    Lowercased, stripped, internal whitespace collapsed. 'молоко',
    'Молоко' and '  молоко  ' all normalise to the same key so
    add_items can skip cross-day duplicates.
    """
    if not title:
        return ""
    return " ".join(title.strip().lower().split())


def _coerce_category(raw: str | None) -> str:
    """Normalise an LLM/user-supplied category name.

    As of v1.2 launch: we accept ARBITRARY categories, not just the
    fixed taxonomy. When the LLM invents "специи" / "детское питание"
    / "канцелярия" — that's a legitimate user bucket and should be
    preserved, not force-mapped to "другое". The Mini App renders
    unknown keys as-is (fallback in ``_SHOPPING_CATEGORY_LABELS``),
    and sorting treats unknown categories as trailing the fixed
    taxonomy (see ``_list_by_status``).

    Normalisation: strip whitespace, collapse internal whitespace to
    single spaces, lowercase, truncate to 64 chars. Empty / None →
    ``DEFAULT_CATEGORY`` because "no category" = "другое".

    SHOPPING_CATEGORIES still maps to canonical form (keeps DB
    consistent for historical rows that came through the old path).
    """
    if not raw:
        return DEFAULT_CATEGORY
    candidate = " ".join(raw.strip().lower().split())
    if not candidate:
        return DEFAULT_CATEGORY
    # Canonicalise known taxonomy names (defensive for typos like
    # "молочные  " or "МОЛОЧНЫЕ").
    for allowed in SHOPPING_CATEGORIES:
        if candidate == allowed:
            return allowed
    # Custom category from LLM / user — preserve but truncate.
    return candidate[:64]


# Keyword dictionary for title-based auto-classification. Each entry is
# (category, [keywords]). First-match wins — so order the categories
# by specificity if a keyword could match multiple.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "молочные",
        (
            "молоко", "сметан", "творог", "кефир", "йогурт", "ряженк",
            "сыр", "масло сливоч", "масло слив", "сливки", "сгущёнк", "сгущенк",
            "брынза", "моцарелл", "рикотт", "пармезан", "фета",
        ),
    ),
    (
        "мясо_рыба",
        (
            "курица", "куриное", "куриная", "куриный", "курин",
            "говядин", "свинин", "баранин", "телятин", "индейк", "утк", "утиное",
            "фарш", "котлет", "стейк", "ребр", "окорок", "бекон", "ветчин",
            "колбас", "сосиск", "карбонад",
            "рыба", "рыбн", "лосос", "сёмг", "семг", "тунец", "треск", "минта",
            "форель", "скумбри", "сельд", "селёдк", "селедк", "хек",
            "кревет", "кальмар", "мидии", "осьминог",
        ),
    ),
    (
        "овощи_фрукты",
        (
            "морков", "лук ", "лук.", "репчат", "картошк", "картоф",
            "помидор", "томат", "огурц", "огурец", "огурк",
            "капуст", "свёкл", "свекл", "чеснок", "перец", "болгарск",
            "баклажан", "кабачок", "цуккин", "тыкв", "редис", "редьк",
            "укроп", "петрушк", "кинза", "базилик", "зелень", "шпинат",
            "яблок", "груш", "банан", "апельсин", "лимон", "мандарин",
            "виноград", "арбуз", "дын", "клубник", "малин", "вишн", "черешн",
            "ягод", "авокадо", "ананас", "манго", "киви", "персик", "слив",
            "гриб", "шампиньон",
        ),
    ),
    (
        "хлеб",
        (
            "хлеб", "батон", "булк", "булоч", "багет", "лаваш", "тост",
        ),
    ),
    (
        "бакалея",
        (
            "мука", "сахар", "соль", "сод", "разрыхлит", "дрожж",
            "рис", "гречк", "пшен", "овсянк", "овсян", "перловк", "булгур", "киноа",
            "макарон", "спагетти", "паста", "лапш", "вермишел",
            "масло растит", "масло подсолн", "масло олив", "оливковое",
            "уксус", "соус", "кетчуп", "майонез", "горчиц",
            "специй", "специи", "приправ", "перец молот", "лаврушк", "лавровы",
            "чай", "кофе", "какао",
            "орех", "миндал", "фундук", "арахис", "фисташк", "кешью",
            "мёд", "мед ", "варенье",
        ),
    ),
    (
        "напитки",
        (
            "сок", "вода", "минерал", "лимонад", "кола", "пепси",
            "пиво", "вино", "шампанск",
        ),
    ),
    (
        "замороженное",
        ("мороженое", "заморож", "пельмен", "вареник",),
    ),
    (
        "бытовая_химия",
        (
            "мыло", "шампунь", "гель для душа", "стиральн", "порошок стир",
            "кондицион", "туалетн", "салфетк", "полотенце бумаж",
            "губк", "тряпк", "средство для", "жидкость для",
        ),
    ),
    (
        "лекарства",
        (
            "таблетк", "капсул", "сироп", "капли", "мазь", "бальзам",
            # audit 2026-07-18 MINOR: сравнение идёт по title.lower() — ключ
            # "БАД" в верхнем регистре был мёртв; " бад" покрывался подстрокой "бад".
            "витамин", "бад",
            "парацетамол", "ибупрофен", "анальгин", "аспирин", "цитрамон",
            "нурофен", "но-шпа", "спазмалгон", "пенталгин",
            "беталок", "аевит", "компливит", "аспаркам", "фестал",
            "смекта", "омепразол", "лоратадин", "супрастин", "зиртек",
            "физраствор", "лейкопласт", "бинт", "бинтов", "вата мед",
            "шприц", "спирт меди", "перекись",
        ),
    ),
]


def _guess_category(title: str) -> str:
    """Best-effort keyword classification of a shopping item title.

    Returns one of ``SHOPPING_CATEGORIES``. Used when the LLM doesn't
    supply an explicit category (happens with auto-gen from menu —
    previously every ingredient ended up in 'другое'). Dictionary-based
    Russian keywords; case-insensitive substring match. First matching
    category wins (see ``_CATEGORY_KEYWORDS`` ordering for priority).
    Unknown titles fall back to ``DEFAULT_CATEGORY``.
    """
    low = (title or "").lower().strip()
    if not low:
        return DEFAULT_CATEGORY
    for cat, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in low:
                return cat
    return DEFAULT_CATEGORY


@dataclass(slots=True)
class AddItemsResult:
    """#115 by-name outcome for ``add_items_detailed``.

    ``ordered_rows`` is EXACTLY the legacy ``add_items`` return value (created
    rows in legacy path; created + existing-pending + replayed rows in planner
    path) — the thin ``add_items`` delegate returns it so every existing caller
    (miniapp, menu autogen) keeps byte-identical behaviour.

    ``created`` — rows actually INSERTED by THIS call.
    ``duplicates_existing`` — titles of already-PENDING rows that title-dedup
    matched (cross-call UX rule; name = the existing row's title).
    ``duplicates_in_batch`` — input titles repeated within this batch.
    ``replayed`` — planner path only: rows whose INSERT hit ON CONFLICT on the
    per-item operation_id, i.e. an earlier retry of THIS SAME step already
    inserted them (idempotent replay — NOT a user-facing duplicate).
    ``duplicate_item_ids`` — ids of the existing pending rows behind
    ``duplicates_existing`` (planner may reference them in follow-ups)."""

    ordered_rows: list[ShoppingListItem] = field(default_factory=list)
    created: list[ShoppingListItem] = field(default_factory=list)
    duplicates_existing: list[str] = field(default_factory=list)
    duplicates_in_batch: list[str] = field(default_factory=list)
    replayed: list[ShoppingListItem] = field(default_factory=list)
    duplicate_item_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BulkStatusResult:
    """#115 by-name outcome for bulk status flips (mark_bought / remove_items)
    so the live voice can confirm what happened.

    ``affected`` — rows actually mutated (names via ``.title``).
    ``not_eligible`` — titles of OWNED rows skipped because their status wasn't
    in ``only_from`` (name knowable → reported by name).
    ``not_found_count`` — requested unique ids with no owned row (foreign or
    nonexistent → no name knowable, count only)."""

    affected: list[ShoppingListItem] = field(default_factory=list)
    not_eligible: list[str] = field(default_factory=list)
    not_found_count: int = 0


class HousewifeShoppingService:
    """Service facade for the shopping list.

    Methods are scoped by (tenant_id, user_id) — the only thing stored
    per-user; no multi-user shared list in v1 (that's v2 with multi-user
    tenants). LLM tools wrap these calls; Mini App endpoints call them
    directly.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_items(
        self,
        *,
        tenant_id: str,
        user_id: str,
        items: list[ShoppingItemInput] | list[dict[str, Any]],
    ) -> list[ShoppingListItem]:
        """Thin back-compat delegate — EXACT legacy return shape (see
        ``add_items_detailed.ordered_rows``). Existing callers (miniapp, menu
        autogen, chat) keep byte-identical behaviour."""
        return self.add_items_detailed(
            tenant_id=tenant_id, user_id=user_id, items=items
        ).ordered_rows

    def add_items_detailed(
        self,
        *,
        tenant_id: str,
        user_id: str,
        items: list[ShoppingItemInput] | list[dict[str, Any]],
    ) -> AddItemsResult:
        """Batch-insert items with ``status='pending'``. #115: returns the full
        by-name outcome (created / duplicates_existing / duplicates_in_batch /
        replayed) plus ``ordered_rows`` — the exact legacy return value.

        Empty title is rejected at the item level (returns with a warning
        log) — we don't want silently-created empty placeholder rows.

        **Idempotency branching (Sub-A12 Phase E PR-2a)**:

        - ``current_tool_runtime() is None`` (legacy / chat path — THE
          LIVE PATH TODAY): behaviour is byte-for-byte identical to the
          pre-PR code. No operation_id, no normalized_title_hash, no
          emit_event.  This is the only branch that runs outside the
          planner.

        - ``current_tool_runtime() is not None`` (planner path): each
          inserted row gets a stable PER-ITEM operation_id (hash of
          execution_id + step_id + "create" + "shopping_list_item" +
          normalize_for_dedup(title)) via ``INSERT ... ON CONFLICT
          (tenant_id, user_id, operation_id) DO NOTHING`` — that is the
          ROW's uniqueness key (a step inserting N items needs N keys).
          Exactly ONE audit event is emitted per step, keyed by the
          PER-STEP ``ctx.operation_id`` (the ledger id recovery probes),
          in the same transaction — even when every item was
          title-deduped — so no durable-write step is invisible to crash
          recovery.  A re-run with the same ctx produces the same
          per-item op_ids (ON CONFLICT no-ops) and the same
          ctx.operation_id (emit_event dedups) — no duplicate row, no
          duplicate audit.

        Return contract: same shape in both branches — a list of
        ``ShoppingListItem`` rows.  In the legacy path that is exactly
        the newly-created rows.  In the planner path it additionally
        includes already-pending rows that title-dedup matched (fetched
        from the pending map) so the planner sees a valid id; a row that
        ON-CONFLICT-skipped is fetched by its per-item operation_id.
        Note: ``len(rows)`` therefore is NOT a reliable "newly added"
        count in the planner path — a wrapper that needs a user-facing
        count must compute it separately (PR-2b concern).
        """
        normalised: list[ShoppingItemInput] = []
        for raw in items or []:
            if isinstance(raw, ShoppingItemInput):
                normalised.append(raw)
                continue
            if not isinstance(raw, dict):
                continue
            title = (raw.get("title") or "").strip()
            if not title:
                continue
            normalised.append(
                ShoppingItemInput(
                    title=title[:500],
                    quantity_text=(raw.get("quantity_text") or "").strip()[:64]
                    or None,
                    category=raw.get("category"),
                    source_recipe_id=raw.get("source_recipe_id"),
                )
            )

        ctx = current_tool_runtime()

        if ctx is None:
            # ------------------------------------------------------------------
            # LEGACY PATH — unchanged from pre-PR.  Must remain byte-for-byte
            # identical so the live chat path is unaffected.
            # ------------------------------------------------------------------
            # Dedup: skip items whose normalised title already exists as
            # a PENDING row for this (tenant, user). Prevents the "Friday
            # menu re-adds Thursday's ingredients" duplicate flood. Bought
            # / cancelled rows don't count — if user bought 'молоко'
            # yesterday, re-adding today is a legitimate new need.
            # #115: keep a SNAPSHOT map of the pre-existing pending rows so a
            # cross-call duplicate (already on the list) is distinguishable from
            # an in-batch repeat — both were a single opaque `continue` before.
            pending_by_title_legacy: dict[str, ShoppingListItem] = {}
            for existing in self._list_by_status(
                tenant_id, user_id, ("pending",)
            ):
                pending_by_title_legacy.setdefault(
                    _normalise_title(existing.title), existing
                )
            existing_titles: set[str] = set(pending_by_title_legacy.keys())

            result = AddItemsResult()
            now = _utcnow()
            rows: list[ShoppingListItem] = []
            for item in normalised:
                norm = _normalise_title(item.title)
                if norm in existing_titles:
                    # Already on the list (cross-call) or repeated in this batch.
                    pre = pending_by_title_legacy.get(norm)
                    if pre is not None:
                        result.duplicates_existing.append(pre.title)
                        result.duplicate_item_ids.append(pre.id)
                    else:
                        result.duplicates_in_batch.append(item.title)
                    continue
                # Track this one so a later item within the same batch
                # with the same normalised title also gets deduped.
                existing_titles.add(norm)
                # When the caller supplies an explicit category, respect it
                # (LLM in chat may know context the heuristic doesn't).
                # When it's missing — common for generate_shopping_from_menu
                # which passes None — guess from the title so the item
                # lands in a real bucket rather than piling into "другое".
                if item.category:
                    resolved_category = _coerce_category(item.category)
                else:
                    resolved_category = _guess_category(item.title)
                row = ShoppingListItem(
                    id=f"sh_{uuid4().hex[:24]}",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    title=item.title,
                    quantity_text=item.quantity_text,
                    category=resolved_category,
                    status="pending",
                    source_recipe_id=item.source_recipe_id,
                    added_at=now,
                    updated_at=now,
                )
                self.session.add(row)
                rows.append(row)

            if rows:
                self.session.commit()
            result.created = rows
            result.ordered_rows = rows  # legacy return shape: created only
            return result

        # ------------------------------------------------------------------
        # PLANNER PATH — option-(a) idempotency adapter (Sub-A12 Phase E).
        # ------------------------------------------------------------------
        # Fail-closed wiring guard: the runtime context's tenant MUST match the
        # tenant this call writes for.  A mismatch means the contextvar leaked
        # across a tenant boundary (a wiring bug) — refuse rather than write
        # cross-tenant rows.  ctx.user_id is intentionally NOT asserted: the
        # executor binds user_id=None today; the authoritative user scope is
        # the explicit `user_id` argument, used for BOTH the row and the hash.
        if ctx.tenant_id and ctx.tenant_id != tenant_id:
            raise ValueError(
                "add_items planner path: ctx.tenant_id="
                f"{ctx.tenant_id!r} != tenant_id={tenant_id!r} — "
                "ToolRuntimeContext leaked across a tenant boundary."
            )

        # Title-dedup is an AUTHORITATIVE business rule (NOT a "fast
        # non-authoritative pre-filter"): a title already PENDING for this
        # (tenant, user) is not re-added — the legacy UX ("Friday menu doesn't
        # re-add Thursday's still-pending ingredients").  This is independent
        # of, and complementary to, the crash-safety idempotency:
        #   * title-dedup   — cross-operation UX rule (don't flood duplicates)
        #   * operation_id  — same-operation retry idempotency (DB ON CONFLICT)
        # Build a normalised-title -> existing pending row map ONCE so the loop
        # is O(items + pending), not O(items x pending).
        pending_by_title: dict[str, ShoppingListItem] = {}
        for existing in self._list_by_status(tenant_id, user_id, ("pending",)):
            pending_by_title.setdefault(_normalise_title(existing.title), existing)

        now = _utcnow()

        # Choose the dialect-aware insert constructor.
        dialect_name = self.session.bind.dialect.name  # type: ignore[union-attr]
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        else:
            # SQLite (tests) + any other dialect with SQLAlchemy upsert support.
            from sqlalchemy.dialects.sqlite import insert as _insert  # type: ignore[no-redef]

        result = AddItemsResult()
        rows_p: list[ShoppingListItem] = []
        seen: set[str] = set(pending_by_title.keys())
        for item in normalised:
            norm = _normalise_title(item.title)
            # Per-ITEM operation_id — the ROW's idempotency key for the UNIQUE
            # (tenant, user, operation_id) index.  A step inserting N items
            # needs N distinct keys, so this is derived per item (includes the
            # normalised title).  It is DELIBERATELY different from the per-STEP
            # ctx.operation_id used for the recovery-probe audit event below
            # (a single global op_id on one row is not sufficient for multi-row
            # — plan item #1).  Computed BEFORE the title-dedup decision (#115
            # Codex CRITICAL): a pending row carrying THIS op_id was inserted by
            # an earlier retry of THIS SAME step — that's an idempotent REPLAY,
            # not a user-facing duplicate, and must not degrade to `empty`
            # (which sits outside committed_statuses and would make recovery
            # treat the durable write as never committed).
            op_id = compute_operation_id_create(
                plan_id=ctx.execution_id,
                step_id=ctx.step_id,
                action="create",
                entity_type="shopping_list_item",
                logical_key=normalize_for_dedup(item.title),
            )
            # Already pending (cross-call) OR already handled earlier in this
            # batch (intra-batch dedup, mirrors the legacy single-set logic).
            if norm in seen:
                existing_row = pending_by_title.get(norm)
                if existing_row is not None:
                    # Return the existing row so the planner sees a valid id.
                    # The per-step audit footprint below is emitted regardless,
                    # so a fully-deduped step is still visible to recovery.
                    rows_p.append(existing_row)
                    if existing_row.operation_id == op_id:
                        # Same-step retry: row exists because WE inserted it.
                        result.replayed.append(existing_row)  # #115
                    else:
                        result.duplicates_existing.append(existing_row.title)  # #115
                        result.duplicate_item_ids.append(existing_row.id)
                else:
                    result.duplicates_in_batch.append(item.title)  # #115
                continue
            seen.add(norm)

            if item.category:
                resolved_category = _coerce_category(item.category)
            else:
                resolved_category = _guess_category(item.title)
            # Hash scoped to the AUTHORITATIVE user (the `user_id` argument),
            # never ctx.user_id (the executor binds that to None today, which
            # would scope the hash to a blank user and break future lookups +
            # leak cross-user correlation within a tenant — Codex A/B R1).
            nhash = compute_normalized_title_hash(
                item.title,
                entity_type="shopping_list_item",
                tenant_id=tenant_id,
                user_id=user_id,
            )
            row_id = f"sh_{uuid4().hex[:24]}"

            stmt = (
                _insert(ShoppingListItem)
                .values(
                    id=row_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    title=item.title,
                    quantity_text=item.quantity_text,
                    category=resolved_category,
                    status="pending",
                    source_recipe_id=item.source_recipe_id,
                    added_at=now,
                    updated_at=now,
                    operation_id=op_id,
                    normalized_title_hash=nhash,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "user_id", "operation_id"]
                )
            )
            insert_res = self.session.execute(stmt)
            # #115: rowcount==1 → THIS call inserted the row (created);
            # rowcount==0 → ON CONFLICT fired (an earlier retry of this same
            # step already inserted it) → idempotent REPLAY, not a user-facing
            # duplicate. Works on both postgresql and sqlite single-row inserts.
            was_inserted = bool(getattr(insert_res, "rowcount", 0) == 1)

            # Fetch the row by operation_id — covers both "inserted just now"
            # and "ON CONFLICT fired because a concurrent racer / earlier retry
            # of THIS step already inserted the same op_id".
            actual_row = (
                self.session.query(ShoppingListItem)
                .filter(
                    ShoppingListItem.tenant_id == tenant_id,
                    ShoppingListItem.user_id == user_id,
                    ShoppingListItem.operation_id == op_id,
                )
                .one_or_none()
            )
            if actual_row is None:
                # Should not happen: either insert landed or conflict existed.
                logger.error(
                    "add_items planner path: row not found after insert "
                    "for op_id=%s (tenant=%s user=%s title=%r)",
                    op_id, tenant_id, user_id, item.title,
                )
                continue
            rows_p.append(actual_row)
            if was_inserted:
                result.created.append(actual_row)  # #115
            else:
                result.replayed.append(actual_row)  # #115 same-operation replay

        # ------------------------------------------------------------------
        # ONE per-step audit footprint — the crash-recovery PROBE SURFACE.
        # ------------------------------------------------------------------
        # Keyed by ctx.operation_id (the LEDGER operation_id the executor
        # persisted BEFORE execution and that recovery.probe_operation() looks
        # up — recovery "never recomputes from args", plan item #1).  Emitted
        # ONCE per step, in the SAME transaction as the row writes, whenever the
        # step attempted a durable write (normalised non-empty) — even if every
        # item was title-deduped — so NO durable-write step is invisible to
        # crash recovery.  The per-item op_ids are the rows' uniqueness keys,
        # NOT the probe surface (recovery cannot reconstruct per-item titles).
        #
        # SAVEPOINT per emit_event's contract: a concurrent racer emitting the
        # same ctx.operation_id raises IntegrityError on flush; begin_nested()
        # rolls the savepoint back (parent txn survives) and we treat the
        # racer's row as the winner.
        if normalised:
            # #163 Фаза 3d-B: react-провенанс ТОЛЬКО для react-ctx (origin=="react"); на легаси
            # plan-execute (origin!=react) caused_by=None — поведение как до 3d-B (Codex medium R1 MAJOR:
            # ToolRuntimeContext биндит и planner/executor → иначе ложный react-тег у легаси-покупок).
            react_cb = (
                {"source": "react", "turn_key": ctx.turn_key,
                 "thread_id": getattr(ctx, "thread_id", None),
                 "channel": getattr(ctx, "channel", None), "tool_name": ctx.tool_name}
                if getattr(ctx, "origin", None) == "react" else None)
            try:
                with self.session.begin_nested():
                    emit_event(
                        self.session,
                        operation_id=ctx.operation_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        source="среда",
                        entity_type="shopping_list_item",
                        entity_id=None,
                        action="created",
                        payload=None,
                        caused_by=react_cb,
                    )
            except IntegrityError:
                # Concurrent racer already wrote this step's audit row; the
                # savepoint is rolled back and the parent txn is intact.
                logger.info(
                    "add_items planner path: audit row for op_id=%s already "
                    "written by a concurrent racer; continuing.",
                    ctx.operation_id,
                )
            self.session.commit()
        result.ordered_rows = rows_p  # legacy planner return shape
        return result

    def mark_bought(
        self, *, tenant_id: str, user_id: str, ids: list[str]
    ) -> int:
        """Flip ``status='bought'`` for the listed ids. Returns the
        count of rows actually updated — ids we didn't own / didn't
        exist are silently skipped (no LLM-visible error; the LLM already
        sees the list via ``list_pending``)."""
        return len(
            self.mark_bought_detailed(
                tenant_id=tenant_id, user_id=user_id, ids=ids
            ).affected
        )

    def mark_bought_detailed(
        self, *, tenant_id: str, user_id: str, ids: list[str]
    ) -> "BulkStatusResult":
        """#115: like ``mark_bought`` but returns the by-name outcome so the
        live voice can confirm WHAT was marked vs skipped."""
        return self._bulk_update_status_detailed(
            tenant_id=tenant_id,
            user_id=user_id,
            ids=ids,
            new_status="bought",
            only_from=("pending",),
        )

    def remove_items(
        self, *, tenant_id: str, user_id: str, ids: list[str]
    ) -> int:
        """Flip ``status='cancelled'``. Hides from ``list_pending``;
        row stays for history."""
        return len(
            self.remove_items_detailed(
                tenant_id=tenant_id, user_id=user_id, ids=ids
            ).affected
        )

    def remove_items_detailed(
        self, *, tenant_id: str, user_id: str, ids: list[str]
    ) -> "BulkStatusResult":
        """#115: like ``remove_items`` but returns the by-name outcome."""
        return self._bulk_update_status_detailed(
            tenant_id=tenant_id,
            user_id=user_id,
            ids=ids,
            new_status="cancelled",
            only_from=("pending", "bought"),
        )

    def delete_by_source_recipe(
        self, *, tenant_id: str, user_id: str, recipe_id: str
    ) -> int:
        """Hard-delete every pending/bought shopping item that was
        added from the given recipe (``source_recipe_id`` match).

        Used by the "Подобрать заново" menu-cell action: when the user
        swaps out a dish, the ingredients auto-generated from the OLD
        dish should disappear from the shopping list — otherwise the
        list keeps stale items that the user won't buy. Cancelled-status
        items are also removed so the recipe-tied history is clean.

        Returns the number of rows actually deleted. Cross-tenant safe.
        """
        if not recipe_id:
            return 0
        q = self.session.query(ShoppingListItem).filter(
            ShoppingListItem.tenant_id == tenant_id,
            ShoppingListItem.user_id == user_id,
            ShoppingListItem.source_recipe_id == recipe_id,
        )
        rows = q.all()
        if not rows:
            return 0
        for row in rows:
            self.session.delete(row)
        self.session.commit()
        return len(rows)

    def update_item(
        self,
        *,
        tenant_id: str,
        user_id: str,
        item_id: str,
        title: str | None = None,
        quantity_text: str | None = None,
        category: str | None = None,
    ) -> ShoppingListItem | None:
        """Partial update of a single shopping item — lets the LLM
        re-categorise / rename / re-quantify with ONE tool call
        instead of the burn-your-budget delete+add cycle.

        Any arg passed as None leaves that field untouched. Pass an
        explicit empty string to clear (only makes sense for
        quantity_text). Returns the updated row, or None if the item
        doesn't exist or belongs to another tenant.
        """
        detailed = self.update_item_detailed(
            tenant_id=tenant_id, user_id=user_id, item_id=item_id,
            title=title, quantity_text=quantity_text, category=category,
        )
        return None if detailed is None else detailed[0]

    def update_item_detailed(
        self,
        *,
        tenant_id: str,
        user_id: str,
        item_id: str,
        title: str | None = None,
        quantity_text: str | None = None,
        category: str | None = None,
    ) -> tuple[ShoppingListItem, str] | None:
        """#115: like ``update_item`` but returns ``(row, previous_title)`` —
        the title captured BEFORE mutation, so the voice can say
        «переименовала X → Y». ``None`` if not found / cross-tenant."""
        row = (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.id == item_id,
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
            )
            .one_or_none()
        )
        if row is None:
            return None
        previous_title = row.title
        if title is not None:
            clean = (title or "").strip()
            if clean:
                row.title = clean[:500]
        if quantity_text is not None:
            q = (quantity_text or "").strip()[:64]
            row.quantity_text = q or None
        if category is not None:
            row.category = _coerce_category(category)
        row.updated_at = _utcnow()
        self.session.commit()
        return row, previous_title

    def update_items_category(
        self,
        *,
        tenant_id: str,
        user_id: str,
        ids: list[str],
        category: str,
    ) -> int:
        """Bulk re-assign category for a set of items. Rows owned by
        other tenants / users are silently skipped (cross-tenant safe).
        Returns the count of rows actually updated.

        Keeps the LLM's tool-budget down — one call instead of one
        per item via ``update_item`` or a remove+add cycle.
        """
        outcome, _cat = self.update_items_category_detailed(
            tenant_id=tenant_id, user_id=user_id, ids=ids, category=category
        )
        return len(outcome.affected)

    def update_items_category_detailed(
        self,
        *,
        tenant_id: str,
        user_id: str,
        ids: list[str],
        category: str,
    ) -> tuple["BulkStatusResult", str]:
        """#115: like ``update_items_category`` but returns
        ``(by-name outcome, coerced_category)`` — the second element is the
        category actually stored (``_coerce_category`` lowercases/maps/caps), so
        the wire/display never advertises a bucket different from the DB row
        (Codex #115). Re-categorisation has no status predicate (any owned row
        qualifies), so ``not_eligible`` is empty by construction; unknown/
        foreign ids surface as ``not_found_count``. Requested ids deduped."""
        result = BulkStatusResult()
        new_cat = _coerce_category(category)
        if not ids:
            return result, new_cat
        # Dedup WITHOUT dropping falsey tokens — a blank/invalid requested id
        # must still count as not_found (Codex #115: filtering it out
        # under-reported failures as a silent "Готово.").
        unique_ids = list(dict.fromkeys(ids))
        rows = (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
                ShoppingListItem.id.in_(unique_ids),
            )
            .all()
        )
        result.not_found_count = len(unique_ids) - len(rows)
        now = _utcnow()
        for row in rows:
            row.category = new_cat
            row.updated_at = now
            result.affected.append(row)
        if result.affected:
            self.session.commit()
        return result, new_cat

    def clear_pending(self, *, tenant_id: str, user_id: str) -> int:
        """Mark every pending item as cancelled — the "Очистить всё"
        button on the Mini App shopping screen. Bought items are
        preserved (they're part of history, not clutter) and
        already-cancelled rows are left alone. Returns the count of
        rows actually moved.
        """
        q = self.session.query(ShoppingListItem).filter(
            ShoppingListItem.tenant_id == tenant_id,
            ShoppingListItem.user_id == user_id,
            ShoppingListItem.status == "pending",
        )
        now = _utcnow()
        updated = 0
        for row in q.all():
            row.status = "cancelled"
            row.updated_at = now
            updated += 1
        if updated:
            self.session.commit()
        return updated

    def clear_bought(self, *, tenant_id: str, user_id: str) -> int:
        """Cancel everything currently in ``bought`` state. Used as a
        bulk housekeeping operation — "уже всё закупил, убери из списка
        что было куплено". Returns count."""
        q = (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
                ShoppingListItem.status == "bought",
            )
        )
        now = _utcnow()
        updated = 0
        for row in q.all():
            row.status = "cancelled"
            row.updated_at = now
            updated += 1
        if updated:
            self.session.commit()
        return updated

    def _bulk_update_status(
        self,
        *,
        tenant_id: str,
        user_id: str,
        ids: list[str],
        new_status: str,
        only_from: tuple[str, ...],
    ) -> int:
        return len(
            self._bulk_update_status_detailed(
                tenant_id=tenant_id,
                user_id=user_id,
                ids=ids,
                new_status=new_status,
                only_from=only_from,
            ).affected
        )

    def _bulk_update_status_detailed(
        self,
        *,
        tenant_id: str,
        user_id: str,
        ids: list[str],
        new_status: str,
        only_from: tuple[str, ...],
    ) -> "BulkStatusResult":
        """#115: single SELECT over the owned requested rows WITHOUT the status
        predicate, then split in code — so the affected NAMES come from exactly
        the rows we mutate (same query, same transaction), owned-but-wrong-status
        rows surface by name (``not_eligible``), and unknown/foreign ids surface
        as a count only (no name knowable). Requested ids are de-duplicated."""
        result = BulkStatusResult()
        if not ids:
            return result
        if new_status not in SHOPPING_STATUSES:
            raise ValueError(f"bad status: {new_status!r}")
        # Dedup WITHOUT dropping falsey tokens — a blank/invalid requested id
        # must still count as not_found (Codex #115: filtering it out
        # under-reported failures as a silent "Готово.").
        unique_ids = list(dict.fromkeys(ids))
        rows = (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
                ShoppingListItem.id.in_(unique_ids),
            )
            .all()
        )
        result.not_found_count = len(unique_ids) - len(rows)
        now = _utcnow()
        for row in rows:
            if row.status in only_from:
                row.status = new_status
                row.updated_at = now
                result.affected.append(row)
            else:
                result.not_eligible.append(row.title)
        if result.affected:
            self.session.commit()
        return result

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_pending(
        self, *, tenant_id: str, user_id: str
    ) -> list[ShoppingListItem]:
        """All pending items, ordered by category (fixed taxonomy order)
        then added_at. Mini App uses this same order to render."""
        return self._list_by_status(tenant_id, user_id, ("pending",))

    def list_by_status(
        self,
        *,
        tenant_id: str,
        user_id: str,
        statuses: tuple[str, ...] = ("pending",),
    ) -> list[ShoppingListItem]:
        return self._list_by_status(tenant_id, user_id, statuses)

    def _list_by_status(
        self,
        tenant_id: str,
        user_id: str,
        statuses: tuple[str, ...],
    ) -> list[ShoppingListItem]:
        rows = (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
                ShoppingListItem.status.in_(statuses),
            )
            .order_by(
                ShoppingListItem.category.asc(),
                ShoppingListItem.added_at.asc(),
            )
            .all()
        )
        # Resort in-memory so category order matches the fixed taxonomy
        # (SQL alphabetical doesn't produce "молочные → мясо → ..." in a
        # useful way; the shopper wants the layout that matches store
        # sections).
        order_map = {c: i for i, c in enumerate(SHOPPING_CATEGORIES)}
        rows.sort(
            key=lambda r: (
                order_map.get(r.category, len(order_map)),
                r.added_at,
            )
        )
        return rows

    def count_pending(self, *, tenant_id: str, user_id: str) -> int:
        """Cheap count for the Mini App dashboard counter."""
        return (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
                ShoppingListItem.status == "pending",
            )
            .count()
        )
