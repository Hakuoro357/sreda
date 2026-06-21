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
from sreda.services.admin_alerts import send_admin_alert


logger = logging.getLogger("sreda.retention")


# Default state file path. Может быть переопределён через
# SREDA_RETENTION_STATE_FILE env var в settings (для тестов / контейнеров).
DEFAULT_STATE_FILE = "/tmp/sreda-retention-state.json"

# Интервал между прогонами. 24 часа — стандарт для retention.
DEFAULT_INTERVAL = timedelta(hours=24)

# #127: состояние писалось только при УСПЕХЕ → провал повторялся на
# каждом тике джоб-раннера (раз в секунду, 264k ошибок/сутки, трое суток).
# Откат после провала + P1-алерт после серии (тихое гниение запрещено —
# правило «ошибка должна быть записана у нас»).
FAILURE_BACKOFF = timedelta(minutes=30)
ALERT_AFTER_CONSECUTIVE_FAILURES = 3


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
            # #127 CRITICAL (Codex R1 оба): cleanup делает только flush(),
            # а job_runner закрывает сессию БЕЗ commit — удаления тихо
            # откатывались, state говорил «успех», ретеншн молчал сутки.
            # Коммитим ЗДЕСЬ, до записи успешного state; провал коммита —
            # это провал прогона (rollback + запись провала ниже).
            self.session.commit()
        except Exception:  # noqa: BLE001 — никогда не убиваем job_runner
            logger.exception("retention cleanup failed")
            # 2026-04-28: rollback чтобы транзакция не висела в bad state
            # — иначе следующие workers внутри того же job_runner tick'а
            # начнут падать с InvalidRequestError на чтении.
            try:
                self.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            self._record_failure(now)
            return 0
        self._record_run(now, result)
        self._log_result(now, result)
        return result.total

    def _should_run(self, now: datetime) -> bool:
        """True если пора: успешный прогон >= interval назад И провал
        (если был) >= FAILURE_BACKOFF назад (#127: без отката провал
        повторялся на каждом тике)."""
        state = self._read_state()
        last = self._parse_ts(state.get("last_run_at"))
        if last is not None and (now - last) < self.interval:
            return False
        last_failure = self._parse_ts(state.get("last_failure_at"))
        if last_failure is not None and (now - last_failure) < FAILURE_BACKOFF:
            return False
        return True

    def _record_failure(self, now: datetime) -> None:
        """#127: зафиксировать провал (откат + счётчик) и алертить серию."""
        state = self._read_state()
        # защитный парс: мусор в state-файле не должен ронять
        # обработчик провала (мы и так внутри except)
        try:
            failures = int(state.get("failure_count") or 0) + 1
        except (TypeError, ValueError):
            failures = 1
        state["last_failure_at"] = now.isoformat()
        state["failure_count"] = failures
        self._write_state(state)
        if failures >= ALERT_AFTER_CONSECUTIVE_FAILURES:
            try:
                send_admin_alert(
                    "P1",
                    "ретеншн-чистка падает серией",
                    f"{failures} провалов подряд; последний "
                    f"{now.isoformat(timespec='seconds')} — см. лог "
                    f"sreda.retention (чистка стоит, строки копятся)",
                    dedupe_key="retention:consecutive_failures",
                )
            except Exception:  # noqa: BLE001 — алерт не валит воркер
                logger.exception("retention: alert delivery failed")

    def _read_state(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError, OSError):
            # Corrupt state file — игнорируем, прогон случится.
            return {}

    @staticmethod
    def _parse_ts(raw: object) -> datetime | None:
        if not isinstance(raw, str):
            return None
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return None
        # наивный ts в state-файле (ручная правка) дал бы TypeError на
        # `now - ts` ВНЕ try в process_pending — считаем его UTC
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def _write_state(self, state: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8",
            )
        except OSError:
            logger.warning(
                "could not write retention state file %s", self.state_file
            )

    def _record_run(self, now: datetime, result: RetentionCleanupResult) -> None:
        # успех сбрасывает серию провалов (#127)
        state = self._read_state()
        state["last_run_at"] = now.isoformat()
        state["total_deleted"] = result.total
        state["failure_count"] = 0
        state.pop("last_failure_at", None)
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(state, ensure_ascii=False),
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
            "react_debug": result.react_debug_turns,  # #185
        }
        logger.info(
            "retention cleanup ts=%s total=%d details=%s",
            now.isoformat(timespec="seconds"),
            result.total,
            details,
        )
