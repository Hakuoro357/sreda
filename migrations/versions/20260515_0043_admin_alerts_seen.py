"""admin_alerts_seen: dedup table for R-28 admin alerting service

R-28 (vex-assistant#37): observability gap discovered после R-27 incident —
mimo reasoning_content bug active 6 days без notifications. Boris:
«любой ответ от llm с ошибкой должен вызывать алертинг».

Этот migration создаёт persistent dedup state для admin alerts:
- LLM fallback engagement (per provider/exception type)
- Tool dispatch errors (error:/skipped: prefixes)
- Provider 5xx / timeouts
- Other CRITICAL classes

Каждый alert keyed by stable `dedupe_key`. Re-fire rate limited via
``last_sent_at`` — default 5 min, P0 severity 60s.

Survives restarts (persistent table) — иначе при service restart
тот же exception class re-alert'нется (recovery storm pattern).

Schema:
- ``dedupe_key`` PRIMARY KEY — stable id like ``"llm_fallback:BadRequestError:housewife_assistant"``
- ``severity`` — `P0` / `P1` / `P2` / `INFO`
- ``title`` — first occurrence's title (for audit)
- ``first_seen_at`` / ``last_sent_at`` — for rate-limit + freshness
- ``occurrence_count`` — total fires since first seen

Retention: NO automatic cleanup. Table grows О(unique dedupe_keys),
typically <100 rows. Manual cleanup via SQL если нужно.

Revision ID: 20260515_0043
Revises: 20260507_0042
Create Date: 2026-05-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260515_0043"
down_revision = "20260507_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_alerts_seen",
        sa.Column("dedupe_key", sa.String(256), primary_key=True),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "occurrence_count",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
    )
    op.create_index(
        "ix_admin_alerts_seen_last_sent_at",
        "admin_alerts_seen",
        ["last_sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_alerts_seen_last_sent_at", "admin_alerts_seen")
    op.drop_table("admin_alerts_seen")
