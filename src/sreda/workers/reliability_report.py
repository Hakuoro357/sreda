"""#139 — мерило надёжности диалога: ежедневный автоотчёт (этап 0).

Раз в сутки шлёт владельцу сводку: ходов за сутки / провалов по классам
(наблюдаемые признаки, не эвристика по тексту) + скользящий KPI за 14 дней
(порог запуска: ≥95% ходов без провала — утверждён 2026-06-12).

Классы провалов:
- ``runs_failed`` — ход (react_turn) с ошибочным исполнением за окно
  (#303: раньше agent_runs.status='failed', но agent_runs мертва с 23.06 —
  старый планировщик задепрекейчен, ReAct пишет skill_ai_executions);
- ``inbound_stuck`` — входящее старше 10 минут, так и не дошедшее до
  processed/ignored (ход умер до ответа);
- ``outbox_failed`` — исходящее не доставлено;
- ``breakdowns_shown`` — строки «ПОЛОМКА показана пользователю» в логе
  job-runner за окно (инвариант f2b9a18: каждый показ пишется ERROR).

#227 (мини-спека утверждена 2026-07-10): отдельная строка «% успешных ходов
(ReAct)» по исходам durable ``react_turn_trace`` — знаменатель = завершённые
ходы (outcome IS NOT NULL) за 24ч-окно UTC; провал = ``safe_reply`` |
``breakdown`` (legacy-страховка); «умершие» (in_progress старше 1ч) — отдельным
числом, НЕ в знаменателе. В скользящий KPI по сигналам НЕ входит (аддитивно).

Каденс и устойчивость — по образцу retention (#127): state-файл,
не чаще раза в сутки, откат после провала вместо шторма.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from glob import glob as _glob
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from sreda.db.models.core import InboundMessage, OutboxMessage
from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.models.skill_platform import SkillAIExecution
from sreda.db.session import privileged_session
from sreda.services.admin_alerts import send_admin_alert

logger = logging.getLogger("sreda.reliability")

# state — САМА метрика (история KPI), /tmp стирается ребутом (субагент R1)
DEFAULT_STATE_FILE = "/var/lib/sreda/reliability-state.json"
# поломки пишут ВСЕ процессы (поллер/uvicorn/job-runner) — каждый в свой
# лог (субагент R1 CRITICAL: интерактивные ходы идут в поллере)
DEFAULT_LOG_GLOB = "/var/log/sreda/*.log"
REPORT_WINDOW = timedelta(hours=24)
STUCK_GRACE = timedelta(minutes=10)
# #227: «умерший» ход = react_turn_trace.status='in_progress' старше часа —
# непойманная смерть процесса mid-turn (kill/OOM/рестарт), finish-хук не
# отработал, outcome остался NULL. Порог 1ч утверждён оркестратором
# 2026-07-10. awaiting_confirm — НЕ умерший (пауза ждёт юзера — модель
# react_trace.py); done без outcome — брошенная пауза #320, тоже не умерший.
DEAD_TURN_GRACE = timedelta(hours=1)
FAILURE_BACKOFF = timedelta(minutes=30)
KPI_DAYS = 14
KPI_THRESHOLD_PCT = 95.0
ALERT_AFTER_CONSECUTIVE_FAILURES = 3
_BREAKDOWN_MARKER = "ПОЛОМКА показана пользователю"
# логи пишутся локальным временем сервера (config/logging.py, datefmt
# без tz) — операционная зона проекта MSK
_LOG_TZ = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class DayCounts:
    turns_total: int
    runs_failed: int
    inbound_stuck: int
    outbox_failed: int
    breakdowns_shown: int
    # #227: срез react_turn_trace за то же окно (мини-спека 2026-07-10).
    # Дефолты держат обратную совместимость позиционных конструкторов
    # (тесты/моки). В скользящий KPI по сигналам (failures_total) эти
    # числа НЕ входят — строка «% успешных ходов» аддитивна.
    react_finished: int = 0     # знаменатель: outcome IS NOT NULL за окно
    react_safe_reply: int = 0   # провал: юзер получил заглушку
    react_breakdown: int = 0    # провал: legacy, страховка back-compat
    react_dead: int = 0         # in_progress старше 1ч — НЕ в знаменателе

    @property
    def failures_total(self) -> int:
        # классы могут пересекаться (один ход — и failed, и поломка);
        # для KPI v1 считаем сумму — консервативно в сторону тревоги
        return (self.runs_failed + self.inbound_stuck
                + self.outbox_failed + self.breakdowns_shown)

    @property
    def react_success(self) -> int:
        # успех = завершённые минус провальные исходы (мини-спека #227:
        # числитель = outcome NOT IN ('safe_reply','breakdown'))
        return self.react_finished - self.react_safe_reply - self.react_breakdown


def _count(session: Session, stmt) -> int:
    return int(session.execute(stmt).scalar() or 0)


def gather_day_counts(
    session: Session, *, now: datetime, log_path: str | None = None,
    breakdowns_precounted: int | None = None,
) -> DayCounts:
    since = now - REPORT_WINDOW
    # #303: ход = distinct run_id из react_turn (agent_runs мертва с 23.06).
    # ЕДИНАЯ семантика с админ-дашбордом (overview_snapshot._health_block):
    # мульти-итерационный ReAct с N react_turn в одном run = ОДИН ход;
    # провал = run с хотя бы одним ошибочным react_turn.
    _react = and_(
        SkillAIExecution.created_at >= since,
        SkillAIExecution.created_at < now,
        SkillAIExecution.task_type == "react_turn",
    )
    turns_total = _count(session, select(
        func.count(func.distinct(SkillAIExecution.run_id))).where(_react))
    runs_failed = _count(session, select(
        func.count(func.distinct(SkillAIExecution.run_id))).where(and_(
            _react,
            SkillAIExecution.status.in_(("failed", "validation_failed")),
        )))
    # окно сдвинуто целиком на grace — иначе последние 10 минут каждых
    # суток не проверяются никогда (субагент R1 MINOR)
    inbound_stuck = _count(
        session, select(func.count(InboundMessage.id)).where(and_(
            InboundMessage.created_at >= since - STUCK_GRACE,
            InboundMessage.created_at < now - STUCK_GRACE,
            InboundMessage.processing_status.notin_(("processed", "ignored")),
        )))
    outbox_failed = _count(
        session, select(func.count(OutboxMessage.id)).where(and_(
            OutboxMessage.created_at >= since,
            OutboxMessage.created_at < now,
            OutboxMessage.status == "failed",
        )))
    breakdowns = (breakdowns_precounted if breakdowns_precounted is not None
                  else count_breakdown_lines(
                      log_path or DEFAULT_LOG_GLOB, since=since, until=now))
    # #227: исходы ReAct из durable react_turn_trace (источник истины по
    # ходам обоих путей — единого #285 и легаси-сплита; персист в общем
    # выходе handle_turn). Завершённые = outcome IS NOT NULL: паузы
    # (awaiting_confirm) и брошенные паузы (#320: done без outcome) — вне
    # знаменателя, как у probe_failed_turns_rate.
    outcome_rows = session.execute(
        select(ReactTurnTrace.outcome, func.count(ReactTurnTrace.id))
        .where(and_(
            ReactTurnTrace.created_at >= since,
            ReactTurnTrace.created_at < now,
            ReactTurnTrace.outcome.is_not(None),
        ))
        .group_by(ReactTurnTrace.outcome)).all()
    by_outcome = {str(oc): int(cnt or 0) for oc, cnt in outcome_rows}
    react_finished = sum(by_outcome.values())
    # Провальные исходы — как в probe_failed_turns_rate (monitor_health.py):
    # safe_reply = юзер получил заглушку (пойманный краш / таймаут / транзиент
    # LLM); breakdown = legacy plan-execute, в react_turn_trace его сейчас
    # никто не пишет — страховка back-compat. tool_error / fallback_used — НЕ
    # провал: ход дал содержательный ответ (деградации алертятся #258).
    # Сегодня цикл пишет ТОЛЬКО ok|tool_error|fallback_used|safe_reply
    # (_turn_outcome + краш-хендлер react_loop.py); любой НОВЫЙ исход по
    # мини-спеке #227 по умолчанию считается успехом (числитель = NOT IN
    # ('safe_reply','breakdown')) — тест freeze: unknown outcome = success.
    react_safe_reply = by_outcome.get("safe_reply", 0)
    react_breakdown = by_outcome.get("breakdown", 0)
    # «умершие»: непойманная смерть хода (строка навсегда in_progress).
    # Свежие in_progress (< 1ч) ещё могут завершиться — не считаем. Окно
    # сдвинуто ЦЕЛИКОМ на grace (как у inbound_stuck — R1 Codex high+medium
    # MAJOR): иначе ход из последнего часа окна не dead сегодня и уже вне
    # окна завтра → терялся бы навсегда. Так каждый умерший считается ровно
    # один раз — в отчёте, где он впервые простоял час. outcome IS NULL —
    # страховка от частично обновлённой строки (не задвоить с finished).
    react_dead = _count(
        session, select(func.count(ReactTurnTrace.id)).where(and_(
            ReactTurnTrace.created_at >= since - DEAD_TURN_GRACE,
            ReactTurnTrace.created_at < now - DEAD_TURN_GRACE,
            ReactTurnTrace.status == "in_progress",
            ReactTurnTrace.outcome.is_(None),
        )))
    return DayCounts(turns_total, runs_failed, inbound_stuck,
                     outbox_failed, breakdowns,
                     react_finished=react_finished,
                     react_safe_reply=react_safe_reply,
                     react_breakdown=react_breakdown,
                     react_dead=react_dead)


def count_breakdown_lines(
    log_glob: str, *, since: datetime, until: datetime,
) -> int:
    """Маркер по ВСЕМ логам glob-шаблона (поллер/uvicorn/job-runner —
    каждый процесс пишет в свой; субагент R1 CRITICAL). Времена строк —
    локальные (MSK) → в UTC. Строки без парсящейся даты НЕ считаются
    (раздув истории при смене формата), но логируются скопом.
    Недоступность файлов — fail-soft 0 + warning."""
    n = 0
    unparsed = 0
    if any(c in log_glob for c in "*?["):
        # + .1 от logrotate (delaycompress): маркеры до ротации внутри
        # 24ч-окна не должны теряться (high R2)
        paths = sorted(set(_glob(log_glob)) | set(_glob(log_glob + ".1")))
    else:
        paths = [log_glob]
    if not paths:
        logger.warning("reliability: логи по %s не найдены — breakdowns=0",
                       log_glob)
        return 0
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if _BREAKDOWN_MARKER not in line:
                        continue
                    try:
                        ts = datetime.strptime(line[:19],
                                               "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        unparsed += 1
                        continue
                    ts = ts.replace(tzinfo=_LOG_TZ).astimezone(timezone.utc)
                    if since <= ts < until:
                        n += 1
        except OSError:
            logger.warning("reliability: лог %s недоступен", path)
    if unparsed:
        logger.warning("reliability: %d строк с маркером без парсящейся "
                       "даты — НЕ учтены", unparsed)
    return n


def format_report(counts, history: list[dict],
                  *, kpi_threshold_pct: float = KPI_THRESHOLD_PCT) -> str:
    total = sum(int(h.get("total") or 0) for h in history)
    failures = sum(int(h.get("failures") or 0) for h in history)
    if total == 0:
        # сигналы без единого хода не могут давать «100% чистых»
        # (Codex R1 medium)
        pct = 0.0 if failures > 0 else 100.0
    else:
        pct = 100.0 * (1 - min(failures, total) / total)
    days = len(history)
    kpi_line = (f"KPI за {days} дн: {pct:.1f}% чистых "
                f"(по сигналам; классы могут пересекаться)")
    if pct < kpi_threshold_pct:
        kpi_line += f" — ниже порога {kpi_threshold_pct:g}%! 🔴"
    else:
        kpi_line += f" (порог {kpi_threshold_pct:g}%) ✅"
    # #227: «% успешных ходов за день» по исходам react_turn_trace (формат
    # утверждён оркестратором 2026-07-10). breakdown — только если ненулевой
    # (мёртвый легаси, страховка); «умерших» — всегда (стабильный формат).
    if counts.react_finished > 0:
        _fails = f"safe_reply={counts.react_safe_reply}"
        if counts.react_breakdown:
            _fails += f", breakdown={counts.react_breakdown}"
        _rpct = 100.0 * counts.react_success / counts.react_finished
        react_line = (
            f"успешных ходов (ReAct): {counts.react_success}/"
            f"{counts.react_finished} ({_rpct:.1f}%) - провалы: {_fails}; "
            f"умерших (без исхода): {counts.react_dead}")
    else:
        react_line = ("успешных ходов (ReAct): нет завершённых ходов за "
                      f"сутки; умерших (без исхода): {counts.react_dead}")
    return (
        "📊 Надёжность диалога за сутки\n"
        f"ходов: {counts.turns_total}\n"
        f"{react_line}\n"
        f"провалы — исполнение: {counts.runs_failed}, "
        f"застряло: {counts.inbound_stuck}, "
        f"доставка: {counts.outbox_failed}, "
        f"поломок показано: {counts.breakdowns_shown}\n"
        f"{kpi_line}"
    )


class ReliabilityReportWorker:
    """Шлёт сводку не чаще раза в сутки; провал → откат (не шторм)."""

    def __init__(self, *, state_file: str | None = None,
                 log_path: str | None = None) -> None:
        # #138 Ф2: воркер сам открывает privileged_session("monitor") на
        # глобальный KPI-COUNT; общую сессию больше не принимает.
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        # Codex R2 (оба): при неписабельном основном каталоге провальная
        # отметка писалась тем же сломанным писателем → бесконечные
        # повторные отправки. Запасной путь в /tmp держит хотя бы откат.
        self.fallback_state_file = Path(
            "/tmp/sreda-reliability-state-fallback.json")
        self.log_path = log_path or DEFAULT_LOG_GLOB

    async def process_pending(self) -> int:
        now = datetime.now(timezone.utc)
        state = self._read_state()
        if not self._should_run(now, state):
            return 0
        try:
            # скан логов — в поток и ДО DB-запросов (не держать транзакцию
            # на время чтения файлов — субагент R1)
            import asyncio as _aio
            breakdowns = await _aio.to_thread(
                count_breakdown_lines, self.log_path,
                since=now - REPORT_WINDOW, until=now)
            # #138 Ф2: глобальный KPI (COUNT по ВСЕМ тенантам) → privileged.
            with privileged_session("monitor") as session:
                counts = gather_day_counts(
                    session, now=now,
                    breakdowns_precounted=breakdowns)
            day = {"date": now.date().isoformat(),
                   "total": counts.turns_total,
                   "failures": counts.failures_total}
            history = [h for h in state.get("history", [])
                       if h.get("date") != day["date"]]
            history.append(day)
            # 14 КАЛЕНДАРНЫХ дней, не «последние 14 отчётов» (субагент)
            cutoff = (now.date() - timedelta(days=KPI_DAYS - 1)).isoformat()
            history = [h for h in history if str(h.get("date")) >= cutoff]
            send_admin_alert(
                "INFO", "надёжность диалога — суточная сводка",
                format_report(counts, history),
                dedupe_key=f"reliability:daily:{day['date']}",
            )
        except Exception:  # noqa: BLE001 — не валим job_runner
            logger.exception("reliability report failed")
            failures = int(state.get("failure_count") or 0) + 1
            state["last_failure_at"] = now.isoformat()
            state["failure_count"] = failures
            self._write_state(state)
            if failures >= ALERT_AFTER_CONSECUTIVE_FAILURES:
                try:  # серия провалов — не тихое гниение (#127-паттерн)
                    send_admin_alert(
                        "P1", "сводка надёжности падает серией",
                        f"{failures} провалов подряд — лог sreda.reliability",
                        dedupe_key="reliability:consecutive_failures")
                except Exception:  # noqa: BLE001
                    logger.exception("reliability: alert delivery failed")
            return 0
        state["last_run_at"] = now.isoformat()
        state.pop("last_failure_at", None)
        state["failure_count"] = 0
        state["history"] = history
        if not self._write_state(state):
            # state не записан → день НЕ зачтён: уходим в откат как провал,
            # СО счётчиком серии (high R2: иначе P1 по этому пути не
            # сработает); запись пойдёт в запасной /tmp-путь
            state.pop("last_run_at", None)
            state["last_failure_at"] = now.isoformat()
            state["failure_count"] = int(state.get("failure_count") or 0) + 1
            self._write_state(state)
            return 0
        logger.info("reliability report sent: %s", day)
        return 1

    def _should_run(self, now: datetime, state: dict) -> bool:
        last = self._parse_ts(state.get("last_run_at"))
        # раз в сутки: по смене календарной даты UTC (стабильнее интервала)
        if last is not None and last.date() == now.date():
            return False
        fail = self._parse_ts(state.get("last_failure_at"))
        if fail is not None and (now - fail) < FAILURE_BACKOFF:
            return False
        return True

    @staticmethod
    def _parse_ts(raw: object) -> datetime | None:
        if not isinstance(raw, str):
            return None
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    def _read_state(self) -> dict:
        # high R3: основной может быть ЧИТАЕМ, но устаревшим (записи шли
        # в запасной) — читаем оба и берём свежайший по отметкам времени
        candidates: list[dict] = []
        for path in (self.state_file, self.fallback_state_file):
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    candidates.append(data)
            except (json.JSONDecodeError, ValueError, OSError):
                continue
        if not candidates:
            return {}

        def freshness(d: dict):
            stamps = [self._parse_ts(d.get(k))
                      for k in ("last_run_at", "last_failure_at")]
            stamps = [t for t in stamps if t is not None]
            return max(stamps) if stamps else datetime.min.replace(
                tzinfo=timezone.utc)

        return max(candidates, key=freshness)

    def _write_state(self, state: dict) -> bool:
        payload = json.dumps(state, ensure_ascii=False)
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(payload, encoding="utf-8")
            return True
        except OSError:
            logger.warning("reliability: state file %s недоступен — "
                           "пробую запасной", self.state_file)
        try:
            self.fallback_state_file.write_text(payload, encoding="utf-8")
            return True
        except OSError:
            logger.warning("reliability: запасной state %s тоже недоступен",
                           self.fallback_state_file)
            return False
