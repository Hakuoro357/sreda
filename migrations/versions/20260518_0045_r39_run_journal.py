"""r39_run_journal: persistent журнал R-39 hybrid pipeline turns

R-39 (vex-assistant#46): новый анти-confab pipeline для conversational
chat (планировщик + исполнитель + детерминированная первая строка +
композитор живой фразы + post-hoc audit).

Эта миграция создаёт persistent журнал R-39 ходов для:

- **Correction resolver** (Day 1-5 contracts): следующий «нет, не на 14
  а на 15» находит target reminder через предыдущий R-39 SUCCESS-ход
  того же thread'а.
- **Shadow mode сравнение**: shadow run пишется отдельной row (mode='shadow'),
  с тем же run_id что у соответствующего live legacy run'а; offline
  анализ сравнивает «что R-39 хотел сделать» vs «что legacy сделал».
- **Audit / analytics**: post-canary анализ — сколько turn'ов прошли
  через R-39, сколько L4 unbacked claims детектировано, какие plan_kind
  преобладают (action / no_action / clarification).

graph.py перезаписывает AgentRun.result_json после handler'а полностью
(outbox_message_ids / outbox_statuses / reply_count) — поэтому R-39
journal в нём не может выжить. Этой отдельной таблицей мы обходим
ограничение без модификации graph'а.

Trade-off (option B в плане R-39 integration R7): tools (housewife
services) commit'ят сами внутри. Если между committed tool.invoke и
этой R39RunJournal row упадёт persist — side effect остался, journal
row — нет. Correction resolver имеет DB fallback на FamilyReminder.status
для этих edge cases.

Revision ID: 20260518_0045
Revises: 20260515_0044
Create Date: 2026-05-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260518_0045"
down_revision = "20260515_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "r39_run_journal",
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),  # 'live' | 'shadow'
        sa.Column("plan_kind", sa.String(32), nullable=True),
        sa.Column("journal_json", sa.Text, nullable=True),
        sa.Column("correction_pending", sa.Text, nullable=True),
        sa.Column(
            "audit_unbacked",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "side_effects_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_r39_run_journal_tenant_created",
        "r39_run_journal",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_r39_run_journal_tenant_created", "r39_run_journal")
    op.drop_table("r39_run_journal")
