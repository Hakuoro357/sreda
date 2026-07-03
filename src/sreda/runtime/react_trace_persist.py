"""#192 Фаза B — персист durable-трейса хода ReAct (`react_turn_trace`).

Три точки жизненного цикла (вызываются из `react_loop.handle_turn`):
- `persist_trace_start` — свежий ход, ДО графа: INSERT `in_progress` (DO-NOTHING при гонке/replay).
- `persist_trace_pause` — confirm-пауза: UPDATE → `awaiting_confirm`+`pending`, ТОЛЬКО из `in_progress`.
- `persist_trace_finish` — финал/resume/handled-error: UPSERT → `done` + структура, ТОЛЬКО из
  `in_progress`/`awaiting_confirm` (терминал НЕИЗМЕНЕН — replay уже-`done` не перезатирает).

Дедуп/идемпотентность: expression-unique `uq_react_turn_trace_scope (tenant, coalesce(user_id,''),
turn_key)` — backstop; персист использует try-INSERT + conditional-UPDATE (портативно sqlite/PG, без
dialect-специфичного ON CONFLICT). ВСЁ guarded: сбой записи НЕ роняет ход (трейс = отладка, best-effort).
Аргументы инструментов — только HMAC (`trace_hash`); сырьё в таблицу НЕ пишется.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("sreda.react_trace")


def trace_enabled() -> bool:
    from sreda.config.settings import get_settings
    return bool(getattr(get_settings(), "react_trace_enabled", False))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session():
    from sreda.db.session import get_session_factory
    return get_session_factory()()


def persist_trace_start(*, tenant_id: str, user_id: str | None, thread_id: str, channel: str,
                        turn_key: str, origin_user_text: str) -> None:
    """Свежий ход: INSERT строки `in_progress`. DO-NOTHING при существующей (replay/гонка) — НЕ трогаем
    origin/статус. Guarded."""
    if not trace_enabled():
        return
    try:
        from sqlalchemy.exc import IntegrityError

        from sreda.db.models import ReactTurnTrace
        from uuid import uuid4
        sess = _session()
        try:
            sess.add(ReactTurnTrace(
                id=f"rtt_{uuid4().hex}", tenant_id=tenant_id, user_id=user_id,
                thread_id=thread_id, channel=channel, turn_key=turn_key,
                status="in_progress", origin_user_text=origin_user_text or "",
                created_at=_now()))
            sess.commit()
        except IntegrityError:
            sess.rollback()  # строка уже есть (replay/гонка) — DO NOTHING
        finally:
            sess.close()
    except Exception as exc:  # noqa: BLE001 — трейс не валит ход
        logger.warning("react_trace: start failed type=%s", type(exc).__name__)


def persist_trace_pause(*, tenant_id: str, user_id: str | None, turn_key: str) -> None:
    """Confirm-пауза: `in_progress` → `awaiting_confirm`+`pending`. CONDITIONAL (не переоткрывает
    `done`/`awaiting_confirm` при replay). Guarded."""
    if not trace_enabled():
        return
    try:
        from sqlalchemy import func, update

        from sreda.db.models import ReactTurnTrace
        sess = _session()
        try:
            sess.execute(
                update(ReactTurnTrace)
                .where(ReactTurnTrace.turn_key == turn_key,
                       ReactTurnTrace.tenant_id == tenant_id,
                       # nullable-safe user-скоуп (как в uq_react_turn_trace_scope) — не задеть
                       # чужую строку при коллизии turn_key внутри тенанта (R1 MAJOR Codex)
                       func.coalesce(ReactTurnTrace.user_id, "") == (user_id or ""),
                       ReactTurnTrace.status == "in_progress")
                .values(status="awaiting_confirm", confirm_state="pending"))
            sess.commit()
        finally:
            sess.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("react_trace: pause failed type=%s", type(exc).__name__)


def persist_trace_finish(*, tenant_id: str, user_id: str | None, thread_id: str, channel: str,
                         turn_key: str, reply_text: str, llm_calls: list[dict] | None,
                         tool_calls: list[dict] | None, confirm_state: str, outcome: str,
                         passes: int, routing_decision_json: str | None = None,
                         turn_policy_json: str | None = None,
                         confirm_resolution: str | None = None) -> None:
    """Финал/resume/handled-error: → `done` + структура. CONDITIONAL UPDATE из
    `in_progress`/`awaiting_confirm` (терминал НЕИЗМЕНЕН). Если строки нет (start потерян) — INSERT
    сразу `done` (finish-only recovery). НЕ перезаписывает origin/created_at. Guarded.

    routing_decision_json (#221 Ф3b): сериализованное решение доменного роутера (БЕЗ ПД). None в
    disabled-режиме → колонка NULL (никаких новых данных при выключенном роутере).
    turn_policy_json / confirm_resolution (#285 Фаза A): снапшот TurnPolicy (shadow) и исход
    confirm-паузы "yes"|"no"; None → NULL (флаг OFF / паузы не было)."""
    if not trace_enabled():
        return
    try:
        from sqlalchemy import func, update
        from sqlalchemy.exc import IntegrityError

        from sreda.db.models import ReactTurnTrace
        from uuid import uuid4
        vals = dict(
            status="done", reply_text=reply_text or "", confirm_state=confirm_state or "none",
            outcome=outcome or "ok", passes=int(passes or 0), finished_at=_now(),
            llm_calls_json=json.dumps(llm_calls or [], ensure_ascii=False),
            tool_calls_json=json.dumps(tool_calls or [], ensure_ascii=False),
            routing_decision_json=routing_decision_json,
            turn_policy_json=turn_policy_json,
            confirm_resolution=confirm_resolution)
        sess = _session()
        try:
            res = sess.execute(
                update(ReactTurnTrace)
                .where(ReactTurnTrace.turn_key == turn_key,
                       ReactTurnTrace.tenant_id == tenant_id,
                       # nullable-safe user-скоуп (R1 MAJOR Codex) — как в uq_react_turn_trace_scope
                       func.coalesce(ReactTurnTrace.user_id, "") == (user_id or ""),
                       ReactTurnTrace.status.in_(("in_progress", "awaiting_confirm")))
                .values(**vals))
            if (res.rowcount or 0) == 0:
                # строки нет (start потерян) ИЛИ уже done (replay) → INSERT done; если конфликт
                # (уже done) — IntegrityError → DO NOTHING (терминал неизменен).
                sess.add(ReactTurnTrace(
                    id=f"rtt_{uuid4().hex}", tenant_id=tenant_id, user_id=user_id,
                    thread_id=thread_id, channel=channel, turn_key=turn_key,
                    created_at=_now(), **vals))
            sess.commit()
        except IntegrityError:
            sess.rollback()  # уже done (replay-гонка) — не трогаем терминал
        finally:
            sess.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("react_trace: finish failed type=%s", type(exc).__name__)


def collect_tool_calls(messages: list, *, tenant_id: str) -> list[dict]:
    """Собрать tool_calls ИЗ ИСТОРИИ (идемпотентно к перевыполнению run_tools на resume): args из
    `AIMessage.tool_calls`, исход — из соответствующего `ToolMessage` (по tool_call_id) + его
    `artifact` ({latency_ms, error_type, result_kind}). args_hash = HMAC (сырьё не хранится).
    merge-by-id: повтор того же tool_call_id (re-exec) НЕ двоит."""
    from langchain_core.messages import AIMessage, ToolMessage

    from sreda.services.trace_hash import args_hmac

    # 1) собрать tool-результаты по id (last-wins → идемпотентно к re-exec)
    results: dict[str, dict] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            art = getattr(m, "artifact", None) or {}
            results[str(getattr(m, "tool_call_id", ""))] = {
                "status": getattr(m, "status", None),
                "result_kind": (art.get("result_kind") if isinstance(art, dict) else None),
                "error_type": (art.get("error_type") if isinstance(art, dict) else None),
                "latency_ms": (art.get("latency_ms") if isinstance(art, dict) else None),
            }
    # 2) пройтись по вызовам (из AIMessage.tool_calls), сшить с результатом, посчитать HMAC
    out: dict[str, dict] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in (getattr(m, "tool_calls", None) or []):
                cid = str(tc.get("id") or "")
                name = tc.get("name") or ""
                r = results.get(cid, {})
                rk = r.get("result_kind") or ("error" if r.get("status") == "error" else "ok")
                out[cid or f"{name}:{len(out)}"] = {
                    "name": name,
                    "args_hash": args_hmac(tenant_id=tenant_id, tool_name=name,
                                           args=tc.get("args") or {}),
                    "ok": (rk == "ok"),
                    "result_kind": rk,
                    # #285 Фаза A: результат НАБЛЮДЁН (ToolMessage найден) или rk — дефолт «ok»
                    # (resume-обрыв/деградация). Честный executed-счёт: ok AND observed
                    # (rk-ok best-effort дыра — CodexH R1 фазового ревью Фазы 0).
                    "observed": cid in results,
                    "error_type": r.get("error_type"),
                    "latency_ms": r.get("latency_ms"),
                }
    return list(out.values())
