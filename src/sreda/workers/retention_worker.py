"""Runtime retention worker (152-ФЗ Часть 2, 2026-04-28).

Crons-less scheduler. Каждый tick `job_runner` спрашивает worker'а
«пора ли запустить cleanup?». Worker отвечает по простой модели:
не чаще одного раза в 24 часа. State хранится в файле
`/tmp/sreda-retention-state.json` (или другом пути из настроек) —
ISO timestamp последнего успешного прогона.

Почему не time-of-day cron-style:
- Job-runner — полл-луп, не cron. Добавлять отдельный scheduler
  ради одной задачи — усложнение.
- Cleanup идемпотентен: безопасен при повторных вызовах.
- «Не чаще раз в 24ч» достаточно для compliance с retention TTL'ами
  (30/60/90 дней). Окно для удаления rows — несущественно.

Логирование — через стандартный logger `sreda.retention` + одну
строку INFO с timestamp'ом и количеством удалённых строк (директива
«логи без даты бессмысленны»).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from sreda.maintenance.retention_cleanup import (
    RetentionCleanupResult,
    cleanup_runtime_retention,
)


logger = logging.getLogger("sreda.retention")


# Default state file path. Может быть переопределён через
# SREDA_RETENTION_STATE_FILE env var в settings (для тестов / контейнеров).
DEFAULT_STATE_FILE = "/tmp/sreda-retention-state.json"

# Интервал между прогонами. 24 часа — стандарт для retention.
DEFAULT_INTERVAL = timedelta(hours=24)


class RetentionWorker:
    """Запускает `cleanup_runtime_retention` не чаще раз в `interval`."""

    def __init__(
        self,
        session: Session,
        *,
        state_file: str | None = None,
        interval: timedelta = DEFAULT_INTERVAL,
    ) -> None:
        self.session = session
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.interval = interval

    async def process_pending(self) -> int:
        """Главный entry point. Вызывается из job_runner.

        Returns: количество удалённых строк (0 если ещё рано).
        """
        now = datetime.now(timezone.utc)
        if not self._should_run(now):
            return 0
        try:
            result = cleanup_runtime_retention(self.session, now=now)
        except Exception:  # noqa: BLE001 — никогда не убиваем job_runner
            logger.exception("retention cleanup failed")
            return 0
        self._record_run(now, result)
        self._log_result(now, result)
        return result.total

    def _should_run(self, now: datetime) -> bool:
        """True если последний прогон был >= `interval` назад
        (или state-файл отсутствует / некорректен)."""
        last = self._read_last_run()
        if last is None:
            return True
        return (now - last) >= self.interval

    def _read_last_run(self) -> datetime | None:
        if not self.state_file.exists():
            return None
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            ts = data.get("last_run_at")
            if not isinstance(ts, str):
                return None
            return datetime.fromisoformat(ts)
        except (json.JSONDecodeError, ValueError, OSError):
            # Corrupt state file — игнорируем, прогон случится.
            return None

    def _record_run(self, now: datetime, result: RetentionCleanupResult) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(
                    {
                        "last_run_at": now.isoformat(),
                        "total_deleted": result.total,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.warning(
                "could not write retention state file %s", self.state_file
            )

    def _log_result(
        self, now: datetime, result: RetentionCleanupResult
    ) -> None:
        # Один INFO-лог с распределением по таблицам — достаточно для
        # отслеживания «нормально ли работает retention».
        details = {
            "agent_runs": result.agent_runs,
            "inbound_messages": result.inbound_messages,
            "jobs": result.jobs,
            "outbox_sent": result.outbox_messages_sent,
            "outbox_failed": result.outbox_messages_failed,
            "secure_records_eds": result.secure_records_eds_connect_payload,
            "skill_ai": result.skill_ai_executions,
            "skill_events_low": result.skill_events_debug_info,
            "skill_events_high": result.skill_events_warn_error,
            "skill_attempts": result.skill_run_attempts,
            "skill_runs": result.skill_runs,
        }
        logger.info(
            "retention cleanup ts=%s total=%d details=%s",
            now.isoformat(timespec="seconds"),
            result.total,
            details,
        )
