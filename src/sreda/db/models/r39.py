"""R-39 persistent journal table — hybrid pipeline turn metadata.

Хранит результаты R-39 ходов параллельно с `AgentRun.result_json`
(который полностью перезаписывается graph'ом). Используется:

- ``correction_resolver`` ищет prior R-39 SUCCESS-ходы того же thread'а
  через JOIN на ``agent_runs.thread_id``.
- Shadow mode пишет ``mode='shadow'`` без реальных side effects.
- Post-canary analytics: распределение `plan_kind`, доля unbacked,
  `side_effects_count` distribution.

См. ``plans/r39-integration-final.md`` §«Architecture / persist».
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class R39RunJournal(Base):
    __tablename__ = "r39_run_journal"

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # 'live' | 'shadow'
    plan_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journal_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_pending: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_unbacked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    side_effects_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
