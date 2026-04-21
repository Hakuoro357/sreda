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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.housewife_food import (
    SHOPPING_CATEGORIES,
    SHOPPING_STATUSES,
    ShoppingListItem,
)

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


def _coerce_category(raw: str | None) -> str:
    """Map an LLM-supplied category string to the fixed taxonomy.

    Accepts exact matches (case-insensitive) or falls through to
    ``DEFAULT_CATEGORY`` — the LLM occasionally invents novel buckets
    ("специи", "детское питание") and we don't want those to leak into
    the DB as uncategorised mystery values.
    """
    if not raw:
        return DEFAULT_CATEGORY
    candidate = raw.strip().lower()
    for allowed in SHOPPING_CATEGORIES:
        if candidate == allowed:
            return allowed
    return DEFAULT_CATEGORY


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
        """Batch-insert items with ``status='pending'``. Returns the
        created rows in order.

        Empty title is rejected at the item level (returns with a warning
        log) — we don't want silently-created empty placeholder rows."""
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

        now = _utcnow()
        rows: list[ShoppingListItem] = []
        for item in normalised:
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
        return rows

    def mark_bought(
        self, *, tenant_id: str, user_id: str, ids: list[str]
    ) -> int:
        """Flip ``status='bought'`` for the listed ids. Returns the
        count of rows actually updated — ids we didn't own / didn't
        exist are silently skipped (no LLM-visible error; the LLM already
        sees the list via ``list_pending``)."""
        return self._bulk_update_status(
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
        return self._bulk_update_status(
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
        if not ids:
            return 0
        if new_status not in SHOPPING_STATUSES:
            raise ValueError(f"bad status: {new_status!r}")
        q = (
            self.session.query(ShoppingListItem)
            .filter(
                ShoppingListItem.tenant_id == tenant_id,
                ShoppingListItem.user_id == user_id,
                ShoppingListItem.id.in_(ids),
                ShoppingListItem.status.in_(only_from),
            )
        )
        now = _utcnow()
        updated = 0
        for row in q.all():
            row.status = new_status
            row.updated_at = now
            updated += 1
        if updated:
            self.session.commit()
        return updated

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
