"""Compute + store the admin overview snapshot (#292, путь 1).

The background loop in ``job_runner`` calls :func:`refresh_overview`
every few minutes; the ``/admin`` page only reads the stored row via
:func:`load_snapshot`. Page load therefore does ZERO network calls and
zero heavy aggregates (owner requirement 2026-07-02) — the freshest it
gets is "обновлено N мин назад".

Money: reuses ``services.llm_pricing`` (#150 F1) — cost is estimated
ONLY for (provider_key, model) pairs with a confirmed price; unpriced
pairs surface tokens with ``priced=False`` instead of invented dollars.
The per-call estimate formula is linear in prompt/completion tokens, so
computing it once over SUMMED tokens equals summing per-call estimates.

Fail-soft: every sub-block degrades independently (balances outage
must not hide spend, etc.); :func:`refresh_overview` never raises.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from sreda.db.models.admin_dashboard import AdminDashboardSnapshot
from sreda.db.models.skill_platform import SkillAIExecution

logger = logging.getLogger(__name__)

KEY_OVERVIEW = "overview"

# SkillAIExecutionStatus enum (features/skill_contracts.py): started /
# succeeded / failed / validation_failed. Errors = the two failure states.
_ERROR_STATUSES = ("failed", "validation_failed")

SLOW_MS = 30_000          # "медленный запрос" threshold for the overview list
_RECENT_LIMIT = 5         # rows in each of the errors/slow panels


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def compute_overview(session: Session, settings: Any) -> dict:
    """Assemble the full overview payload (JSON-serializable dict).

    Reuses the #150 dashboard queries (``get_cost_volume_summary``,
    ``get_top_tenants_by_spend``, ``gather_day_counts``) instead of
    duplicating their aggregation logic — this module only adds the
    24h error/slow lists and the snapshot plumbing.
    """
    now = datetime.now(UTC)
    since_24h = now - timedelta(hours=24)
    payload: dict[str, Any] = {"generated_at": now.isoformat()}

    # R1/R2 (high+medium MAJOR): КАЖДЫЙ session-блок через _safe — сбой
    # даёт пустой блок + rollback (иначе aborted PG-транзакция валит
    # последующие блоки и сам store_snapshot). _balances_block session
    # не трогает (сеть) — у него собственный try.
    payload["llm_24h"] = _safe(_llm_24h_block, {}, session, since_24h)
    payload["errors_recent"] = _safe(_errors_recent_block, [], session, since_24h)
    # 2026-07-03 (фидбек владельца): «медленные» = ТУРНЫ из trace.log
    # (skill_ai_executions.latency_ms на прод-пути пуст — SQL-фильтр
    # не матчил никогда). Не session-блок → свой fail-soft внутри.
    payload["slow_turns"] = _slow_turns_block(settings, now)
    payload["users"] = _safe(_users_block, {}, session, now)
    payload["purchases"] = _safe(_purchases_block, {}, session, now)
    # Отчёты трат считаем ОДИН раз (день/неделя/месяц) — их читают и
    # cost-блок обзора, и providers-блок раздела «LLM и деньги» (Фаза B).
    reports = _safe(_cost_reports, None, session)
    # R1 high MAJOR (Фаза B): трансформация тоже под _safe — сбой формы
    # отчёта не должен ронять весь рефреш.
    payload["cost"] = _safe(_cost_block, {}, reports) if reports else {}
    payload["health"] = _safe(_health_block, {}, session, now)
    payload["balances"] = _balances_block(settings)
    payload["providers"] = _safe(
        _providers_block, [], session, now, reports, payload["balances"])
    payload["llm_chain"] = _safe(
        _llm_chain_block, {"primary": "", "fallback": "", "planner": ""},
        session, settings)
    return payload


def _safe(fn, default, *args):
    """Fail-soft обёртка блока: сбой → дефолт + rollback сессии (после
    ошибочного SQL сессия в aborted-состоянии — без отката упадут и
    ВСЕ последующие блоки)."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        logger.warning("overview_snapshot: %s failed", fn.__name__, exc_info=True)
        for a in args:
            if isinstance(a, Session):
                try:
                    a.rollback()
                except Exception:  # noqa: BLE001
                    pass
                break
        return default


def _llm_24h_block(session: Session, since: datetime) -> dict:
    total = (
        session.query(func.count(SkillAIExecution.id))
        .filter(SkillAIExecution.created_at >= since)
        .scalar()
    ) or 0
    errors = (
        session.query(func.count(SkillAIExecution.id))
        .filter(
            SkillAIExecution.created_at >= since,
            SkillAIExecution.status.in_(_ERROR_STATUSES),
        )
        .scalar()
    ) or 0
    # slow-счётчик здесь удалён (2026-07-03): latency_ms на прод-пути
    # не заполняется — медленные считает _slow_turns_block по трейсам.
    return {
        "calls": total,
        "errors": errors,
        "error_rate_pct": round(errors / total * 100, 1) if total else 0.0,
    }


def _errors_recent_block(session: Session, since: datetime) -> list[dict]:
    rows = (
        session.query(SkillAIExecution)
        .filter(
            SkillAIExecution.created_at >= since,
            SkillAIExecution.status.in_(_ERROR_STATUSES),
        )
        .order_by(SkillAIExecution.created_at.desc())
        .limit(_RECENT_LIMIT)
        .all()
    )
    return [
        {
            "at": _iso(r.created_at),
            "status": r.status,
            "error_code": r.error_code or "",
            "task_type": r.task_type or "",
            "model": r.model or "",
            "tenant_id": r.tenant_id,
            "feature_key": r.feature_key,
        }
        for r in rows
    ]


def _slow_turns_block(settings: Any, now: datetime) -> dict:
    """Медленные ТУРНЫ (>= SLOW_MS) из trace.log — тот же источник, что
    /admin/traces. trace.log пишет UTC-таймстампы (сверено с БД по ходу
    22:05 02.07). tail-окно парсера ~5МБ покрывает сутки с запасом при
    текущем трафике (~50-100 ходов/день); count честно ограничен окном.
    Fail-soft: любая беда с логом → пустой блок."""
    try:
        path = getattr(settings, "trace_log_path", None)
        if not path:
            return {"count_24h": 0, "recent": []}
        from sreda.admin.trace_parser import parse_trace_log

        traces = parse_trace_log(path, tail_turns=500, min_total_ms=SLOW_MS)
        cutoff = now - timedelta(hours=24)
        in_window = []
        for t in traces:
            ts = _parse_trace_ts(t.timestamp)
            if ts is None or ts >= cutoff:
                # непарсибельный ts не выбрасываем (консервативно в тревогу)
                in_window.append((t, ts))
        in_window.sort(key=lambda p: p[0].total_ms or 0, reverse=True)
        recent = []
        for t, ts in in_window[:_RECENT_LIMIT]:
            # «Где застряло» = самая долгая размеченная стадия. Но если она
            # объясняет <50% общего времени — трейс НЕ разметил, куда ушло
            # остальное (владелец 2026-07-03: ход 39с с «voice.transcribe
            # 0.8с» вводил в заблуждение). Тогда честно «между стадиями».
            top_stage = ""
            stages = [s for s in t.stages if s.duration_ms]
            total = t.total_ms or 0
            if stages:
                s = max(stages, key=lambda s: s.duration_ms or 0)
                dur = s.duration_ms or 0
                if total and dur < total * 0.5:
                    top_stage = "между стадиями (не размечено)"
                else:
                    top_stage = f"{s.name} {round(dur / 1000, 1)} с"
            recent.append({
                "at": t.timestamp,
                "total_ms": t.total_ms or 0,
                "user_id": t.user_id or "",
                "channel": t.channel or "",
                "top_stage": top_stage,
            })
        return {"count_24h": len(in_window), "recent": recent}
    except Exception:  # noqa: BLE001
        logger.warning("overview_snapshot: slow turns failed", exc_info=True)
        return {"count_24h": 0, "recent": []}


def _parse_trace_ts(ts: str) -> datetime | None:
    """'2026-07-02 22:05:03.160' (UTC, naive в логе) → aware datetime."""
    try:
        return datetime.fromisoformat(ts.strip()).replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return None


def _users_block(session: Session, now: datetime) -> dict:
    """Регистрации (фидбек владельца 2026-07-03): всего / сегодня (MSK) / 7 дней.

    R1 medium MAJOR: дата «сегодня» — по МОСКОВСКОМУ поясу (now UTC:
    с 21:00 UTC в Москве уже завтра — now.date() дал бы вчерашний день).
    """
    from sreda.admin.queries import MSK_TZ, _msk_day_window_utc
    from sreda.db.models.core import Tenant

    today_start, _today_end = _msk_day_window_utc(now.astimezone(MSK_TZ).date())
    week_start = today_start - timedelta(days=6)
    total = session.query(func.count(Tenant.id)).scalar() or 0
    new_today = (
        session.query(func.count(Tenant.id))
        .filter(Tenant.created_at >= today_start).scalar()
    ) or 0
    new_7d = (
        session.query(func.count(Tenant.id))
        .filter(Tenant.created_at >= week_start).scalar()
    ) or 0
    return {"total": total, "new_today": new_today, "new_7d": new_7d}


def _purchases_block(session: Session, now: datetime) -> dict:
    """Покупки и оплаченные тенанты (задел под запуск продаж):
    оплаченных тенантов всего (distinct, когда-либо), заказов и сумма ₽
    за 7/30 дней по paid_at (status='paid' — сверено с billing.py:706).

    Считаем только РЕАЛЬНЫЕ платежи: provider='stub' — заглушечные/ручные
    «покупки» из раннего дева (payment_id=None), не выручка. Владелец
    2026-07-03: «оплаты пока не подключены» — виджет показывал 2 stub-заказа."""
    from sreda.db.models.billing import PaymentOrder

    is_real = PaymentOrder.provider_key != "stub"
    paid_tenants = (
        session.query(func.count(func.distinct(PaymentOrder.tenant_id)))
        .filter(PaymentOrder.status == "paid", is_real).scalar()
    ) or 0

    def _window(days: int) -> tuple[int, int]:
        since = now - timedelta(days=days)
        # R1 субагент MAJOR: paid_at nullable — paid-заказ без paid_at
        # не должен выпадать из счёта (fallback на created_at).
        paid_ts = func.coalesce(PaymentOrder.paid_at, PaymentOrder.created_at)
        row = (
            session.query(
                func.count(PaymentOrder.id),
                func.coalesce(func.sum(PaymentOrder.amount_rub), 0),
            )
            .filter(PaymentOrder.status == "paid", is_real, paid_ts >= since)
            .one()
        )
        return int(row[0] or 0), int(row[1] or 0)

    o7, s7 = _window(7)
    o30, s30 = _window(30)
    return {
        "paid_tenants": paid_tenants,
        "orders_7d": o7, "sum_rub_7d": s7,
        "orders_30d": o30, "sum_rub_30d": s30,
    }


_TOP_MODEL_ROWS = 8  # верхних строк модели на период в снапшоте


def _cost_reports(session: Session):
    """Отчёты трат для дашборда — СКОЛЬЗЯЩИЕ окна 24ч/7д/30д (владелец
    2026-07-03: календарные день/неделя/месяц давали «месяц < неделя» в
    начале месяца). Ключи "day"/"week"/"month" сохранены (их читают
    cost/providers-блоки и шаблон); меняются только подписи в шаблоне.
    Единый источник для cost и providers. Исключения — в _safe."""
    from sreda.admin.queries import get_spend_by_model

    now = datetime.now(UTC)
    windows = {
        "day": (now - timedelta(hours=24), now),
        "week": (now - timedelta(days=7), now),
        "month": (now - timedelta(days=30), now),
    }
    return {
        k: get_spend_by_model(session, k, window=w) for k, w in windows.items()
    }


def _cost_block(reports) -> dict:
    """Затраты+объём — тонкая JSON-обёртка над готовыми отчётами #150."""
    out: dict[str, Any] = {}
    for period, rep in reports.items():
        # R1 (high MAJOR): rows отсортированы «priced, затем unpriced» —
        # обрезка top-N режет беспрайсовые ПЕРВЫМИ. Несём их отдельным
        # НЕобрезанным списком: инвариант «беспрайсовая пара всегда
        # показывает токены» не должен зависеть от места в топе.
        unpriced = [r for r in rep.rows if not r.priced]
        out[period] = {
            "priced_subtotal_usd": float(round(rep.priced_subtotal_usd, 4)),
            "upper_subtotal_usd": float(round(rep.upper_subtotal_usd, 4)),
            "calls": sum(r.calls for r in rep.rows),
            "coverage_calls_pct": rep.coverage_calls_pct,
            "unpriced_calls": rep.unpriced_calls,
            "unpriced_tokens": rep.unpriced_tokens,
            "rows": [_row_dict(r) for r in rep.rows[:_TOP_MODEL_ROWS]],
            "unpriced_rows": [_row_dict(r) for r in unpriced],
        }
    return out


def _row_dict(r) -> dict:
    return {
        "provider_key": r.provider_key,
        "model": r.model,
        "calls": r.calls,
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "priced": r.priced,
        "est_usd": float(round(r.est_usd, 4)) if r.est_usd is not None else None,
        "upper_usd": float(round(r.upper_usd, 4)) if r.upper_usd is not None else None,
    }


def _health_block(session: Session, now: datetime) -> dict:
    """Здоровье диалога за 24ч — по ЖИВЫМ данным ReAct-ходов.

    Владелец 2026-07-03: прежний источник ``gather_day_counts`` читает
    таблицу ``agent_runs``, которая перестала писаться 23.06 (депрекейт
    старого планировщика) → виджет застыл на «0 ходов». Считаем по
    ``skill_ai_executions`` task_type='react_turn': ход = уникальный
    run_id; проблема = ход, где был react_turn с ошибкой.
    (Слепой автоотчёт надёжности #39 — отдельный issue, не здесь.)
    Исключения пробрасываются в _safe."""
    since = now - timedelta(hours=24)
    # SELECT = count(DISTINCT run_id) в ОБОИХ; .filter добавляет лишь WHERE,
    # НЕ меняя агрегат → failures_total = число уникальных run_id С ошибкой
    # (НЕ число ошибочных строк: мульти-итерационный ReAct с N ошибочными
    # react_turn в одном run считается ОДНИМ проблемным ходом). Тест это
    # фиксирует (test_health_failures_distinct_run_not_execution_count).
    base = session.query(func.count(func.distinct(SkillAIExecution.run_id))).filter(
        SkillAIExecution.created_at >= since,
        SkillAIExecution.task_type == "react_turn",
    )
    turns_total = base.scalar() or 0
    failures_total = (
        base.filter(SkillAIExecution.status.in_(_ERROR_STATUSES)).scalar()
    ) or 0
    return {"turns_total": turns_total, "failures_total": failures_total}


# Русские подписи ролей моделей — по РЕАЛЬНЫМ task_type из
# skill_ai_executions (прод 2026-07-03: react_turn / speech_recognition /
# summary). Неизвестный тип показываем как есть — не выдумываем.
_TASK_TYPE_LABELS = {
    "react_turn": "диалог",
    "speech_recognition": "распознавание речи",
    "summary": "пересказ истории",
    "planner.plan": "планирование",
    "composer.voice": "озвучка ответа",
    "planner": "планирование",
    "composer": "озвучка ответа",
}

# provider_key исполнений → ключ балансов provider_balances
# (ключи сверены по коду 2026-07-03: openrouter/mimo/groq/
# inception-mercury2/yandex — включая #229 Yandex Cloud ₽ и #157).
_BALANCE_PREFIXES = (
    ("openrouter", "openrouter"),
    ("groq", "groq"),
    ("mimo", "mimo"),
    ("inception", "inception-mercury2"),
    ("yandex", "yandex"),
)

_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "inception": "Inception (Mercury)",
    "groq": "Groq",
    "mimo": "MiMo",
    "yandex": "Yandex (STT)",
}


def _provider_prefix(provider_key: str) -> str:
    if not provider_key or not provider_key.strip():
        return "прочее"  # R1 субагент MINOR: "" не должен стать ключом
    for prefix, _bal in _BALANCE_PREFIXES:
        if provider_key.startswith(prefix):
            return prefix
    return provider_key.split("-")[0]


def _providers_block(
    session: Session, now: datetime, reports, balances: list[dict]
) -> list[dict]:
    """Карточки раздела «LLM и деньги» (Фаза B): на провайдера — траты
    день/неделя/месяц (из тех же отчётов #150), ошибки 24ч, модели с
    ролями (по task_type за 30 дней), баланс (по префиксу ключа).
    Исключения пробрасываются в _safe."""
    if not reports:
        return []
    # 1. траты по префиксу провайдера на период
    spend: dict[str, dict] = {}
    for period, rep in reports.items():
        for r in rep.rows:
            p = _provider_prefix(r.provider_key)
            per = spend.setdefault(p, {}).setdefault(
                period, {"est_usd": 0.0, "calls": 0, "unpriced_tokens": 0})
            per["calls"] += r.calls
            if r.priced and r.est_usd is not None:
                per["est_usd"] += float(r.est_usd)
            else:
                per["unpriced_tokens"] += r.prompt_tokens + r.completion_tokens

    # 2. ошибки за 24ч по провайдеру. sum(case) вместо FILTER-агрегата —
    # (R1 субагент) портативно на любой SQLite/PG без ограничений версии.
    from sqlalchemy import case

    since = now - timedelta(hours=24)
    err_rows = (
        session.query(
            SkillAIExecution.provider_key,
            func.count(SkillAIExecution.id),
            func.coalesce(func.sum(case(
                (SkillAIExecution.status.in_(_ERROR_STATUSES), 1), else_=0)), 0),
        )
        .filter(SkillAIExecution.created_at >= since,
                SkillAIExecution.provider_key.isnot(None),
                SkillAIExecution.provider_key != "")
        .group_by(SkillAIExecution.provider_key)
        .all()
    )
    errors: dict[str, dict] = {}
    for pk, calls, errs in err_rows:
        p = _provider_prefix(pk or "")
        e = errors.setdefault(p, {"calls": 0, "errors": 0})
        e["calls"] += int(calls)
        e["errors"] += int(errs)

    # 3. роли моделей по task_type за 30 дней
    role_rows = (
        session.query(
            SkillAIExecution.provider_key,
            SkillAIExecution.model,
            SkillAIExecution.task_type,
            func.count(SkillAIExecution.id),
        )
        .filter(SkillAIExecution.created_at >= now - timedelta(days=30),
                SkillAIExecution.provider_key.isnot(None))
        .group_by(SkillAIExecution.provider_key, SkillAIExecution.model,
                  SkillAIExecution.task_type)
        .all()
    )
    roles: dict[tuple[str, str], dict[str, int]] = {}
    for pk, model, task_type, cnt in role_rows:
        key = (_provider_prefix(pk or ""), model or "")
        roles.setdefault(key, {})[task_type or "?"] = int(cnt)

    # 4. модели на карточке — из месячного отчёта (полный охват)
    month = reports.get("month")
    models_by_provider: dict[str, list[dict]] = {}
    if month is not None:
        for r in month.rows:
            p = _provider_prefix(r.provider_key)
            tt = roles.get((p, r.model or ""), {})
            role_str = ", ".join(
                _TASK_TYPE_LABELS.get(t, t)
                for t, _c in sorted(tt.items(), key=lambda kv: -kv[1])[:3])
            models_by_provider.setdefault(p, []).append({
                "model": r.model or "",
                "roles": role_str,
                "calls": r.calls,
                "priced": r.priced,
                "est_usd": float(round(r.est_usd, 4)) if r.est_usd is not None else None,
                "tokens": r.prompt_tokens + r.completion_tokens,
            })

    # 5. баланс по префиксу
    bal_by_key = {b.get("key", ""): b for b in balances if isinstance(b, dict)}
    bal_map = dict(_BALANCE_PREFIXES)

    providers: list[dict] = []
    for p in sorted(spend, key=lambda p: -(spend[p].get("month", {}).get("est_usd") or 0)):
        e = errors.get(p, {"calls": 0, "errors": 0})
        providers.append({
            "key": p,
            "label": _PROVIDER_LABELS.get(p, p),
            "balance": bal_by_key.get(bal_map.get(p, ""), None),
            "spend": spend[p],
            "errors_24h": {
                **e,
                "rate_pct": round(e["errors"] / e["calls"] * 100, 1) if e["calls"] else 0.0,
            },
            "models": models_by_provider.get(p, []),
        })
    return providers


def _balances_block(settings: Any) -> list[dict]:
    """Provider balances via the existing 60s-cached service. Fail-soft:
    an outage yields an empty list, never breaks the snapshot."""
    try:
        from sreda.services.provider_balances import fetch_balances
        return [
            {
                "key": b.key,
                "label": b.label,
                "status": b.status,
                "headline": b.headline,
                "details": b.details,
            }
            for b in fetch_balances(settings)
        ]
    except Exception:  # noqa: BLE001 — snapshot must survive balance outages
        logger.warning("overview_snapshot: balances fetch failed", exc_info=True)
        return []


def _llm_chain_block(session: Session, settings: Any) -> dict:
    """Исключения пробрасываются в _safe (R2: rollback обязателен)."""
    from sreda.services import runtime_config as rc

    primary = rc.get_config(session, rc.KEY_CHAT_PROVIDER) or getattr(
        settings, "chat_provider", ""
    )
    fallback = rc.get_config(session, rc.KEY_CHAT_FALLBACK_PROVIDER) or getattr(
        settings, "chat_fallback_provider", ""
    ) or ""
    planner = getattr(settings, "planner_provider", "") or ""
    return {"primary": primary, "fallback": fallback, "planner": planner}


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


# --------------------------------------------------------------------------
# store / load
# --------------------------------------------------------------------------

def store_snapshot(session: Session, key: str, payload: dict) -> None:
    """Upsert the snapshot row. Commits (single-row write).

    R1 (все три ревьюера): атомарный ``INSERT ... ON CONFLICT DO UPDATE``
    вместо read-then-write — фоновый луп и ручной «обновить сейчас» могут
    первый-insert'ить одновременно (два процесса). Образец —
    ``react_summary_store.upsert_summary``.
    """
    vals = {
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "updated_at": datetime.now(UTC),
    }
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # незнакомый диалект → read-then-write (не прод)
        row = session.get(AdminDashboardSnapshot, key)
        if row is None:
            row = AdminDashboardSnapshot(key=key)
            session.add(row)
        for _k, _v in vals.items():
            setattr(row, _k, _v)
        session.commit()
        return
    stmt = _insert(AdminDashboardSnapshot).values(key=key, **vals)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={k: getattr(stmt.excluded, k) for k in vals},
    )
    session.execute(stmt)
    session.commit()


def normalize_overview(payload: dict) -> dict:
    """Привести снапшот к форме, которую ожидает шаблон (R1 medium MAJOR):
    устаревший/битый payload (после смены схемы, ручной правки БД) даёт
    пустые блоки и дефолтные типы, а не 500 на format-фильтрах.
    Только coercion — данные не выдумываются."""
    def _num(v, default=0.0):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default

    def _int(v, default=0):
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default

    def _str(v):
        return v if isinstance(v, str) else ""

    def _rows(v):
        out = []
        for r in (v if isinstance(v, list) else []):
            if not isinstance(r, dict):
                continue
            # R3 medium MINOR: bool — подкласс int; True не должен стать $0.0
            def _maybe_num(v):
                return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            est = _maybe_num(r.get("est_usd"))
            upper = _maybe_num(r.get("upper_usd"))
            out.append({
                "provider_key": _str(r.get("provider_key")),
                "model": _str(r.get("model")),
                "calls": _int(r.get("calls")),
                "prompt_tokens": _int(r.get("prompt_tokens")),
                "completion_tokens": _int(r.get("completion_tokens")),
                # R2 (high+medium MAJOR) + Фаза B R1 (high MAJOR): priced —
                # только НАСТОЯЩИЙ bool True И численный est (мусор 1/"yes"
                # из битого снапшота не должен показать доллары).
                "priced": (r.get("priced") is True) and est is not None,
                "est_usd": est,
                "upper_usd": upper,
            })
        return out

    if not isinstance(payload, dict):
        payload = {}
    norm: dict = {}
    b = payload.get("llm_24h")
    if isinstance(b, dict):
        norm["llm_24h"] = {
            "calls": _int(b.get("calls")),
            "errors": _int(b.get("errors")),
            "error_rate_pct": _num(b.get("error_rate_pct")),
        }
    st = payload.get("slow_turns")
    if isinstance(st, dict):
        norm["slow_turns"] = {
            "count_24h": _int(st.get("count_24h")),
            "recent": [
                {"at": _str(s.get("at")), "total_ms": _int(s.get("total_ms")),
                 "user_id": _str(s.get("user_id")), "channel": _str(s.get("channel")),
                 "top_stage": _str(s.get("top_stage"))}
                for s in (st.get("recent") or []) if isinstance(s, dict)
            ],
        }
    u = payload.get("users")
    if isinstance(u, dict) and u:
        norm["users"] = {k: _int(u.get(k)) for k in ("total", "new_today", "new_7d")}
    p = payload.get("purchases")
    if isinstance(p, dict) and p:
        norm["purchases"] = {k: _int(p.get(k)) for k in (
            "paid_tenants", "orders_7d", "sum_rub_7d", "orders_30d", "sum_rub_30d")}
    norm["errors_recent"] = [
        {"at": _str(e.get("at")), "status": _str(e.get("status")),
         "error_code": _str(e.get("error_code")), "task_type": _str(e.get("task_type")),
         "model": _str(e.get("model")), "tenant_id": _str(e.get("tenant_id")),
         "feature_key": _str(e.get("feature_key"))}
        for e in (payload.get("errors_recent") or []) if isinstance(e, dict)
    ]
    cost_in = payload.get("cost")
    if isinstance(cost_in, dict):
        cost: dict = {}
        for period, rep in cost_in.items():
            if not isinstance(rep, dict):
                continue
            cost[period] = {
                "priced_subtotal_usd": _num(rep.get("priced_subtotal_usd")),
                "upper_subtotal_usd": _num(rep.get("upper_subtotal_usd")),
                "calls": _int(rep.get("calls")),
                "coverage_calls_pct": (
                    _int(rep["coverage_calls_pct"])
                    if isinstance(rep.get("coverage_calls_pct"), (int, float))
                    and not isinstance(rep.get("coverage_calls_pct"), bool) else None
                ),
                "unpriced_calls": _int(rep.get("unpriced_calls")),
                "unpriced_tokens": _int(rep.get("unpriced_tokens")),
                "rows": _rows(rep.get("rows")),
                "unpriced_rows": _rows(rep.get("unpriced_rows")),
            }
        if cost:
            norm["cost"] = cost
    h = payload.get("health")
    if isinstance(h, dict) and h:
        norm["health"] = {k: _int(h.get(k)) for k in (
            "turns_total", "failures_total")}
    provs = payload.get("providers")
    if isinstance(provs, list):
        norm_provs = []
        for pr in provs:
            if not isinstance(pr, dict):
                continue
            bal = pr.get("balance")
            spend_in = pr.get("spend") if isinstance(pr.get("spend"), dict) else {}
            spend = {}
            for period, s in spend_in.items():
                if isinstance(s, dict):
                    spend[period] = {
                        "est_usd": _num(s.get("est_usd")),
                        "calls": _int(s.get("calls")),
                        "unpriced_tokens": _int(s.get("unpriced_tokens")),
                    }
            e = pr.get("errors_24h") if isinstance(pr.get("errors_24h"), dict) else {}
            norm_provs.append({
                "key": _str(pr.get("key")),
                "label": _str(pr.get("label")),
                "balance": ({
                    "status": _str(bal.get("status")),
                    "headline": _str(bal.get("headline")),
                    "details": _str(bal.get("details")),
                } if isinstance(bal, dict) else None),
                "spend": spend,
                "errors_24h": {
                    "calls": _int(e.get("calls")),
                    "errors": _int(e.get("errors")),
                    "rate_pct": _num(e.get("rate_pct")),
                },
                "models": [
                    (lambda est: {
                        "model": _str(m.get("model")), "roles": _str(m.get("roles")),
                        "calls": _int(m.get("calls")),
                        # строгий bool + числовой est (R1 high MAJOR)
                        "priced": (m.get("priced") is True) and est is not None,
                        "est_usd": est,
                        "tokens": _int(m.get("tokens"))})(
                        float(m["est_usd"])
                        if isinstance(m.get("est_usd"), (int, float))
                        and not isinstance(m.get("est_usd"), bool) else None)
                    for m in (pr.get("models") or []) if isinstance(m, dict)
                ],
            })
        norm["providers"] = norm_provs
    norm["balances"] = [
        {"key": _str(b2.get("key")), "label": _str(b2.get("label")),
         "status": _str(b2.get("status")), "headline": _str(b2.get("headline")),
         "details": _str(b2.get("details"))}
        for b2 in (payload.get("balances") or []) if isinstance(b2, dict)
    ]
    ch = payload.get("llm_chain")
    if isinstance(ch, dict):
        norm["llm_chain"] = {
            "primary": _str(ch.get("primary")),
            "fallback": _str(ch.get("fallback")),
            "planner": _str(ch.get("planner")),
        }
    return norm


def load_snapshot(session: Session, key: str) -> tuple[dict, datetime | None]:
    """Read a snapshot. Missing row / bad JSON → ({}, None) — the page
    renders "снапшот ещё не собран" instead of erroring."""
    row = session.get(AdminDashboardSnapshot, key)
    if row is None:
        return {}, None
    try:
        payload = json.loads(row.payload_json or "{}")
    except ValueError:
        logger.warning("overview_snapshot: bad JSON in %s — ignoring", key)
        return {}, None
    if not isinstance(payload, dict):
        return {}, None
    return payload, row.updated_at


# --------------------------------------------------------------------------
# refresh entrypoint (called from job_runner background loop)
# --------------------------------------------------------------------------

async def run_overview_refresh_loop() -> None:
    """Always-on loop: пересбор снапшота каждые N секунд
    (``admin_dashboard_refresh_seconds``; ≤0 → луп выключен).

    Spawned as background task в job_runner (тот же паттерн, что
    max_subscription_health). Ошибки итерации не убивают луп;
    ``refresh_overview`` синхронный (БД + сетевые балансы) → в thread,
    чтобы не блокировать event loop очереди сообщений.
    """
    import asyncio

    # R2 (high MINOR): ВСЁ тело в try — сбой ДО первого витка (настройки,
    # фабрика сессий) логируется, а не молча убивает task.
    try:
        from sreda.config.settings import get_settings
        from sreda.db.session import get_session_factory

        interval = float(get_settings().admin_dashboard_refresh_seconds or 0)
        if interval <= 0:
            logger.info("overview refresh loop disabled (interval<=0)")
            return
        while True:
            try:
                await asyncio.to_thread(
                    refresh_overview, get_session_factory(), get_settings()
                )
            except Exception:  # noqa: BLE001 — луп живёт при любых сбоях
                logger.exception("overview refresh loop: iteration failed")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("overview refresh loop: startup failed — loop dead")


def refresh_overview(session_factory, settings) -> bool:
    """One refresh pass. NEVER raises (the loop must survive anything);
    returns True on success for logging/tests."""
    try:
        session = session_factory()
    except Exception:  # noqa: BLE001
        logger.warning("overview_snapshot: session open failed", exc_info=True)
        return False
    try:
        payload = compute_overview(session, settings)
        store_snapshot(session, KEY_OVERVIEW, payload)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("overview_snapshot: refresh failed", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        session.close()
