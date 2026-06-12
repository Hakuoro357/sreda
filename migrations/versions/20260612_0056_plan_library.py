"""#135: библиотека эталонных планов (срез 1) — plan_library_entries.

Аддитивная миграция: одна таблица + индексы. Сырьё/PII не хранится
(только redacted-форма) — план #135 final (R4, оба Codex NSC).
"""

import sqlalchemy as sa
from alembic import op

revision = "20260612_0056"
down_revision = "20260611_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_library_entries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("case_id", sa.String(64), nullable=True),
        sa.Column("case_variant", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="candidate"),
        sa.Column("source", sa.String(16), nullable=False,
                  server_default="prod_turn"),
        sa.Column("form_json", sa.Text(), nullable=False),
        sa.Column("form_tags", sa.Text(), nullable=False,
                  server_default="[]"),
        sa.Column("outcome_json", sa.Text(), nullable=False,
                  server_default="{}"),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("expected_form_json", sa.Text(), nullable=True),
        sa.Column("registry_version", sa.String(64), nullable=True),
        sa.Column("composer_snapshot_hash", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_library_tenant_status_created",
                    "plan_library_entries",
                    ["tenant_id", "status", "created_at"])
    op.create_index("ux_plan_library_tenant_run", "plan_library_entries",
                    ["tenant_id", "run_id"], unique=True,
                    postgresql_where=sa.text("run_id IS NOT NULL"),
                    sqlite_where=sa.text("run_id IS NOT NULL"))
    op.create_index("ux_plan_library_tenant_case", "plan_library_entries",
                    ["tenant_id", "case_id", "case_variant"], unique=True,
                    postgresql_where=sa.text("case_id IS NOT NULL"),
                    sqlite_where=sa.text("case_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ux_plan_library_tenant_case",
                  table_name="plan_library_entries")
    op.drop_index("ux_plan_library_tenant_run",
                  table_name="plan_library_entries")
    op.drop_index("ix_plan_library_tenant_status_created",
                  table_name="plan_library_entries")
    op.drop_table("plan_library_entries")
