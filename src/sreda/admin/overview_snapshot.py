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
    payload["slow_recent"] = _safe(_slow_recent_block, [], session, since_24h)
    payload["cost"] = _safe(_cost_block, {}, session)
    payload["health"] = _safe(_health_block, {}, session, now)
    payload["top_tenants"] = _safe(
        _top_tenants_block, {"by_spend": [], "by_unpriced": []}, session)
    payload["balances"] = _balances_block(settings)
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
    slow = (
        session.query(func.count(SkillAIExecution.id))
        .filter(
            SkillAIExecution.created_at >= since,
            SkillAIExecution.latency_ms >= SLOW_MS,
        )
        .scalar()
    ) or 0
    return {
        "calls": total,
        "errors": errors,
        "error_rate_pct": round(errors / total * 100, 1) if total else 0.0,
        "slow": slow,
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


def _slow_recent_block(session: Session, since: datetime) -> list[dict]:
    rows = (
        session.query(SkillAIExecution)
        .filter(
            SkillAIExecution.created_at >= since,
            SkillAIExecution.latency_ms >= SLOW_MS,
        )
        .order_by(SkillAIExecution.latency_ms.desc())
        .limit(_RECENT_LIMIT)
        .all()
    )
    return [
        {
            "at": _iso(r.created_at),
            "latency_ms": r.latency_ms,
            "task_type": r.task_type or "",
            "model": r.model or "",
            "tenant_id": r.tenant_id,
            "feature_key": r.feature_key,
        }
        for r in rows
    ]


_TOP_MODEL_ROWS = 8  # верхних строк модели на период в снапшоте


def _cost_block(session: Session) -> dict:
    """Затраты+объём день/неделя/месяц — тонкая JSON-обёртка над
    ``get_cost_volume_summary`` (#150). Исключения пробрасываются —
    fail-soft (+rollback) обеспечивает _safe в compute_overview (R2)."""
    from sreda.admin.queries import get_cost_volume_summary

    reports = get_cost_volume_summary(session)
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
    """Здоровье диалога за сутки — обёртка над ``gather_day_counts``
    (reliability_report). Исключения пробрасываются в _safe (R2)."""
    from sreda.workers.reliability_report import gather_day_counts

    c = gather_day_counts(session, now=now, breakdowns_precounted=0)
    return {
        "turns_total": c.turns_total,
        "runs_failed": c.runs_failed,
        "inbound_stuck": c.inbound_stuck,
        "outbox_failed": c.outbox_failed,
        "breakdowns_shown": c.breakdowns_shown,
        "failures_total": c.failures_total,
    }


def _top_tenants_block(session: Session, limit: int = 5) -> dict:
    """Исключения пробрасываются в _safe (R2: rollback обязателен)."""
    from sreda.admin.queries import get_top_tenants_by_spend

    top = get_top_tenants_by_spend(session, "month", limit=limit)
    return {
        "by_spend": [
            {"tenant_id": tid, "name": name,
             "est_usd": float(round(est, 4)), "calls": calls}
            for tid, name, est, calls in top.by_spend
        ],
        "by_unpriced": [
            {"tenant_id": tid, "name": name, "tokens": tok}
            for tid, name, tok in top.by_unpriced
        ],
    }


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
                # R2 (high+medium MAJOR): priced ТОЛЬКО при численном est —
                # иначе шаблон отформатирует None и даст 500.
                "priced": bool(r.get("priced")) and est is not None,
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
            "slow": _int(b.get("slow")),
        }
    norm["errors_recent"] = [
        {"at": _str(e.get("at")), "status": _str(e.get("status")),
         "error_code": _str(e.get("error_code")), "task_type": _str(e.get("task_type")),
         "model": _str(e.get("model")), "tenant_id": _str(e.get("tenant_id")),
         "feature_key": _str(e.get("feature_key"))}
        for e in (payload.get("errors_recent") or []) if isinstance(e, dict)
    ]
    norm["slow_recent"] = [
        {"at": _str(s.get("at")), "latency_ms": _int(s.get("latency_ms")),
         "task_type": _str(s.get("task_type")), "model": _str(s.get("model")),
         "tenant_id": _str(s.get("tenant_id")), "feature_key": _str(s.get("feature_key"))}
        for s in (payload.get("slow_recent") or []) if isinstance(s, dict)
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
            "turns_total", "runs_failed", "inbound_stuck",
            "outbox_failed", "breakdowns_shown", "failures_total")}
    tt = payload.get("top_tenants")
    if isinstance(tt, dict):
        norm["top_tenants"] = {
            "by_spend": [
                {"tenant_id": _str(t.get("tenant_id")), "name": _str(t.get("name")),
                 "est_usd": _num(t.get("est_usd")), "calls": _int(t.get("calls"))}
                for t in (tt.get("by_spend") or []) if isinstance(t, dict)
            ],
            "by_unpriced": [
                {"tenant_id": _str(t.get("tenant_id")), "name": _str(t.get("name")),
                 "tokens": _int(t.get("tokens"))}
                for t in (tt.get("by_unpriced") or []) if isinstance(t, dict)
            ],
        }
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
