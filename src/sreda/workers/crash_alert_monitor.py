"""#335 (мера D пост-мортема vex#331): алерт на крэши ходов ПО КАНАЛАМ + класс ошибки из логов.

Инцидент #331 (крэш ActiveSqlTransaction на free-тире) шёл незамеченным: 0 алертов, у канарейки
основного бота всё зелёное, «background turn processing crashed» был виден только грепом постфактум.
Этот монитор делает крэш видимым оператору в течение минут, двумя независимыми сигналами:

1. **БД: зависшие необработанные входящие по срезам (канал, бот).** Крэш фонового хода НЕ доводит
   ``inbound_messages.processing_status`` до processed/ignored (``_set_processing_status`` стоит на
   success-пути ДО except — telegram_inbound.py) → строка остаётся ingested/processing_started.
   Считаем такие строки старше grace в скользящем окне, группируя по (channel_type, bot_key) —
   всплеск на ЛЮБОМ срезе (в т.ч. лендинг-бот sreda_home, невидимый канарейке) даёт алерт.
2. **Логи: класс ошибки.** Инкрементальный хвост ``/var/log/sreda/*.log`` (offset-state, без
   пере-скана): ``psycopg.errors.<Class>`` (вкл. ActiveSqlTransaction) и строка
   «background turn processing crashed» → алерт с ПЕРВОГО появления, с именем класса.

Анти-шторм: state в runtime_config (переживает рестарт) — offset'ы логов + кулдаун 60 мин на
повторяющуюся сигнатуру. Первый запуск инициализирует offset'ы концом файлов (историю не алертим).
Best-effort по образцу svo_monitor: исключение тика проглатывается с логом, цикл не падает.

Запуск: фоновый task в ``run_job_loop_async`` (job-runner). Выключатель: ``SREDA_CRASH_ALERT_ENABLED=0``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from glob import glob
from typing import Final

from sqlalchemy import func, select

from sreda.db.models.core import InboundMessage
from sreda.db.session import privileged_session
from sreda.services import runtime_config as rc
from sreda.services.admin_alerts import send_admin_alert

logger = logging.getLogger(__name__)

_RC_STATE_KEY: Final = "crash_alert_monitor_state"
_LOG_GLOB_DEFAULT: Final = "/var/log/sreda/*.log"

# Скользящее окно и порог БД-сигнала. Grace = 10 мин (как inbound_stuck в reliability_report #139:
# штатный ход завершается за секунды; confirm-пауза помечает processed до паузы). Порог 2 на срез:
# одиночный transient не будит, инцидент класса #331 (несколько юзеров/ретраев) — будит.
_DB_WINDOW: Final = timedelta(minutes=30)
_DB_GRACE: Final = timedelta(minutes=10)
_DB_THRESHOLD_DEFAULT: Final = 2

_COOLDOWN: Final = timedelta(minutes=60)
_TICK_SECONDS_DEFAULT: Final = 300.0
# потолок чтения нового куска лога за тик — защита от разового скана гигантского хвоста
_MAX_CHUNK_BYTES: Final = 5 * 1024 * 1024

# классы ошибок в логах. NB: ActiveSqlTransaction ловится первым паттерном (psycopg.errors.*),
# «background turn processing crashed» — маркер проглоченного крэша фона (telegram_inbound).
_LOG_PATTERNS: Final = (
    re.compile(r"psycopg\.errors\.(\w+)"),
    re.compile(r"background turn processing crashed"),
)
_TURN_CRASH_CLASS: Final = "turn_crash(background)"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enabled() -> bool:
    return os.getenv("SREDA_CRASH_ALERT_ENABLED", "1").strip() != "0"


def _tick_seconds() -> float:
    try:
        return max(30.0, float(os.getenv("SREDA_CRASH_ALERT_INTERVAL_SEC", "") or _TICK_SECONDS_DEFAULT))
    except ValueError:
        return _TICK_SECONDS_DEFAULT


def _db_threshold() -> int:
    try:
        return max(1, int(os.getenv("SREDA_CRASH_ALERT_DB_THRESHOLD", "") or _DB_THRESHOLD_DEFAULT))
    except ValueError:
        return _DB_THRESHOLD_DEFAULT


@dataclass
class _State:
    """Персистентное состояние монитора (JSON в runtime_config)."""

    offsets: dict[str, int] = field(default_factory=dict)      # путь лога → прочитанный offset
    cooldowns: dict[str, str] = field(default_factory=dict)    # сигнатура алерта → iso-время
    initialized: bool = False                                   # первый запуск: offsets = EOF, историю молчим

    @classmethod
    def load(cls) -> "_State":
        try:
            with privileged_session("monitor") as session:
                raw = rc.get_config(session, _RC_STATE_KEY)
            if raw:
                d = json.loads(raw)
                return cls(offsets=dict(d.get("offsets", {})),
                           cooldowns=dict(d.get("cooldowns", {})),
                           initialized=bool(d.get("initialized", False)))
        except Exception:  # noqa: BLE001 — сломанный state не должен убить монитор
            logger.warning("crash_alert: state load failed, starting fresh", exc_info=True)
        return cls()

    def save(self) -> None:
        try:
            payload = json.dumps({"offsets": self.offsets, "cooldowns": self.cooldowns,
                                  "initialized": self.initialized}, ensure_ascii=False)
            with privileged_session("monitor") as session:
                rc.set_config(session, _RC_STATE_KEY, payload)
        except Exception:  # noqa: BLE001
            logger.warning("crash_alert: state save failed", exc_info=True)

    def cooldown_passed(self, sig: str, now: datetime) -> bool:
        raw = self.cooldowns.get(sig)
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return (now - last) >= _COOLDOWN

    def stamp(self, sig: str, now: datetime) -> None:
        self.cooldowns[sig] = now.isoformat()
        # не копим сигнатуры бесконечно: выкидываем протухшие (>24ч)
        stale = [k for k, v in self.cooldowns.items()
                 if (now - datetime.fromisoformat(v)) > timedelta(hours=24)]
        for k in stale:
            self.cooldowns.pop(k, None)


@dataclass(frozen=True)
class StuckSlice:
    """Срез зависших входящих: (канал, бот) + счёт + примеры тенантов."""

    channel_type: str
    bot_key: str
    count: int
    tenants: tuple[str, ...]


def scan_stuck_inbound(session, now: datetime, threshold: int) -> list[StuckSlice]:  # noqa: ANN001
    """БД-сигнал: необработанные inbound старше grace в окне, по срезам (канал, бот).

    Крэш фона оставляет processing_status в ingested/processing_started (telegram_inbound: processed
    ставится только на success-пути). Порог на СРЕЗ, не суммарно — всплеск лендинг-бота виден отдельно.
    """
    lo = now - _DB_WINDOW
    hi = now - _DB_GRACE
    rows = session.execute(
        select(InboundMessage.channel_type, InboundMessage.bot_key,
               func.count().label("n"))
        .where(InboundMessage.processing_status.notin_(("processed", "ignored")),
               InboundMessage.created_at >= lo,
               InboundMessage.created_at < hi)
        .group_by(InboundMessage.channel_type, InboundMessage.bot_key)
    ).all()
    out: list[StuckSlice] = []
    for channel_type, bot_key, n in rows:
        if n < threshold:
            continue
        tenants = session.execute(
            select(InboundMessage.tenant_id).distinct()
            .where(InboundMessage.processing_status.notin_(("processed", "ignored")),
                   InboundMessage.created_at >= lo,
                   InboundMessage.created_at < hi,
                   InboundMessage.channel_type == channel_type,
                   InboundMessage.bot_key == bot_key,
                   InboundMessage.tenant_id.is_not(None))
            .limit(3)
        ).scalars().all()
        out.append(StuckSlice(channel_type=channel_type or "?", bot_key=bot_key or "?",
                              count=int(n), tenants=tuple(tenants)))
    return out


def scan_log_chunk(chunk: str) -> dict[str, int]:
    """Счёт классов ошибок в куске лога. Ключ = имя класса (psycopg.errors.X → X)."""
    found: dict[str, int] = {}
    for m in _LOG_PATTERNS[0].finditer(chunk):
        cls = m.group(1)
        found[cls] = found.get(cls, 0) + 1
    n_crash = len(_LOG_PATTERNS[1].findall(chunk))
    if n_crash:
        found[_TURN_CRASH_CLASS] = found.get(_TURN_CRASH_CLASS, 0) + n_crash
    return found


def scan_logs(state: _State, log_glob: str) -> dict[str, dict[str, int]]:
    """Инкрементальный хвост логов: {класс_ошибки: {файл: счёт}} по НОВЫМ байтам с прошлого тика.

    Первый запуск (state.initialized=False) только выставляет offset'ы в EOF — историю не алертим.
    Ротация (size < offset) → читаем файл с нуля. Кусок больше потолка — читаем последние N байт.
    """
    results: dict[str, dict[str, int]] = {}
    for path in sorted(glob(log_glob)):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        prev = state.offsets.get(path, None)
        if prev is None or not state.initialized:
            state.offsets[path] = size  # первый взгляд на файл: начинаем с конца
            continue
        start = 0 if size < prev else prev  # ротация → с нуля
        state.offsets[path] = size
        if size <= start:
            continue
        if size - start > _MAX_CHUNK_BYTES:
            start = size - _MAX_CHUNK_BYTES
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(start)
                chunk = f.read(size - start)
        except OSError:
            continue
        for cls, n in scan_log_chunk(chunk).items():
            results.setdefault(cls, {})[os.path.basename(path)] = n
    state.initialized = True
    return results


def _format_alert(slices: list[StuckSlice], log_hits: dict[str, dict[str, int]]) -> str:
    lines = ["⚠️ Крэши ходов [P2] (#335, монитор мера D)"]
    for s in slices:
        who = (" (" + ", ".join(s.tenants) + ")") if s.tenants else ""
        lines.append(f"БД: {s.channel_type}/{s.bot_key} — {s.count} необработанных входящих "
                     f"за {int(_DB_WINDOW.total_seconds() // 60)} мин{who}")
    for cls, files in sorted(log_hits.items()):
        per_file = ", ".join(f"{f}×{n}" for f, n in sorted(files.items()))
        lines.append(f"Логи: {cls} — {per_file}")
    lines.append("Разбор: journalctl/греп по классу; трейс — react_turn_trace (status=in_progress).")
    return "\n".join(lines)


def tick_once(state: _State, *, now: datetime | None = None,
              log_glob: str = _LOG_GLOB_DEFAULT) -> str | None:
    """Один проход монитора. Возвращает текст отправленного алерта (или None). Sync (гоняем в thread)."""
    now = now or _utcnow()

    # 1) БД-сигнал по срезам
    slices: list[StuckSlice] = []
    try:
        with privileged_session("monitor") as session:
            slices = scan_stuck_inbound(session, now, _db_threshold())
    except Exception:  # noqa: BLE001
        logger.warning("crash_alert: db scan failed", exc_info=True)

    # 2) лог-сигнал (классы ошибок)
    log_hits: dict[str, dict[str, int]] = {}
    try:
        log_hits = scan_logs(state, log_glob)
    except Exception:  # noqa: BLE001
        logger.warning("crash_alert: log scan failed", exc_info=True)

    # 3) кулдаун по сигнатурам — алертим только новое/остывшее
    fresh_slices = [s for s in slices
                    if state.cooldown_passed(f"db:{s.channel_type}:{s.bot_key}", now)]
    fresh_logs = {cls: files for cls, files in log_hits.items()
                  if state.cooldown_passed(f"log:{cls}", now)}
    if not fresh_slices and not fresh_logs:
        state.save()
        return None

    text = _format_alert(fresh_slices, fresh_logs)
    try:
        send_admin_alert(text)
    except Exception:  # noqa: BLE001
        logger.warning("crash_alert: send_admin_alert failed", exc_info=True)
        state.save()
        return None
    for s in fresh_slices:
        state.stamp(f"db:{s.channel_type}:{s.bot_key}", now)
    for cls in fresh_logs:
        state.stamp(f"log:{cls}", now)
    state.save()
    logger.warning("crash_alert: alert sent\n%s", text)
    return text


async def run_crash_alert_loop() -> None:
    """Фоновый цикл монитора (спавнится в job-runner). Best-effort: тик не может убить цикл."""
    if not _enabled():
        logger.info("crash_alert: disabled via SREDA_CRASH_ALERT_ENABLED=0")
        return
    logger.info("crash_alert: monitor started (tick=%ss)", _tick_seconds())
    state = _State.load()
    while True:
        try:
            await asyncio.to_thread(tick_once, state)
        except Exception:  # noqa: BLE001
            logger.exception("crash_alert: tick failed, continuing")
        await asyncio.sleep(_tick_seconds())
