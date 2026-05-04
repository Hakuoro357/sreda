"""add embedding_json + embedding_model to checklist_items and family_reminders

Phase 1 of recall-broadcast plan (`plans/archive/recall-broadcast-fanout/...`):
extend recall_memory to fan out across structured stores so a fact dictated
into a checklist or reminder is surfaced when the user later asks "помнишь X".

Two new nullable columns per table:

  * ``embedding_json TEXT``  — JSON-encoded list[float] (e5-large 1024d).
    Nullable so existing rows stay readable until backfill catches up.
  * ``embedding_model VARCHAR(32)`` — model identifier (``e5-large``,
    future-proofing for vendor swaps).  ``embedding_dim`` redundant with
    model name and intentionally NOT added (per qwen R2 review).

No data backfill in this migration — done by ``scripts/backfill_structured_embeddings.py``
in Phase 2 with API rate-limit awareness.

Revision ID: 20260504_0037
Revises: 20260502_0036
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260504_0037"
down_revision = "20260502_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # checklist_items
    op.add_column(
        "checklist_items",
        sa.Column("embedding_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "checklist_items",
        sa.Column("embedding_model", sa.String(length=32), nullable=True),
    )

    # family_reminders
    op.add_column(
        "family_reminders",
        sa.Column("embedding_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "family_reminders",
        sa.Column("embedding_model", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("family_reminders", "embedding_model")
    op.drop_column("family_reminders", "embedding_json")
    op.drop_column("checklist_items", "embedding_model")
    op.drop_column("checklist_items", "embedding_json")
