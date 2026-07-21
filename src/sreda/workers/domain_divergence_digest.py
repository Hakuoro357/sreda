"""#410 — недельный разбор расхождений доменов (#376) вместо потока per-turn алертов.

Механизм дизамбигуации (#376) сравнивает статический доменный роутер с вердиктом
Фредди-классификатора. Раньше КАЖДОЕ расхождение уходило владельцу отдельным
сообщением (``react_loop._notify_domain_divergence``, in-process dedup на час) —
служебный канал шумел. Решение владельца 2026-07-21: не глушить, а копить и раз в
неделю выдавать разбор «где расходятся, как часто, по каким формулировкам, что чинить».

Новой персистенции НЕ нужно: блок ``disambig`` уже пишется в
``react_turn_trace.routing_decision_json`` на каждом ходе при включённом гейте #376
(``react_loop.py``: ``_rdj["disambig"] = _dis376``). Форма блока::

    {"ran": bool, "duration_ms": int, "confidence": "high"|"low",
     "freddie_domains": [...], "static_domains": [...],
     "kind": "subtract"|"add"|None, "applied": bool}
    {"ran": True, "error": True}          # сбой дизамбигуации (fail-open на базовую политику)
    {"ran": False}                        # гейт не отработал на этом ходе

**Предикат расхождения** воспроизводит тот, что раньше решал слать ли алерт
(``kind == "add" or (kind == "subtract" and политика изменилась)``); второе слагаемое
тождественно ``applied`` (react_loop выставляет ``applied = kind=="subtract" and changed``).

**Что чинить в первую очередь.** Ранжирование: сначала НЕприменённые расхождения
(``kind="add"`` — классификатор увидел раздел вне поднятых статиком; вердикт не
применяется как анти-инъекционная мера, значит ход ушёл к модели как есть — это либо
дыра статического роутера, либо инъекция), затем применённые вычитания (механизм уже
исправил статик сам; частота показывает, где статик системно шире нужного).
Внутри группы — по частоте убыванием.

**ПД.** Формулировки пользователей берутся из ``origin_user_text`` (EncryptedString,
ORM расшифровывает) ТОЛЬКО для ограниченного числа строк-примеров, проходят скраббер
(ссылки/почта/@-хэндлы/цифры) и обрезку, дедуплицируются и режутся до
``EXAMPLES_PER_CASE`` на случай. Сбой чтения/расшифровки → отчёт уходит без примеров.

Каденс и устойчивость — по образцу ``reliability_report`` (#139): state-файл (+запасной
путь), не чаще раза в ISO-неделю, откат после провала вместо шторма, серия провалов → P1.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.session import privileged_session
from sreda.services.admin_alerts import send_admin_alert

logger = logging.getLogger("sreda.domain_divergence")

# state — отметка «эта неделя уже отчитана»; /tmp стирается ребутом, потому /var/lib
DEFAULT_STATE_FILE = "/var/lib/sreda/domain-divergence-state.json"
FALLBACK_STATE_FILE = "/tmp/sreda-domain-divergence-state-fallback.json"
REPORT_WINDOW = timedelta(days=7)
FAILURE_BACKOFF = timedelta(minutes=30)
ALERT_AFTER_CONSECUTIVE_FAILURES = 3
# сколько формулировок-лидеров показываем на случай расхождения
EXAMPLES_PER_CASE = 2
# потолок строк ОДНОГО случая, чей текст расшифровывается ради частотной статистики.
# CR R2 (sol+terra MAJOR): глобальный потолок обрывал набор ДО подсчёта частот и без
# ORDER BY давал произвольную подвыборку — массовая фраза могла не попасть в топ, а
# напечатанное xN не было недельной частотой. Потолок теперь ПО-КЕЙСНЫЙ, и если он
# сработал, случай честно помечается выборкой (см. DivergenceCase.examples_sampled).
MAX_EXAMPLE_ROWS_PER_CASE = 1000
# Отчёт идёт ДВУМЯ секциями с раздельными потолками. Общий топ-N не годится: живой
# прогон 2026-07-21 (214 ходов, 92 расхождения) дал 19 неприменённых, размазанных по
# 8 подписям, и 73 применённых всего в 2 подписи — единый список из 8 строк выдавил
# применённые целиком, хотя это 4/5 объёма.
TOP_CASES_UNAPPLIED = 6
TOP_CASES_APPLIED = 4
# верхняя граница скана недели: канарейка даёт сотни ходов, wildcard — тысячи;
# жёсткий потолок бережёт память/латентность и честно отмечается в отчёте
MAX_ROWS = 20000
_EXAMPLE_FETCH_CHUNK = 200
MAX_PHRASE_LEN = 60

_RE_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_HANDLE = re.compile(r"@[\w_]+")
_RE_DIGITS = re.compile(r"\d+")
_RE_WS = re.compile(r"\s+")


def scrub_phrase(text: str | None, *, max_len: int = MAX_PHRASE_LEN) -> str:
    """Снизить чувствительность формулировки для служебного отчёта.

    Порядок важен: ссылки и почта вычищаются ДО @-хэндлов и цифр (иначе от почты
    остаётся хвост домена). Цифры схлопываются в «#» — телефоны/адреса/суммы/даты
    не должны утекать даже в служебный канал; для калибровки роутера важна форма
    фразы, а не числа в ней. Длинные тире → дефисы (проектное ограничение на
    исходящие тексты; CR R1 terra MINOR).

    ⚠️ Это НЕ анонимизация (CR R1 sol MAJOR / terra CRITICAL): имена, адреса и
    прочий свободный чувствительный текст регулярками не вычищаются в принципе.
    Поэтому формулировки вообще попадают в отчёт ТОЛЬКО для тенантов из
    privacy-allowlist ``admin_alert_preview_tenants`` (пусто по умолчанию → ни
    для кого). Скраббер — второй рубеж, а не единственный.
    """
    if not text:
        return ""
    s = _RE_URL.sub("<ссылка>", str(text))
    s = _RE_EMAIL.sub("<почта>", s)
    s = _RE_HANDLE.sub("<имя>", s)
    s = _RE_DIGITS.sub("#", s)
    s = s.replace("—", "-").replace("–", "-")
    s = _RE_WS.sub(" ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def _domains_tuple(raw: object) -> tuple[str, ...] | None:
    """Список доменов из трейса → кортеж строк; всё непохожее (в т.ч. ОТСУТСТВИЕ) → None.

    CR R1 sol MAJOR: без этого объект внутри ``static_domains`` даёт
    ``TypeError: unhashable type`` при сборке ключа и роняет ВЕСЬ недельный отчёт.
    CR R2 (sol+terra MAJOR): отсутствующий список — тоже битая форма, а не «пусто»:
    ``react_loop`` на живом ходе ВСЕГДА пишет оба списка (react_loop.py:5511-5514),
    поэтому их нехватка означает разъехавшуюся схему и должна быть видна в отчёте.
    """
    if not isinstance(raw, (list, tuple)):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        out.append(item)
    return tuple(out)


def _validate_disambig(dis: dict) -> tuple[str | None, bool, tuple, tuple] | None:
    """Строгая проверка формы живого блока ``disambig``. None → строка битая.

    Инварианты берутся из того, что реально пишет ``react_loop`` (строки 5508-5516):
    ``kind`` ∈ {None, "subtract", "add"}; ``applied`` — НАСТОЯЩИЙ bool и он тождествен
    ``kind == "subtract" and политика изменилась``, значит при ``kind`` = "add"/None
    он может быть только False. CR R2 (sol+terra MAJOR): раньше семантически
    невозможный ``kind="add", applied=True`` проходил и портил разбивку.
    """
    kind = dis.get("kind")
    applied = dis.get("applied")
    static_d = _domains_tuple(dis.get("static_domains"))
    freddie_d = _domains_tuple(dis.get("freddie_domains"))
    if kind not in (None, "subtract", "add"):
        return None
    if not isinstance(applied, bool):
        return None
    if applied and kind != "subtract":
        return None
    if static_d is None or freddie_d is None:
        return None
    return kind, applied, static_d, freddie_d


@dataclass(frozen=True)
class DivergenceCase:
    """Один класс расхождения = (вид, что поднял статик, что сказал классификатор)."""

    kind: str                              # subtract | add
    static_domains: tuple[str, ...]
    freddie_domains: tuple[str, ...]
    applied: bool
    count: int
    # (формулировка, сколько раз встретилась) — лидеры частоты, не «первые попавшиеся»
    examples: tuple[tuple[str, int], ...] = ()
    # частоты посчитаны не по всем строкам случая (упёрлись в по-кейсный потолок)
    examples_sampled: bool = False

    @property
    def signature(self) -> str:
        return (",".join(self.static_domains) or "-") + " → " + \
               (",".join(self.freddie_domains) or "-")


@dataclass(frozen=True)
class WeekCounts:
    turns_with_disambig: int    # ходов, где дизамбигуация реально отработала (ran=True)
    divergences: int            # из них разошлись статик и классификатор
    subtract_applied: int       # вердикт применён (лишние разделы вычтены)
    add_not_applied: int        # вердикт вне поднятых — НЕ применён (анти-инъекция)
    disambig_errors: int        # сбой самого механизма (fail-open, исход хода это прячет)
    cases: tuple[DivergenceCase, ...] = ()
    truncated: bool = False     # уперлись в MAX_ROWS — отчёт по части недели
    malformed: int = 0          # блок disambig не той формы (схема разъехалась)
    # расхождения есть, но ни одна строка не из privacy-allowlist → формулировок нет
    examples_suppressed: bool = False
    _debug: dict = field(default_factory=dict, compare=False, repr=False)


def _rank_key(case: DivergenceCase) -> tuple:
    """«Что чинить в первую очередь»: неприменённые впереди применённых, внутри —
    по частоте убыванием; подпись третьим ключом даёт детерминированный порядок."""
    return (0 if not case.applied else 1, -case.count, case.signature)


def _fetch_example_texts(session: Session, ids: list[str]) -> dict[str, str]:
    """Тексты ходов-примеров по id. Отдельным проходом и ТОЛЬКО для отобранных строк:
    ``origin_user_text`` — EncryptedString, читать всю неделю ради двух примеров и
    дорого, и лишняя работа с ПД."""
    out: dict[str, str] = {}
    for i in range(0, len(ids), _EXAMPLE_FETCH_CHUNK):
        chunk = ids[i:i + _EXAMPLE_FETCH_CHUNK]
        rows = session.execute(
            select(ReactTurnTrace.id, ReactTurnTrace.origin_user_text)
            .where(ReactTurnTrace.id.in_(chunk))).all()
        for rid, txt in rows:
            if txt:
                out[str(rid)] = str(txt)
    return out


def _preview_tenants() -> frozenset[str]:
    """Privacy-allowlist #149 M5: чьи тексты вообще можно показывать в служебном
    сообщении владельцу. Строгий список без «*», пусто по умолчанию."""
    from sreda.config.settings import get_settings
    return get_settings().admin_alert_preview_tenants


def _scan_scope() -> tuple[str, list[str]]:
    """Что вообще нужно сканировать: ``("none"|"all"|"list", тенанты)``.

    CR R1 terra MAJOR: без сужения недельный запрос идёт по ВСЕМУ трейсу окна, а
    ``MAX_ROWS`` ограничивает результат, не скан.
    CR R2 terra MAJOR: пустой/выключенный гейт надо ОТЛИЧАТЬ от «всем» — при
    выключенном #376 блоков disambig нет в принципе, и глобальный скан на первом
    недельном тике зря придерживал бы job-runner для всех тенантов.

    ``_ReactTenantGate`` в режиме «*» truthy, но итерируется пусто (см. settings.py),
    поэтому «всем» и «никому» различаются именно так: truthy + пустая итерация = «*».
    """
    from sreda.config.settings import get_settings
    try:
        s = get_settings()
        if not s.domain_clf_disambig_enabled:
            return ("none", [])
        gate = s.domain_clf_disambig_tenants
        if not gate:
            return ("none", [])
        explicit = sorted(gate)
        return ("list", explicit) if explicit else ("all", [])
    except Exception:  # noqa: BLE001 — конфиг не должен ронять отчёт
        return ("all", [])


def gather_week_counts(session: Session, *, now: datetime,
                       window: timedelta = REPORT_WINDOW,
                       scope: tuple[str, list[str]] | None = None) -> WeekCounts:
    """Свести расхождения доменов за окно по durable-трейсу ходов.

    ``scope`` — что сканировать: ``("none", [])`` (ничего, SQL вообще не идёт),
    ``("all", [])`` (весь трейс окна) или ``("list", [тенанты])``. ``None`` →
    определить по настройкам. Воркер передаёт ОБЪЕДИНЕНИЕ текущего гейта с гейтом
    прошлого прогона (CR R2 sol MAJOR: окно охватывает прошедшие 7 дней, а текущая
    конфигурация не есть источник истины за весь этот исторический период — смена
    канарейки посреди недели иначе молча теряла бы расхождения прежнего тенанта).
    """
    mode, scope_tenants = scope if scope is not None else _scan_scope()
    if mode == "none":
        # #376 никому не включён и не был включён → блоков disambig нет; не трогаем БД
        return WeekCounts(turns_with_disambig=0, divergences=0, subtract_applied=0,
                          add_not_applied=0, disambig_errors=0)
    since = now - window
    # LIKE сужает выборку на стороне БД (портируется и на SQLite, и на Postgres —
    # JSON-операторы у них разные), окончательный разбор — в Python по JSON.
    # ORDER BY снят (CR R1 terra MAJOR): сортировка по created_at без подходящего
    # глобального индекса гонялась по всему окну, а порядок строк отчёту не нужен —
    # примеры теперь ранжируются по ЧАСТОТЕ, а не по «кто раньше».
    stmt = (select(ReactTurnTrace.id, ReactTurnTrace.tenant_id,
                   ReactTurnTrace.routing_decision_json)
            .where(and_(
                ReactTurnTrace.created_at >= since,
                ReactTurnTrace.created_at < now,
                ReactTurnTrace.routing_decision_json.is_not(None),
                ReactTurnTrace.routing_decision_json.like('%"disambig"%'),
            )))
    if mode == "list" and scope_tenants:
        stmt = stmt.where(ReactTurnTrace.tenant_id.in_(scope_tenants))
    rows = session.execute(stmt.limit(MAX_ROWS + 1)).all()
    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]
        logger.warning("domain_divergence: упёрлись в потолок %d строк за неделю", MAX_ROWS)

    turns = 0
    errors = 0
    malformed = 0
    sub_applied = 0
    add_not_applied = 0
    preview = _preview_tenants()
    # подпись случая → [счётчик, id строк-кандидатов на текст, сколько кандидатов отброшено]
    buckets: dict[tuple, list] = {}
    any_divergence_outside_preview = False

    for rid, tenant_id, raw in rows:
        try:
            rdj = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        dis = rdj.get("disambig") if isinstance(rdj, dict) else None
        if not isinstance(dis, dict) or dis.get("ran") is not True:
            continue
        turns += 1
        if dis.get("error") is True:
            errors += 1
            continue
        parsed = _validate_disambig(dis)
        if parsed is None:
            malformed += 1
            continue
        kind, applied, static_d, freddie_d = parsed
        # предикат ровно как у погашенного per-turn алерта (react_loop.py:5520):
        # применённое вычитание ИЛИ неприменённое добавление. Согласие (вердикт
        # совпал, политика не менялась) расхождением НЕ считается.
        if not (kind == "add" or (kind == "subtract" and applied)):
            continue
        if kind == "add":
            add_not_applied += 1
        else:
            sub_applied += 1
        key = (str(kind), static_d, freddie_d, applied)
        slot = buckets.setdefault(key, [0, [], 0])
        slot[0] += 1
        # текст берём ТОЛЬКО у тенантов из privacy-allowlist (CR R1 sol/terra):
        # скраббер не есть анонимизация, поэтому по умолчанию формулировок нет ни у кого
        if str(tenant_id) in preview:
            if len(slot[1]) < MAX_EXAMPLE_ROWS_PER_CASE:
                slot[1].append(str(rid))
            else:
                slot[2] += 1     # частоты этого случая станут выборочными
        else:
            any_divergence_outside_preview = True

    # тексты — вторым проходом и только по отобранным строкам. Сбой чтения/расшифровки
    # не должен съедать весь отчёт: деградируем до «без формулировок».
    example_ids = [rid for _cnt, ids, _skipped in buckets.values() for rid in ids]
    texts: dict[str, str] = {}
    if example_ids:
        try:
            texts = _fetch_example_texts(session, example_ids)
        except Exception:  # noqa: BLE001 — отчёт важнее примеров
            logger.warning("domain_divergence: формулировки недоступны", exc_info=True)
            texts = {}

    cases: list[DivergenceCase] = []
    for (kind, static_d, freddie_d, applied), (cnt, ids, skipped) in buckets.items():
        # CR R1 sol MAJOR: «топ формулировок» = лидеры ЧАСТОТЫ по всем строкам случая,
        # а не первые попавшиеся — иначе редкая старая фраза вытесняет ту, что
        # повторилась сто раз, и рабочая очередь врёт о приоритетах.
        freq: dict[str, int] = {}
        for rid in ids:
            phrase = scrub_phrase(texts.get(rid))
            if phrase:
                freq[phrase] = freq.get(phrase, 0) + 1
        top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:EXAMPLES_PER_CASE]
        cases.append(DivergenceCase(
            kind=kind, static_domains=static_d, freddie_domains=freddie_d,
            applied=applied, count=cnt, examples=tuple(top),
            examples_sampled=bool(skipped)))
    cases.sort(key=_rank_key)

    return WeekCounts(
        turns_with_disambig=turns,
        divergences=sub_applied + add_not_applied,
        subtract_applied=sub_applied,
        add_not_applied=add_not_applied,
        disambig_errors=errors,
        cases=tuple(cases),
        truncated=truncated,
        malformed=malformed,
        examples_suppressed=any_divergence_outside_preview,
    )


def format_report(counts: WeekCounts, *, now: datetime,
                  window: timedelta = REPORT_WINDOW) -> str:
    """Служебный отчёт владельцу. Технические имена разделов здесь УМЕСТНЫ (канал
    служебный, не пользовательский), но формулировки пользователей — обезличены."""
    since = now - window
    head = (f"🧭 Расхождения доменов (#376) за неделю "
            f"{since.strftime('%d.%m')} - {now.strftime('%d.%m')}")
    lines = [head]

    # CR R2 (sol+terra MAJOR): счётчики сбоев/битых строк и отметка усечения должны быть
    # видны во ВСЕХ ветках. Иначе неделя, где все строки битые, рапортует «согласны».
    def _health(out: list[str]) -> None:
        if counts.disambig_errors:
            out.append(f"сбоев дизамбигуации: {counts.disambig_errors}")
        if counts.malformed:
            out.append(f"строк трейса не той формы (пропущены): {counts.malformed}")
        if counts.truncated:
            out.append(f"(скан упёрся в потолок {MAX_ROWS} ходов - неделя показана частично)")

    if counts.turns_with_disambig == 0:
        lines.append("механизм дизамбигуации за неделю не срабатывал (0 ходов): "
                     "гейт снят или task-ходов на канарейке не было.")
        _health(lines)
        return "\n".join(lines)

    if counts.divergences == 0:
        _agree = ("расхождений нет - статик и классификатор согласны."
                  if not counts.malformed
                  else "расхождений не насчитано.")
        lines.append(f"ходов с дизамбигуацией: {counts.turns_with_disambig}; {_agree}")
        _health(lines)
        return "\n".join(lines)

    pct = 100.0 * counts.divergences / counts.turns_with_disambig
    lines.append(f"ходов с дизамбигуацией: {counts.turns_with_disambig}; "
                 f"расхождений: {counts.divergences} ({pct:.1f}%)")
    lines.append(f"из них вычтено классификатором (применено): {counts.subtract_applied}; "
                 f"добавление вне статика (не применено): {counts.add_not_applied}")
    _health(lines)
    if counts.examples_suppressed:
        lines.append("формулировки части тенантов скрыты: их нет в privacy-allowlist "
                     "SREDA_ADMIN_ALERT_PREVIEW_TENANTS")

    unapplied = [c for c in counts.cases if not c.applied]
    applied = [c for c in counts.cases if c.applied]
    _section(lines, "Чинить в первую очередь (вердикт НЕ применён - ход ушёл как есть):",
             unapplied, TOP_CASES_UNAPPLIED)
    _section(lines, "Вычтено классификатором (статик системно шире нужного):",
             applied, TOP_CASES_APPLIED)
    return "\n".join(lines)


def _section(lines: list[str], title: str, cases: list[DivergenceCase], top: int) -> None:
    """Одна секция списка случаев. Пустая секция не печатается вовсе."""
    if not cases:
        return
    lines.append("")
    lines.append(title)
    for i, case in enumerate(cases[:top], start=1):
        lines.append(f"{i}. {case.signature} · ходов: {case.count}")
        if case.examples:
            # «по выборке» — честная метка, если частоты посчитаны не по всем строкам
            # случая (CR R2: выборочное xN нельзя выдавать за недельную частоту)
            _mark = " (по выборке)" if case.examples_sampled else ""
            lines.append("   частые формулировки: "
                         + "; ".join(f"«{p}» x{n}" for p, n in case.examples) + _mark)
    hidden = len(cases) - top
    if hidden > 0:
        lines.append(f"   ещё видов в этой группе: {hidden} (реже)")


def _week_key(now: datetime) -> str:
    """Ключ ISO-недели: каденс «раз в неделю» = смена календарной недели UTC
    (стабильнее интервала — не плывёт от пропущенных тиков, как и суточный #139)."""
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _merge_scope(current: tuple[str, list[str]],
                 prev_mode: object, prev_tenants: object) -> tuple[str, list[str]]:
    """Объединить область скана текущего гейта с гейтом ПРОШЛОГО прогона.

    CR R2 sol MAJOR: окно отчёта охватывает прошедшие 7 дней, поэтому сегодняшняя
    конфигурация — не источник истины за весь этот период. Смена канарейки (или снятие
    гейта) посреди недели иначе молча выкидывала бы валидные расхождения прежнего
    тенанта. Объединение self-healing: в state лежит только гейт последнего прогона,
    накапливать историю не нужно — прогоны идут раз в неделю, ровно по ширине окна.
    """
    mode, tenants = current
    if mode == "all" or prev_mode == "all":
        return ("all", [])
    merged = set(tenants)
    if isinstance(prev_tenants, list):
        merged |= {str(t) for t in prev_tenants if isinstance(t, str)}
    return ("list", sorted(merged)) if merged else ("none", [])


def _gather_with_session(now: datetime,
                         scope: tuple[str, list[str]] | None = None) -> WeekCounts:
    """Срез по трейсу (#138 Ф2: глобальный COUNT → privileged-сессия, как у #139)."""
    with privileged_session("monitor") as session:
        return gather_week_counts(session, now=now, scope=scope)


class DomainDivergenceDigestWorker:
    """Шлёт разбор расхождений не чаще раза в ISO-неделю; провал → откат (не шторм)."""

    def __init__(self, *, state_file: str | None = None) -> None:
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        # как в #139: если основной каталог неписабелен, отметку о провале должен
        # писать ДРУГОЙ писатель — иначе откат не работает и получается шторм
        self.fallback_state_file = Path(FALLBACK_STATE_FILE)

    async def process_pending(self, *, now: datetime | None = None) -> int:
        import asyncio as _aio

        now = now or datetime.now(timezone.utc)
        state = self._read_state()
        if not self._should_run(now, state):
            return 0
        week = _week_key(now)
        current_scope = _scan_scope()
        scope = _merge_scope(current_scope, state.get("last_scan_mode"),
                             state.get("last_scan_tenants"))
        try:
            # скан недели + расшифровка примеров — в поток: job_runner не должен
            # замирать на время запроса (раз в неделю, но выборка до MAX_ROWS)
            counts = await _aio.to_thread(_gather_with_session, now, scope)
            send_admin_alert(
                "INFO", "расхождения доменов - недельный разбор",
                format_report(counts, now=now),
                dedupe_key=f"domain_divergence:weekly:{week}",
            )
        except Exception:  # noqa: BLE001 — не валим job_runner
            logger.exception("domain divergence digest failed")
            failures = int(state.get("failure_count") or 0) + 1
            state["last_failure_at"] = now.isoformat()
            state["failure_count"] = failures
            self._write_state(state)
            if failures >= ALERT_AFTER_CONSECUTIVE_FAILURES:
                try:  # серия провалов — не тихое гниение (#127-паттерн)
                    send_admin_alert(
                        "P1", "недельный разбор расхождений падает серией",
                        f"{failures} провалов подряд - лог sreda.domain_divergence",
                        dedupe_key="domain_divergence:consecutive_failures")
                except Exception:  # noqa: BLE001
                    logger.exception("domain_divergence: alert delivery failed")
            return 0
        state["last_run_week"] = week
        # гейт ЭТОГО прогона — чтобы следующая неделя знала, что было в начале окна
        state["last_scan_mode"] = current_scope[0]
        state["last_scan_tenants"] = current_scope[1]
        state.pop("last_failure_at", None)
        state["failure_count"] = 0
        if not self._write_state(state):
            # неделя НЕ зачтена (отметка не легла) → уходим в откат СО счётчиком серии,
            # иначе следующий тик пошлёт тот же отчёт заново (#139 high R2)
            state.pop("last_run_week", None)
            state["last_failure_at"] = now.isoformat()
            state["failure_count"] = int(state.get("failure_count") or 0) + 1
            self._write_state(state)
            return 0
        logger.info("domain divergence digest sent: %s", week)
        return 1

    def _should_run(self, now: datetime, state: dict) -> bool:
        if state.get("last_run_week") == _week_key(now):
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
        # основной файл может быть ЧИТАЕМ, но устаревшим (записи шли в запасной) —
        # читаем оба и берём свежайший (#139 high R3)
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
            stamps = [self._parse_ts(d.get("last_failure_at"))]
            stamps = [t for t in stamps if t is not None]
            week = str(d.get("last_run_week") or "")
            return (week, max(stamps) if stamps else datetime.min.replace(tzinfo=timezone.utc))

        return max(candidates, key=freshness)

    def _write_state(self, state: dict) -> bool:
        payload = json.dumps(state, ensure_ascii=False)
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(payload, encoding="utf-8")
            return True
        except OSError:
            logger.warning("domain_divergence: state file %s недоступен - пробую запасной",
                           self.state_file)
        try:
            self.fallback_state_file.write_text(payload, encoding="utf-8")
            return True
        except OSError:
            logger.warning("domain_divergence: запасной state %s тоже недоступен",
                           self.fallback_state_file)
            return False
