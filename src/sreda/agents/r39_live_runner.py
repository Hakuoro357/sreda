"""R-39 Slice 4: live runner — главная точка R-39 в handlers.

`_r39_try_live` принимает session + action + tools list + локалы из
`execute_conversation_chat` и возвращает `LiveResult`:

- ``proceeded=True`` → R-39 ответил пользователю, caller возвращает
  ``[live.reply]`` и НЕ идёт в legacy.
- ``proceeded=False`` → fallback в legacy безопасен (никакие side
  effects не пошли — `side_effect_started=False`).

Safety contract (Codex R6 CRIT + R7 wider try):
- exception ДО `side_effect_started` → proceeded=False (legacy ok)
- exception ПОСЛЕ `side_effect_started` → proceeded=True с degraded
  apology + P0 admin alert (legacy НЕ запускаем — дубль side effect)
- chitchat / empty final_text → proceeded=False

Helpers:
- `_resolve_correction_target_with_db_fallback`: journal hit → use it;
  miss → query FamilyReminder.status="pending" последние 24h.
- `_load_correction_pending`: read prior R39RunJournal row.
- `_make_planner_invoker` / `_make_composer_invoker`: LLM call с
  invoke_with_per_call_timeout, budget logging через
  `BudgetService.record_llm_usage` (real model name из ai_msg.response_metadata).
- `_r39_admin_alert_adapter`: callable `(text) -> None` → send_admin_alert
  с severity="P1" и dedup hash.
- `_r39_result_data_extractor`: для process_turn — кладёт trigger_human
  через format_trigger_human + sanitize.
- `_persist_r39_journal_row`: INSERT в r39_run_journal внутри session
  (commit будет от graph.py).

Все pure-DI: LLM client, session, alert, detector передаются через
parameters. На каждом вызове создаётся свежий llm — `lru_cache` обходит
runtime_config provider switcher (Codex R4 MAJ).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreda.agents.contracts import (
    ConversationTurn,
    ResultKind,
    ToolJournalEntry,
    TurnContext,
)
from sreda.agents.journal import ToolJournal
from sreda.agents.correction_resolver import (
    AmbiguousCorrection,
    NoCorrectionTarget,
    ResolvedCorrection,
    resolve_correction_target,
)
from sreda.agents.r39_pipeline import PipelineResult, process_turn
from sreda.agents.r39_tool_callables import build_r39_tool_callables
from sreda.services.date_formatter import format_trigger_human


logger = logging.getLogger(__name__)


# ─── LiveResult ──────────────────────────────────────────────────────


@dataclass
class LiveResult:
    """Итог `_r39_try_live` для handler.

    proceeded=True ⇒ caller возвращает [reply] и НЕ идёт в legacy.
    proceeded=False ⇒ fallback в legacy безопасен (нет side effects).
    """

    proceeded: bool
    reply: Any = None  # RuntimeReply | None — Any чтобы избежать импорта handler
    side_effects_count: int = 0
    journal: ToolJournal | None = None


# ─── Главная функция ──────────────────────────────────────────────────


def r39_try_live(
    *,
    session: Session,
    tenant_id: str,
    user_id: str | None,
    user_text: str,
    feature_key: str,
    run_id: str,
    user_tz: str,
    tools_list: list[Any],
    runtime_reply_cls: Any,  # RuntimeReply class из handlers (DI)
    send_admin_alert_fn: Callable[..., None],
) -> LiveResult:
    """Главный entry-point R-39 live mode. Вызывается из execute_conversation_chat.

    Contract:
    - Catches любую exception
    - proceeded=False если pre-side-effect crash (legacy ok)
    - proceeded=True с apology + P0 alert если post-side-effect crash

    Args:
        session: SQLAlchemy session — переиспользует graph's session
            (single commit от graph включает наши r39_run_journal INSERT).
        tenant_id: str (Tenant.id из ActionEnvelope).
        user_id: str | None.
        user_text: текст пользователя.
        feature_key: должен быть "housewife_assistant" (caller проверяет).
        run_id: AgentRun.id из context["_run_id"].
        user_tz: IANA timezone из profile или "Europe/Moscow".
        tools_list: build_housewife_tools(...) результат — уже построенный.
        runtime_reply_cls: класс RuntimeReply (DI для избежания cyclic import).
        send_admin_alert_fn: реальная функция send_admin_alert.
    """
    side_effects_state: dict[str, Any] = {"started": False, "count": 0}

    # Pre-flight (без side effects)
    try:
        tool_callables = build_r39_tool_callables(tools_list, side_effects_state)
        r39_history = _load_r39_thread_history(session, run_id, lookback=5)
        correction_target = _resolve_correction_target_with_db_fallback(
            user_text, r39_history,
            session=session, tenant_id=tenant_id, user_id=user_id,
        )
        planner_llm = _make_planner_invoker(
            feature_key=feature_key, tenant_id=tenant_id,
            session=session, run_id=run_id,
        )
        composer_llm = _make_composer_invoker(
            feature_key=feature_key, tenant_id=tenant_id,
            session=session, run_id=run_id,
        )
        extractor = functools.partial(_r39_result_data_extractor, user_tz=user_tz)
        alert_fn = _r39_admin_alert_adapter(
            tenant_id=tenant_id, run_id=run_id,
            send_admin_alert_fn=send_admin_alert_fn,
        )
        correction_pending = _load_correction_pending(session, run_id, tenant_id)

        from sreda.services.llm import detect_unbacked_claim
    except Exception:
        logger.exception(
            "R-39 live preflight failed tenant=%s run=%s — fallback to legacy",
            tenant_id, run_id,
        )
        return LiveResult(proceeded=False)

    # Wider try/except (R7-1): process_turn + persist + reply assembly
    start = time.monotonic()
    try:
        result = process_turn(
            user_text=user_text,
            conversation_history=r39_history,
            turn_context=TurnContext(
                turn_id=run_id, tenant_id=tenant_id, user_tz=user_tz,
            ),
            now_utc=datetime.now(timezone.utc),
            tool_callables=tool_callables,
            detector=detect_unbacked_claim,
            invoke_planner_llm=planner_llm,
            invoke_composer_llm=composer_llm,
            result_data_extractor=extractor,
            admin_alert_fn=alert_fn,
            correction_pending=correction_pending,
            user_tz=user_tz,
            correction_target_override=correction_target,
        )

        # Slow-turn alert (без kill — Qwen R5 MAJ trade-off)
        elapsed = time.monotonic() - start
        if elapsed > 30.0:
            try:
                send_admin_alert_fn(
                    severity="P2",
                    title="R-39 slow turn",
                    body=f"tenant={tenant_id} run={run_id} elapsed={elapsed:.1f}s",
                    dedupe_key=f"r39_slow:{tenant_id}:{int(elapsed) // 5}",
                )
            except Exception:
                logger.exception("R-39 slow-turn alert failed")

        # Chitchat → fallback в legacy (legacy лучше делает small-talk)
        if result.plan_kind == "no_action":
            return LiveResult(proceeded=False)

        # Empty-reply rescue
        final_text = (result.final_text or "").strip()
        if not final_text:
            logger.warning("R-39 live: empty final_text run=%s", run_id)
            return LiveResult(proceeded=False)

        # Persist r39_run_journal row (atomic с graph commit)
        try:
            _persist_r39_journal_row(
                session=session, run_id=run_id, tenant_id=tenant_id,
                mode="live", result=result,
            )
        except Exception:
            logger.exception(
                "R-39 persist row failed tenant=%s run=%s — продолжаем",
                tenant_id, run_id,
            )
            # Persist failure не критична — ответ уже сформирован,
            # journal будет восстановлен через DB reconcile при следующем
            # correction (option B trade-off).

        return LiveResult(
            proceeded=True,
            reply=runtime_reply_cls(
                text=final_text, reply_markup=None, feature_key=feature_key,
            ),
            side_effects_count=side_effects_state["count"],
            journal=result.journal,
        )

    except Exception as exc:
        logger.exception(
            "R-39 live failed tenant=%s run=%s started=%s",
            tenant_id, run_id, side_effects_state["started"],
        )
        if side_effects_state["started"]:
            # ANY mutating call попытался — НИКОГДА не fallback в legacy
            # Code-review MINOR (Qwen): не пишем str(exc) в alert body —
            # может содержать user_text fragments из tool parse failures
            # (152-ФЗ). Тип exception + run_id достаточно для расследования
            # в server logs.
            try:
                send_admin_alert_fn(
                    severity="P0",
                    title="R-39 crashed AFTER side_effect_started",
                    body=(
                        f"tenant={tenant_id} run={run_id} "
                        f"effects={side_effects_state['count']} "
                        f"exc={type(exc).__name__} (see server logs)"
                    ),
                    dedupe_key=f"r39_post_se:{tenant_id}",
                )
            except Exception:
                logger.exception("R-39 post-SE alert failed")
            # Code-review MINOR (Qwen): degraded apology path должен сам
            # быть try-safe. Если runtime_reply_cls() raises — outer except
            # в handlers может попасть в legacy fallback → дубль side effect.
            # Construct reply под try; на failure возвращаем reply=None
            # (handler check `if _live is not None and _live.proceeded` →
            # proceeded=True но reply=None → handler возвращает [None],
            # которое отфильтровывается / либо мы возвращаем хардкод-fallback).
            try:
                degraded_reply = runtime_reply_cls(
                    text="Часть действий могла пройти. Скажи ещё раз — допроверю.",
                    reply_markup=None,
                    feature_key=feature_key,
                )
            except Exception:
                logger.exception("R-39 degraded reply construction failed")
                # Минимальный fallback: даже если cls сломан, мы НЕ должны
                # вернуть proceeded=False (это пустит legacy → дубль).
                # Возвращаем proceeded=True с reply=None — caller вернёт
                # пустой список replies, лучше тишина чем дубль.
                return LiveResult(
                    proceeded=True,
                    reply=None,
                    side_effects_count=side_effects_state["count"],
                )
            return LiveResult(
                proceeded=True,
                reply=degraded_reply,
                side_effects_count=side_effects_state["count"],
            )
        # Pre-side-effect crash — legacy fallback safe
        return LiveResult(proceeded=False)


# ─── History loader ──────────────────────────────────────────────────


def _load_r39_thread_history(
    session: Session, run_id: str, lookback: int = 5,
) -> tuple[ConversationTurn, ...]:
    """Загрузить prior R-39 turns того же thread'а (mode='live')."""
    from sreda.db.models import AgentRun, R39RunJournal

    current = session.get(AgentRun, run_id)
    if current is None:
        return ()
    rows = session.execute(
        select(AgentRun, R39RunJournal)
        .join(R39RunJournal, AgentRun.id == R39RunJournal.run_id)
        .where(
            AgentRun.thread_id == current.thread_id,
            AgentRun.status == "completed",
            AgentRun.id != run_id,
            R39RunJournal.mode == "live",
        )
        .order_by(AgentRun.created_at.desc())
        .limit(lookback)
    ).all()

    turns: list[ConversationTurn] = []
    for run, journal_row in reversed(rows):  # хронологически
        try:
            input_data = json.loads(run.input_json or "{}")
            user_text = input_data.get("params", {}).get("text", "")
            entries_raw = json.loads(journal_row.journal_json or "[]")
            entries = tuple(_deserialize_journal_entry(e) for e in entries_raw)
            turns.append(ConversationTurn(
                user_text=user_text,
                journal_entries=entries,
                turn_id=run.id,
                timestamp_utc=run.created_at.isoformat() if run.created_at else "",
            ))
        except Exception:
            logger.exception("R-39 history: malformed row run=%s", run.id)
            continue
    return tuple(turns)


# ─── Correction resolver с DB fallback ───────────────────────────────


_CORRECTION_MARKER_RE = re.compile(
    r"\b(?:"
    r"нет[,.]\s"
    r"|не\s+(?:на|в)\s+\d"
    r"|не\s+правильн"
    r"|неправильн"
    r"|не\s+так\b"
    r"|не\s+туда\b"
    r"|ошибк"
    r"|переигра"
    r"|поменяй\s+на"
    r")",
    re.IGNORECASE,
)


def _resolve_correction_target_with_db_fallback(
    user_text: str,
    r39_history: tuple[ConversationTurn, ...],
    *,
    session: Session,
    tenant_id: str,
    user_id: str | None,
) -> Any:
    """R-39 R6: option B — journal hit + FamilyReminder fallback.

    Tools commit'ят сами, R39RunJournal row может не успеть записаться
    при crash. correction_resolver жёстко требует journal — без fallback'а
    мы упустим target.

    Code-review CRIT (Codex): DB fallback должен срабатывать ТОЛЬКО при
    явном correction signal в user_text. Иначе обычная mutation «поставь
    напоминание X» матчит pending FamilyReminder → planner update'ит чужое
    напоминание вместо создания нового.

    Code-review MAJ (Qwen): user_id=None приводит к `IS NULL` query —
    matches чужие reminders с user_id NULL → cross-user leak. Early return
    если user_id отсутствует.
    """
    journal_result = resolve_correction_target(user_text, list(r39_history))
    if not isinstance(journal_result, NoCorrectionTarget):
        logger.info(
            "R-39 correction: journal_hit tenant=%s user_text_snip=%r → %s",
            tenant_id, (user_text or "")[:80], type(journal_result).__name__,
        )
        return journal_result

    # Codex CRIT: DB fallback только при explicit correction marker
    marker_match = _CORRECTION_MARKER_RE.search(user_text or "")
    if not marker_match:
        logger.info(
            "R-39 correction: no_marker tenant=%s user_text_snip=%r → NoCorrectionTarget",
            tenant_id, (user_text or "")[:80],
        )
        return journal_result  # NoCorrectionTarget от resolver'а

    # Qwen MAJ: без user_id query может задеть чужие reminders
    if user_id is None or user_id == "":
        logger.info(
            "R-39 correction: marker_matched but user_id missing tenant=%s "
            "marker=%r → NoCorrectionTarget(db_fallback_no_user_id)",
            tenant_id, marker_match.group(0),
        )
        return NoCorrectionTarget(reason="db_fallback_no_user_id")

    # DB fallback (scoped strictly to current user)
    from sreda.db.models.housewife import FamilyReminder

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = session.execute(
        select(FamilyReminder.id, FamilyReminder.title, FamilyReminder.created_at)
        .where(
            FamilyReminder.tenant_id == tenant_id,
            FamilyReminder.user_id == user_id,
            FamilyReminder.status == "pending",
            FamilyReminder.created_at > cutoff,
        )
        .order_by(FamilyReminder.created_at.desc())
        .limit(5)
    ).all()
    if not rows:
        logger.info(
            "R-39 correction: db_fallback no pending reminders for tenant=%s user=%s "
            "marker=%r → NoCorrectionTarget(db_fallback_no_pending_reminders)",
            tenant_id, user_id, marker_match.group(0),
        )
        return NoCorrectionTarget(reason="db_fallback_no_pending_reminders")
    if len(rows) == 1:
        logger.info(
            "R-39 correction: db_fallback hit tenant=%s user=%s marker=%r "
            "→ ResolvedCorrection(target=%s title=%r)",
            tenant_id, user_id, marker_match.group(0),
            rows[0][0], rows[0][1],
        )
        return ResolvedCorrection(
            target_entity_id=rows[0][0],
            target_title=rows[0][1],
            target_tool="schedule_reminder",
            source_turn_id="db_fallback",
        )
    logger.info(
        "R-39 correction: db_fallback ambiguous tenant=%s user=%s marker=%r "
        "→ AmbiguousCorrection(n=%d)",
        tenant_id, user_id, marker_match.group(0), len(rows),
    )
    return AmbiguousCorrection(
        candidates=tuple(
            ResolvedCorrection(
                target_entity_id=r[0], target_title=r[1],
                target_tool="schedule_reminder", source_turn_id="db_fallback",
            )
            for r in rows
        ),
        reason=f"db_fallback_multiple_pending ({len(rows)})",
    )


# ─── correction_pending state ────────────────────────────────────────


def _load_correction_pending(
    session: Session, run_id: str, tenant_id: str,
) -> str | None:
    """Read correction_pending из last R-39 live journal row same thread."""
    from sreda.db.models import AgentRun, R39RunJournal

    current = session.get(AgentRun, run_id)
    if current is None:
        return None
    row = session.execute(
        select(R39RunJournal)
        .join(AgentRun, AgentRun.id == R39RunJournal.run_id)
        .where(
            AgentRun.thread_id == current.thread_id,
            AgentRun.id != run_id,
            R39RunJournal.mode == "live",
            R39RunJournal.correction_pending.isnot(None),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.correction_pending if row else None


# ─── Admin alert adapter ─────────────────────────────────────────────


def _r39_admin_alert_adapter(
    *, tenant_id: str, run_id: str, send_admin_alert_fn: Callable[..., None],
) -> Callable[[str], None]:
    """Адаптер `(text: str) -> None` поверх send_admin_alert.

    Используется composer'ом / audit'ом для L4 алертов.
    Severity=P1, dedup по hash(text) per (tenant, run).
    """
    def alert(text: str) -> None:
        # Code-review MINOR (Codex): stable sha256 dedupe instead of
        # process-randomized hash() — иначе при restart'ах dedupe rate
        # сбрасывается и dedupe эффективность падает.
        digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]
        try:
            send_admin_alert_fn(
                severity="P1",
                title="R-39 audit / lock",
                body=text[:3900],
                dedupe_key=f"r39:{run_id}:{digest}",
                extra_context={"tenant": tenant_id, "run_id": run_id},
            )
        except Exception:
            logger.exception("R-39 admin_alert_adapter failed")

    return alert


# ─── result_data_extractor ───────────────────────────────────────────


def _r39_result_data_extractor(
    tool_name: str,
    args: dict[str, Any],
    raw_result: Any,
    *,
    user_tz: str,
) -> dict[str, Any]:
    """Подготовить result_data для подстановки в шаблон first_line_renderer.

    args + raw_result (от parse_ok_segment) → итоговый dict.
    Дополнительно: format_trigger_human(trigger_iso, user_tz, now_user) →
    trigger_human для шаблонов.

    Wrapper через functools.partial(user_tz=...) делает signature
    совместимой с executor's ResultDataExtractor = (str, dict, Any) -> dict.
    """
    out: dict[str, Any] = {}
    out.update(args)
    if isinstance(raw_result, dict):
        out.update(raw_result)

    iso = out.get("trigger_iso")
    if iso:
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            now_user = datetime.now(ZoneInfo(user_tz))
            out["trigger_human"] = format_trigger_human(dt, user_tz, now_user)
        except (ValueError, TypeError, ImportError):
            out["trigger_human"] = "(время не разобрано)"

    return out


# ─── LLM invokers ────────────────────────────────────────────────────


_PLANNER_TEMP = 0.2
_COMPOSER_TEMP = 0.7
_PLANNER_TIMEOUT = 12.0
_COMPOSER_TIMEOUT = 8.0

# 2026-05-19: переключили R-39 shadow primary на gemini-2.5-flash
# после R-39 planner bench (plans/r39-planner-bench-2026-05-19.md):
# - gemini-2.5-flash: 100% schema, 92% kind, p50=912ms / p95=1943ms
# - mimo-v2.5-pro: 92% schema, p50=6s, p95=30s timeout
#
# Это shadow-only: hallucinated tool names (Bug C) пишутся в журнал
# как failed runs (dry-run callables), пользователь не видит. Это
# желаемая data collection поведение — увидим в production data
# насколько 2.5-flash hallucinates по сравнению с lab bench (2 на 12).
#
# План R-39 говорил 3.1-flash-lite, но bench показал 2.5-flash быстрее
# (p95 1.9s vs 7.3s) при том же tool accuracy. Возврат к 3.1 — если
# 2.5 в проде окажется хуже.
#
# Fallback chain пуст: при error → graceful None (в shadow ок).
# Перед live promotion нужен tool whitelist enforcement (Bug C) +
# timeout chain fix.
# P0.C (2026-05-19): per-provider candidates with per-provider timeouts.
# Замена RunnableWithFallbacks (где outer timeout оборачивал весь chain).
# Структура: list[(provider_name, timeout_seconds)]. Iterate в порядке,
# при exception/timeout/parse-fail → try next. Каждый получает свой full
# budget. cascade fix 2026-05-19 (Codex MAJOR): теперь invoker делает
# `if parsed is None: continue` чтобы реально trigger fallback на bad
# JSON, а не возвращать первое успешное HTTP с broken parse.
#
# 2026-05-19 bench v4 (25 scenarios × 4 models, hallucinated_time metric):
#   gemini-2.5-flash: 84% ok, 0 halluc, p50=939ms, p95=3.4s
#   qwen-plus:        84% ok, 0 halluc, p50=1.5s, p95=2.2s
#   mimo-v2.5-pro:    80% ok, 0 halluc, p50=8.5s, p95=30s timeout
#
# Решение по trio review consensus (Codex + Xiaomi + OpenCode):
# - gemini-2.5-flash primary (быстрее на p50)
# - qwen-plus co-primary (более consistent на p95)
# - mimo УБРАНА из cascade (Xiaomi MAJOR #3, OpenCode MAJOR #2:
#   dead weight на 6s timeout). Legacy mimo продолжает работать через
#   admin switcher — это отдельный path.
#
# Composer cascade — UNCHANGED в этом patch (Xiaomi MAJOR #5 + OpenCode
# M1: no composer bench data). Composer остаётся mimo-only 8s — рерайт
# отдельным patch'ом после composer-specific bench (warmth/persona
# quality, не tool accuracy).
#
# Worst-case wall-clock:
#   Planner:  4+4 = 8s  (cascade gemini → qwen)
#   Composer: 8s        (single mimo candidate)
# 2026-05-19 12:21 — second pilot retest: qwen-plus тоже провалился
# на real voice turns (5/5 Clarification). Часть turn'ов даже не
# дошла до LLM — parser_ambiguous short-circuit на «два часа» (word
# numerals + ambiguous phrasing). Откатываемся на mimo-only как
# stable baseline пока не решён root cause (см. memory backlog).
_PLANNER_CANDIDATES: tuple[tuple[str, float], ...] = (
    ("mimo-v2.5", 12.0),
)
# Composer cascade — UNCHANGED in этом patch (OpenCode MAJOR M1 fix):
# нет composer bench data, нет основания менять. Mimo only / 8s timeout
# как и было. Composer cascade rework — отдельный patch после
# composer-specific bench (warmth/persona quality, не tool accuracy).
_COMPOSER_CANDIDATES: tuple[tuple[str, float], ...] = (
    ("mimo-v2.5", 8.0),
)

# Backward-compat exports (для тестов и diagnostics)
_R39_PRIMARY_PROVIDER = _PLANNER_CANDIDATES[0][0]
_R39_FALLBACK_PROVIDERS: tuple[str, ...] = tuple(c[0] for c in _PLANNER_CANDIDATES[1:])


def _make_planner_invoker(
    *, feature_key: str, tenant_id: str, session: Session, run_id: str,
) -> Callable[[str, str], dict[str, Any] | None]:
    """Planner invoker с per-provider timeout cascade.

    BugFix code-review CRITICAL: ранее использовали RunnableWithFallbacks
    + outer timeout 12s. Если primary висит 10s — fallback'и получают
    2s/0s. Новый подход: явный for-loop по списку (provider, timeout),
    каждый получает свой full budget.
    """

    def invoke(system: str, user: str) -> dict[str, Any] | None:
        from sreda.services.llm import (
            LLMCallTimeout, get_chat_llm, invoke_with_per_call_timeout,
        )
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        last_error: str | None = None
        for provider, tmo in _PLANNER_CANDIDATES:
            llm = get_chat_llm(provider=provider, temperature=_PLANNER_TEMP)
            if llm is None:
                logger.warning(
                    "R-39 planner: %s not configured — try next", provider,
                )
                continue
            try:
                ai_msg = invoke_with_per_call_timeout(
                    llm, messages, timeout_seconds=tmo,
                )
            except LLMCallTimeout:
                logger.warning(
                    "R-39 planner: %s timeout (%.1fs) — try next",
                    provider, tmo,
                )
                last_error = f"{provider}:timeout"
                continue
            except Exception as exc:
                logger.exception(
                    "R-39 planner: %s failed (%s) — try next",
                    provider, type(exc).__name__,
                )
                last_error = f"{provider}:{type(exc).__name__}"
                continue

            # Got HTTP success — проверить parse JSON удался.
            #
            # 2026-05-19 cascade fix: без этого check'a invoker возвращал
            # первый "успешный" HTTP даже если parse_planner_json вернул
            # None (broken JSON output). Cascade фактически не работал
            # на malformed output. Теперь bad parse → try next.
            #
            # Intentional: unexpected `kind` value ({"kind":"weird"}) НЕ
            # триггерит cascade — `_parse_planner_json` permissive, любой
            # valid dict считается success. Caller (`_parse_llm_output`)
            # обрабатывает unknown_kind через NoAction(llm_unknown_kind:X).
            # Retry с другой моделью на schema-level mismatch вероятнее
            # вернёт то же и сожжёт 8s бюджета (Codex+OpenCode flagged).
            #
            # Usage tracking: вызываем _log_and_record_usage В ОБОИХ случаях
            # (success + bad_json) — HTTP succeeded, tokens consumed, budget
            # должен это учесть (Codex MAJOR fix).
            content = getattr(ai_msg, "content", "") or ""
            parsed = _parse_planner_json(content)
            _log_and_record_usage(
                ai_msg=ai_msg, session=session, tenant_id=tenant_id,
                feature_key=feature_key, run_id=run_id,
                task_type="r39_planner" if parsed is not None
                          else "r39_planner_bad_json",
            )
            if parsed is None:
                logger.warning(
                    "R-39 planner: %s returned unparsable JSON "
                    "(content_preview=%r) — try next",
                    provider, content[:120],
                )
                last_error = f"{provider}:bad_json"
                continue
            logger.info(
                "R-39 planner: %s succeeded in <%.1fs", provider, tmo,
            )
            return parsed

        logger.warning(
            "R-39 planner: all %d candidates failed (last=%s)",
            len(_PLANNER_CANDIDATES), last_error,
        )
        return None

    return invoke


def _make_composer_invoker(
    *, feature_key: str, tenant_id: str, session: Session, run_id: str,
) -> Callable[[str, str], str | None]:
    """Composer invoker с per-provider timeout cascade. Same pattern что
    planner, но без _parse_planner_json — composer возвращает plain str.
    """

    def invoke(system: str, user: str) -> str | None:
        from sreda.services.llm import (
            LLMCallTimeout, get_chat_llm, invoke_with_per_call_timeout,
        )
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        for provider, tmo in _COMPOSER_CANDIDATES:
            llm = get_chat_llm(provider=provider, temperature=_COMPOSER_TEMP)
            if llm is None:
                continue
            try:
                ai_msg = invoke_with_per_call_timeout(
                    llm, messages, timeout_seconds=tmo,
                )
            except LLMCallTimeout:
                continue
            except Exception:
                logger.exception(
                    "R-39 composer: %s failed — try next", provider,
                )
                continue

            _log_and_record_usage(
                ai_msg=ai_msg, session=session, tenant_id=tenant_id,
                feature_key=feature_key, run_id=run_id,
                task_type="r39_composer",
            )
            return getattr(ai_msg, "content", "") or None

        return None

    return invoke


def _log_and_record_usage(
    *,
    ai_msg: Any,
    session: Session,
    tenant_id: str,
    feature_key: str,
    run_id: str,
    task_type: str,
) -> None:
    """Общий хелпер: _log_llm_response + BudgetService.record_llm_usage.

    BudgetService.record_llm_usage hardcodes provider_key="mimo" —
    R-39 расходы будут misattribute'ны (R7 trade-off, post-pilot ticket).
    """
    try:
        usage = getattr(ai_msg, "usage_metadata", None) or {}
        prompt_tokens = int(usage.get("input_tokens", 0))
        completion_tokens = int(usage.get("output_tokens", 0))
        from sreda.runtime.handlers import _log_llm_response  # late import
        _log_llm_response(
            tenant_id=tenant_id,
            feature_key=feature_key,
            iteration=0,
            ai_msg=ai_msg,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        meta = getattr(ai_msg, "response_metadata", {}) or {}
        real_model = (
            meta.get("model_name") or meta.get("model") or "r39_unknown"
        )
        from sreda.services.budget import BudgetService
        BudgetService(session).record_llm_usage(
            tenant_id=tenant_id,
            feature_key=feature_key,
            model=str(real_model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            run_id=run_id,
            task_type=task_type,
        )
    except Exception:
        logger.exception(
            "R-39 budget log failed (non-fatal) task=%s", task_type,
        )


_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*\n?")
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")


def _parse_planner_json(content: str) -> dict[str, Any] | None:
    """Парсит planner LLM output. Strip markdown fences если есть."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = _FENCE_OPEN_RE.sub("", text)
        text = _FENCE_CLOSE_RE.sub("", text)
        text = text.strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        logger.warning("R-39 planner: bad JSON: %s", text[:120])
        return None


# ─── Persist row ─────────────────────────────────────────────────────


def _persist_r39_journal_row(
    *,
    session: Session,
    run_id: str,
    tenant_id: str,
    mode: str,
    result: PipelineResult,
) -> None:
    """INSERT в r39_run_journal. Commit будет от graph.py.

    Code-review MAJ (Codex): wrap в SAVEPOINT — если FK/duplicate/data
    error случится на flush, savepoint откатывается без порчи shared
    transaction'а graph'а. Persist failure non-critical (DB fallback
    reconcile через FamilyReminder).
    """
    from sreda.db.models import R39RunJournal

    success_count = sum(
        1 for e in result.journal.entries
        if e.result_kind is ResultKind.SUCCESS
    )
    row = R39RunJournal(
        run_id=run_id,
        tenant_id=tenant_id,
        mode=mode,
        plan_kind=result.plan_kind,
        journal_json=json.dumps(
            [_serialize_journal_entry(e) for e in result.journal.entries],
            ensure_ascii=False,
        ),
        correction_pending=result.correction_pending,
        audit_unbacked=result.audit.is_unbacked,
        side_effects_count=success_count,
    )
    # SAVEPOINT — locally rollback'нем при FK/dup error чтобы graph commit
    # не пострадал. session.begin_nested() это savepoint в SQLAlchemy.
    sp = session.begin_nested()
    try:
        session.add(row)
        session.flush()  # форсирует FK check ЗДЕСЬ, не на graph commit
        sp.commit()
    except Exception:
        sp.rollback()
        logger.exception(
            "R-39 persist row failed (savepoint rolled back) run=%s mode=%s",
            run_id, mode,
        )
        # Не пробрасываем — caller'у вернёт что persist не удался,
        # но live ответ уже отправлен, journal восстановится через
        # DB fallback при следующем correction.


# ─── Journal serialize / deserialize ─────────────────────────────────


def _serialize_journal_entry(e: ToolJournalEntry) -> dict[str, Any]:
    """ToolJournalEntry → JSON-safe dict."""
    return {
        "tool_name": e.tool_name,
        "action_index": e.action_index,
        "result_kind": e.result_kind.value,
        "result_data": {
            k: v for k, v in (e.result_data or {}).items()
            if isinstance(v, (str, int, float, bool, type(None)))
        },
        "entity_id": e.entity_id,
        "idempotency_key": e.idempotency_key,
        "error_code": e.error_code,
        "error_message": e.error_message,
    }


def _deserialize_journal_entry(d: dict[str, Any]) -> ToolJournalEntry:
    """JSON dict → ToolJournalEntry."""
    return ToolJournalEntry(
        tool_name=d["tool_name"],
        action_index=int(d.get("action_index", 0)),
        result_kind=ResultKind(d["result_kind"]),
        result_data=d.get("result_data") or {},
        entity_id=d.get("entity_id"),
        idempotency_key=d.get("idempotency_key"),
        error_code=d.get("error_code"),
        error_message=d.get("error_message"),
    )
