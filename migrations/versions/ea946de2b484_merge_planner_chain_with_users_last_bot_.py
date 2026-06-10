"""merge planner chain with users_last_bot_key

Revision ID: ea946de2b484
Revises: 20260601_0054, 20260606_0054
Create Date: 2026-06-10 16:54:16.894910
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision = 'ea946de2b484'
down_revision = ('20260601_0054', '20260606_0054')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
