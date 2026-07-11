"""#335 (мера D пост-мортема vex#331): алерт на крэши ходов ПО КАНАЛАМ + класс ошибки из логов.

Инцидент #331 (крэш ActiveSqlTransaction на free-тире) шёл незамеченным: 0 алертов, у канарейки
основного бота всё зелёное, «background turn processing crashed» был виден только грепом постфактум.
Этот монитор делает крэш видимым оператору в течение минут, двумя независимыми сигналами:

1. **БД: зависшие необработанные входящие по срезам (канал, бот).** Крэш фонового хода НЕ доводит
   ``inbound_messages.processing_status`` до processed/ignored (``_set_processing_status`` стоит на
   success-пути ДО except — telegram_inbound.py) → строка остаётся ingested/processing_started.
   Считаем такие строки старше grace в скользящем окне, группируя по (channel_type, bot_key) —
   всплеск на ЛЮБОМ срезе (в т.ч. лендинг-бот sreda_home, невидимый канарейке) даёт алерт.
   Порог 2 на срез: одиночный transient не будит (одиночный крэш ловится лог-сигналом ниже).
2. **Логи: класс ошибки.** Инкрементальный хвост ``/var/log/sreda/*.log`` (offset-state, без
   пере-скана): ``psycopg.errors.<Class>`` и маркеры проглоченного крэша хода
   («background turn processing crashed» — telegram, «max approved turn crashed» — MAX).
   Ярусы (ревью R1: не обесценить монитор ложными P2):
   - порча-классы транзакций (ActiveSqlTransaction, InFailedSqlTransaction) и turn-crash маркеры
     → P2 с ПЕРВОГО появления;
   - прочие psycopg.errors.* → INFO и только при ≥3 за тик (обработанные/ретраящиеся одиночки
     фоновых задач не будят).

Дедуп/анти-шторм — ШТАТНЫЙ механизм ``send_admin_alert`` (dedupe_key + окно по severity: P2=10мин,
INFO=30мин; «отправлено» ставится только при успешной доставке → недоставленный алерт ретраится
следующим тиком). Свой кулдаун НЕ держим (ревью R1 high: свой кулдаун глушил бы недоставленное).

Устойчивость к прод-ротации логов (logrotate ``copytruncate``, maxsize 200M): офсеты в БАЙТАХ +
отпечаток головы файла (sha1 зафиксированного префикса) — truncate меняет голову → поколение
сменилось → читаем с нуля, даже если файл успел отрасти выше старого офсета. Офсет двигается
ТОЛЬКО на фактически прочитанное (сбой чтения → ретрай с того же места); потолок 5MiB за тик —
остаток дочитывается следующими тиками. Маркер на границе кусков ловится carry-хвостом.
Принятый компромисс (ревью R1): строки, записанные МЕЖДУ последним тиком и copytruncate, теряются
для лог-сигнала (окно ≤1 тика раз в ротацию); этот хвост подстраховывает БД-сигнал.

Первый запуск инициализирует офсеты концом файлов (историю не алертим). State — в runtime_config
(переживает рестарт). Инвариант деплоя: job-runner — ОДИН systemd-инстанс (state без блокировок).
Best-effort по образцу svo_monitor: исключение тика проглатывается с логом, цикл не падает.

Запуск: фоновый task в ``run_job_loop_async`` (job-runner). Выключатель: ``SREDA_CRASH_ALERT_ENABLED=0``.
"""

from __future__ import annotations

import asyncio
import hashlib
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
# штатный ход завершается за секунды; confirm-пауза помечает processed до паузы).
_DB_WINDOW: Final = timedelta(minutes=30)
_DB_GRACE: Final = timedelta(minutes=10)
_DB_THRESHOLD_DEFAULT: Final = 2

_TICK_SECONDS_DEFAULT: Final = 300.0
_MAX_CHUNK_BYTES: Final = 5 * 1024 * 1024   # потолок чтения за тик (остаток — следующими тиками)
_CARRY_CHARS: Final = 300                   # хвост прошлого куска — ловит маркер на границе
_HEAD_SIG_BYTES: Final = 256                # отпечаток головы файла (детект copytruncate)

_PSYCOPG_RE: Final = re.compile(r"psycopg\.errors\.(\w+)")
# сколько живёт недоставленный лог-алерт в pending-повторе: 9 мин ≈ 2-3 тика ретраев; МЕНЬШЕ
# dedupe-окна P2 (600с) → после УСПЕШНОЙ доставки повторы гасятся дедупом и не дублируют
_PENDING_TTL: Final = timedelta(minutes=9)
# маркеры проглоченного крэша хода по каналам (строки logger.exception в inbound-обвязке)
_TURN_CRASH_MARKERS: Final = (
    (re.compile(r"background turn processing crashed"), "turn_crash(telegram)"),
    (re.compile(r"max approved turn crashed"), "turn_crash(max)"),
)
# порча-классы транзакций: P2 с первого появления (класс инцидента #331)
_POISON_CLASSES: Final = frozenset({"ActiveSqlTransaction", "InFailedSqlTransaction"})
_OTHER_CLASS_MIN_COUNT: Final = 3           # прочие psycopg-классы: INFO и только при >=3 за тик


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


def _head_sig(path: str, n_bytes: int = _HEAD_SIG_BYTES) -> str:
    """Отпечаток головы файла (первые n_bytes): copytruncate переписывает содержимое с нуля →
    отпечаток меняется. Длина префикса ФИКСИРУЕТСЯ в state (sig_len): у файла короче 256Б голова
    растёт при дописывании, и хеш «первых size байт» ложно менялся бы на каждый append."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read(n_bytes)).hexdigest()
    except OSError:
        return ""


def _sanitize_file_entry(e: object) -> dict | None:
    """R2 субагент N3: битая пер-файловая запись state должна выбрасываться, не слепить монитор."""
    if not isinstance(e, dict):
        return None
    try:
        return {"off": max(0, int(e.get("off", 0))),
                "sig": str(e.get("sig", "")),
                "sig_len": max(0, int(e.get("sig_len", 0))),
                "carry": str(e.get("carry", "")),
                "capped": bool(e.get("capped", False))}
    except (TypeError, ValueError):
        return None


@dataclass
class _State:
    """Персистентное состояние монитора (JSON в runtime_config).

    files: путь → {"off": байт-офсет, "sig": отпечаток головы, "sig_len": длина префикса
    отпечатка, "carry": хвост прошлого куска, "capped": прошлый кусок был обрезан потолком}.
    pending: dedupe_key → {"severity","title","body","expires"} — недоставленные лог-алерты
    ретраятся тиками до TTL (send_admin_alert fire-and-forget: успех гасится его дедупом,
    неуспех не помечается «отправлено» → повтор реально уходит). БД-алертам pending не нужен:
    их источник (строки в окне) сам живёт и переобнаруживается каждый тик.
    """

    files: dict[str, dict] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    initialized: bool = False  # первый запуск: офсеты = EOF, историю молчим

    @classmethod
    def load(cls) -> "_State":
        try:
            with privileged_session("monitor") as session:
                raw = rc.get_config(session, _RC_STATE_KEY)
            if raw:
                d = json.loads(raw)
                files_raw = d.get("files", {})
                files: dict[str, dict] = {}
                if isinstance(files_raw, dict):
                    for path, e in files_raw.items():
                        clean = _sanitize_file_entry(e)
                        if clean is not None:
                            files[str(path)] = clean
                pending_raw = d.get("pending", {})
                pending: dict[str, dict] = {}
                if isinstance(pending_raw, dict):
                    for k, p in pending_raw.items():
                        if isinstance(p, dict) and all(isinstance(p.get(x), str)
                                                       for x in ("severity", "title", "body",
                                                                 "expires")):
                            pending[str(k)] = p
                return cls(files=files, pending=pending,
                           initialized=bool(d.get("initialized", False)))
        except Exception:  # noqa: BLE001 — сломанный state не должен убить монитор
            logger.warning("crash_alert: state load failed, starting fresh", exc_info=True)
        return cls()

    def save(self) -> None:
        try:
            payload = json.dumps({"files": self.files, "pending": self.pending,
                                  "initialized": self.initialized}, ensure_ascii=False)
            with privileged_session("monitor") as session:
                rc.set_config(session, _RC_STATE_KEY, payload)
        except Exception:  # noqa: BLE001
            logger.warning("crash_alert: state save failed", exc_info=True)


@dataclass(frozen=True)
class StuckSlice:
    """Срез зависших входящих: (канал, бот) + счёт + примеры тенантов."""

    channel_type: str
    bot_key: str
    count: int
    tenants: tuple[str, ...]


def scan_stuck_inbound(session, now: datetime, threshold: int) -> list[StuckSlice]:  # noqa: ANN001
    """БД-сигнал: необработанные inbound старше grace в окне, по срезам (канал, бот)."""
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


def scan_log_text(text: str, offset_chars: int = 0, *, truncated_tail: bool = False,
                  boundary_inclusive: bool = False) -> dict[str, int]:
    """Счёт классов ошибок в тексте; учитываются только совпадения, ЗАКАНЧИВАЮЩИЕСЯ после
    offset_chars (начало текста = carry прошлого тика, его совпадения уже считались).

    truncated_tail=True — кусок обрезан потолком чтения: psycopg-матч, упирающийся в самый конец,
    может нести ОБРЕЗАННОЕ имя класса (\\w+ съедает сколько есть — «ActiveSqlTr») → бросаем его,
    следующий тик доловит. Фиксированным turn-crash маркерам обрезка не грозит.

    boundary_inclusive=True — ПРОШЛЫЙ кусок был обрезан потолком: его хвостовой psycopg-матч был
    брошен как потенциально обрезанный → матч, заканчивающийся РОВНО на offset_chars, ещё не
    считался и считается сейчас (R2 субагент N1: полное имя ровно на границе терялось навсегда).
    Применяется ТОЛЬКО к psycopg-регексу: turn-crash маркеры хвостом не бросаются (фиксированная
    строка не бывает обрезанным матчем) → на границе они УЖЕ посчитаны, включать их = двойной счёт."""
    found: dict[str, int] = {}
    for m in _PSYCOPG_RE.finditer(text):
        end = m.end()
        if not (end > offset_chars or (boundary_inclusive and end == offset_chars)):
            continue
        if truncated_tail and end == len(text):
            continue
        cls = m.group(1)
        found[cls] = found.get(cls, 0) + 1
    for rx, label in _TURN_CRASH_MARKERS:
        n = sum(1 for m in rx.finditer(text) if m.end() > offset_chars)
        if n:
            found[label] = found.get(label, 0) + n
    return found


def scan_logs(state: _State, log_glob: str) -> dict[str, dict[str, int]]:
    """Инкрементальный хвост логов: {класс_ошибки: {файл: счёт}} по НОВЫМ байтам с прошлого тика.

    - первый запуск монитора: только выставить офсеты в EOF (историю не алертим);
    - НОВЫЙ файл после инициализации: читаем С НУЛА (ревью R1: файл после rename-ротации);
    - смена поколения (size < off ИЛИ отпечаток головы изменился — copytruncate): с нуля;
    - офсет двигается ТОЛЬКО на фактически прочитанные байты (сбой чтения → ретрай);
    - кусок больше потолка → читаем потолок, остаток следующими тиками;
    - маркер на границе кусков → carry-хвост прошлого куска.
    """
    results: dict[str, dict[str, int]] = {}
    for path in sorted(glob(log_glob)):
        # ВЕСЬ разбор файла — через ОДИН дескриптор: размер (fstat), голова (читается один раз,
        # отпечаток штампуется из ЭТОГО буфера, никогда не перечитывается после chunk-чтения) и
        # сам кусок. Закрывает гонку R2 (оба Codex): truncate между проверкой поколения и чтением
        # давал штамп «отпечаток нового поколения + офсет старого» → начало нового файла терялось.
        # С одним fd любая перемежающаяся truncate даёт либо срабатывание gen-проверки сейчас,
        # либо несовпадение отпечатка следующим тиком → перечитывание с нуля (самолечение).
        try:
            f = open(path, "rb")
        except OSError:
            continue
        try:
            size = os.fstat(f.fileno()).st_size
            head = f.read(min(_HEAD_SIG_BYTES, size))
        except OSError:
            f.close()
            continue
        entry = state.files.get(path)
        if entry is None:
            if not state.initialized:
                f.close()
                state.files[path] = {"off": size, "sig": hashlib.sha1(head).hexdigest(),
                                     "sig_len": len(head), "carry": "", "capped": False}
                continue
            entry = {"off": 0, "sig": "", "sig_len": 0, "carry": "", "capped": False}
        start = int(entry.get("off", 0))
        carry = str(entry.get("carry", ""))
        prev_capped = bool(entry.get("capped", False))
        sig_len = int(entry.get("sig_len", 0))
        # поколение сменилось (copytruncate/rename): файл короче офсета/отпечатка ИЛИ голова
        # (в зафиксированной длине префикса) больше не совпадает
        gen_changed = size < start or size < sig_len or (
            sig_len > 0 and hashlib.sha1(head[:sig_len]).hexdigest() != entry.get("sig"))
        if gen_changed:
            start, carry, prev_capped = 0, "", False
        stamped_sig = hashlib.sha1(head).hexdigest()  # из УЖЕ прочитанного буфера, не с диска
        if size <= start:
            f.close()
            state.files[path] = {"off": start, "sig": stamped_sig, "sig_len": len(head),
                                 "carry": carry, "capped": prev_capped}
            continue
        to_read = min(size - start, _MAX_CHUNK_BYTES)
        try:
            f.seek(start)
            raw = f.read(to_read)
        except OSError:
            continue  # офсет/отпечаток НЕ трогаем — ретрай следующим тиком
        finally:
            f.close()
        text = raw.decode("utf-8", errors="replace")
        combined = carry + text
        capped = (start + len(raw)) < size  # кусок обрезан потолком — хвост может нести обрезанный класс
        for cls, n in scan_log_text(combined, offset_chars=len(carry), truncated_tail=capped,
                                    boundary_inclusive=prev_capped).items():
            results.setdefault(cls, {})[os.path.basename(path)] = n
        state.files[path] = {"off": start + len(raw), "sig": stamped_sig, "sig_len": len(head),
                             "carry": combined[-_CARRY_CHARS:], "capped": capped}
    # R2 субагент N2: initialized ставим только когда РЕАЛЬНО зарегистрировали файлы — транзиентный
    # сбой глоба на первом тике иначе превращал бы всю историю в «новые файлы» (алерты по старью)
    if state.files:
        state.initialized = True
    return results


def _classify_log_hits(log_hits: dict[str, dict[str, int]]) -> list[tuple[str, str, dict[str, int]]]:
    """Ярусы лог-сигнала: [(severity, класс, {файл: счёт})]. Прочие psycopg — INFO и только >=3."""
    out: list[tuple[str, str, dict[str, int]]] = []
    for cls, files in sorted(log_hits.items()):
        total = sum(files.values())
        if cls in _POISON_CLASSES or cls.startswith("turn_crash"):
            out.append(("P2", cls, files))
        elif total >= _OTHER_CLASS_MIN_COUNT:
            out.append(("INFO", cls, files))
    return out


def tick_once(state: _State, *, now: datetime | None = None,
              log_glob: str = _LOG_GLOB_DEFAULT) -> list[str]:
    """Один проход. Возвращает dedupe_key отправленных алертов (пусто = тихо). Sync (гоняем в thread).

    Дедуп повторов — внутри send_admin_alert (dedupe_key + окно severity; помечает «отправлено»
    только при успешной доставке). Каждая сигнатура — ОТДЕЛЬНЫЙ алерт со своим dedupe_key, чтобы
    новая сигнатура во время живого инцидента не глушилась ключом предыдущей.
    """
    now = now or _utcnow()
    sent: list[str] = []

    # 1) БД-сигнал по срезам
    slices: list[StuckSlice] = []
    try:
        with privileged_session("monitor") as session:
            slices = scan_stuck_inbound(session, now, _db_threshold())
    except Exception:  # noqa: BLE001
        logger.warning("crash_alert: db scan failed", exc_info=True)

    # 2) лог-сигнал (классы ошибок, ярусы)
    log_hits: dict[str, dict[str, int]] = {}
    try:
        log_hits = scan_logs(state, log_glob)
    except Exception:  # noqa: BLE001
        logger.warning("crash_alert: log scan failed", exc_info=True)

    for s in slices:
        key = f"crash_alert:db:{s.channel_type}:{s.bot_key}"
        who = ("; тенанты: " + ", ".join(s.tenants)) if s.tenants else ""
        try:
            send_admin_alert(
                "P2",
                f"⚠️ Крэши ходов: {s.channel_type}/{s.bot_key} (#335)",
                (f"{s.count} необработанных входящих за "
                 f"{int(_DB_WINDOW.total_seconds() // 60)} мин на срезе "
                 f"{s.channel_type}/{s.bot_key}{who}.\n"
                 f"Разбор: react_turn_trace (status=in_progress), греп логов по тенанту."),
                dedupe_key=key,
            )
            sent.append(key)
        except Exception:  # noqa: BLE001
            logger.warning("crash_alert: send failed for %s", key, exc_info=True)

    # Лог-хиты — через pending-повтор (ревью R2 high MAJOR): send_admin_alert fire-and-forget, а
    # офсеты уже съедены → недоставленный алерт без повтора терялся бы. Пишем алерт в pending и
    # шлём КАЖДЫЙ тик до TTL: успех гасится дедупом admin_alerts (метит «отправлено» только при
    # доставке; TTL 9 мин < P2-окна 600с → успешный не дублируется), неуспех реально ретраится.
    for severity, cls, files in _classify_log_hits(log_hits):
        key = f"crash_alert:log:{cls}"
        per_file = ", ".join(f"{f}×{n}" for f, n in sorted(files.items()))
        state.pending[key] = {
            "severity": severity,
            "title": f"⚠️ Ошибки в логах: {cls} (#335)",
            "body": (f"Класс {cls}: {per_file} (новые строки с прошлого тика).\n"
                     f"Разбор: греп по классу в /var/log/sreda/."),
            "expires": (now + _PENDING_TTL).isoformat(),
        }

    for key, p in list(state.pending.items()):
        try:
            if datetime.fromisoformat(p["expires"]) < now:
                state.pending.pop(key, None)
                continue
        except (ValueError, TypeError):
            state.pending.pop(key, None)
            continue
        try:
            send_admin_alert(p["severity"], p["title"], p["body"], dedupe_key=key)
            sent.append(key)
        except Exception:  # noqa: BLE001
            logger.warning("crash_alert: send failed for %s", key, exc_info=True)

    state.save()
    if sent:
        logger.warning("crash_alert: alerts sent: %s", ", ".join(sent))
    return sent


async def run_crash_alert_loop() -> None:
    """Фоновый цикл монитора (спавнится в job-runner). Best-effort: тик не может убить цикл."""
    if not _enabled():
        logger.info("crash_alert: disabled via SREDA_CRASH_ALERT_ENABLED=0")
        return
    logger.info("crash_alert: monitor started (tick=%ss)", _tick_seconds())
    # R1 субагент MAJOR: load ходит в БД синхронно — в thread, не блокировать event loop job-runner
    state = await asyncio.to_thread(_State.load)
    while True:
        try:
            await asyncio.to_thread(tick_once, state)
        except Exception:  # noqa: BLE001
            logger.exception("crash_alert: tick failed, continuing")
        await asyncio.sleep(_tick_seconds())
