"""R-39 Slice 5: shadow worker — параллельный dry-run R-39 pipeline.

Цель: собирать данные «что R-39 решил бы сделать» на реальном трафике
БЕЗ риска для пользователя. Реальные tool calls **не** выполняются —
вместо этого fake-callables возвращают фиктивные dict как parsed
результат tools.

Запускается через ``kick_off_shadow_thread`` ПОСЛЕ возврата legacy reply
(в ``threading.Thread(daemon=True)``), своя SessionLocal через
``get_session_factory()`` — чтобы НЕ шарить session с graph'ом (Qwen R3
CRIT race condition).

Persist через ``r39_run_journal`` (mode='shadow'). Offline сравнение:
- shadow plan_kind == legacy fact (action ↔ tool call counts)
- shadow journal entries' tool_names vs legacy AIMessage.tool_calls

Trade-off (Codex R3): shadow тестирует только **planner accuracy** и
**adapter robustness**, НЕ tool integration. Реальные tools могут
вернуть error/skipped — в shadow они всегда ok. После стабилизации
shadow → live переключение покажет реальные ошибки tools (Slice 6).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from sreda.agents.contracts import TurnContext
from sreda.agents.r39_pipeline import process_turn
from sreda.agents.r39_tool_callables import REQUIRED_TOOLS


logger = logging.getLogger(__name__)


# ─── Главная функция ──────────────────────────────────────────────────


def kick_off_shadow_thread(
    *,
    user_text: str,
    tenant_id: str,
    user_id: str | None,
    run_id: str,
    user_tz: str,
    feature_key: str,
    session_factory: Callable[[], Any] | None = None,
    runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Запустить shadow worker в фоне. Не блокирует caller.

    Args:
        session_factory: ``sessionmaker`` для shadow session. По
            умолчанию ``get_session_factory()`` (lru_cached).
        runner: функция запуска worker'а. По умолчанию
            ``threading.Thread(daemon=True).start()``. В тестах можно
            передать lambda fn: fn() для синхронного execution.
    """
    def worker() -> None:
        # Code-review MAJ (Codex): wait for graph to commit AgentRun row
        # before shadow читает history (FK insert требует existing run_id).
        # 2 секунды — запас для graph commit; daemon thread не блокирует UX.
        time.sleep(2.0)
        try:
            sf = session_factory or _default_session_factory()
            if sf is None:
                logger.error("R-39 shadow: session_factory unavailable")
                return
            with sf() as shadow_session:
                _run_shadow_turn(
                    session=shadow_session,
                    user_text=user_text,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    user_tz=user_tz,
                    feature_key=feature_key,
                )
                shadow_session.commit()
        except Exception:
            logger.exception(
                "R-39 shadow worker failed run=%s — silently ignored", run_id,
            )

    if runner is not None:
        runner(worker)
    else:
        threading.Thread(
            target=worker, daemon=True, name=f"r39-shadow-{run_id}",
        ).start()


# ─── Worker body ──────────────────────────────────────────────────────


def _run_shadow_turn(
    *,
    session: Any,  # Session — параметризован чтобы избежать круговых импортов
    user_text: str,
    tenant_id: str,
    user_id: str | None,
    run_id: str,
    user_tz: str,
    feature_key: str,
) -> None:
    """Прогнать process_turn в shadow режиме и записать r39_run_journal row."""
    from sreda.agents.r39_live_runner import (
        _load_r39_thread_history,
        _make_planner_invoker,
        _persist_r39_journal_row,
        _r39_result_data_extractor,
        _resolve_correction_target_with_db_fallback,
    )

    # 1. History из той же thread'а (читаем r39_run_journal mode=live строки).
    history = _load_r39_thread_history(session, run_id, lookback=5)

    # 2. Dry-run tool callables — НЕ trigger реальных DB writes
    tool_callables = build_shadow_dry_run_callables()

    # 3. Planner — реальный LLM (но без budget logging — shadow off-budget)
    planner_llm = _make_planner_invoker(
        feature_key=feature_key, tenant_id=tenant_id,
        session=session, run_id=run_id,
    )

    # 4. Resolver — journal hit ИЛИ DB fallback
    correction_target = _resolve_correction_target_with_db_fallback(
        user_text, history,
        session=session, tenant_id=tenant_id, user_id=user_id,
    )

    # 5. Extractor с user_tz
    import functools as _ft
    extractor = _ft.partial(
        _r39_result_data_extractor, user_tz=user_tz,
    )

    # 6. process_turn без composer (composer не нужен в shadow — текст не отправляем)
    result = process_turn(
        user_text=user_text,
        conversation_history=history,
        turn_context=TurnContext(
            turn_id=run_id, tenant_id=tenant_id, user_tz=user_tz,
        ),
        now_utc=datetime.now(timezone.utc),
        tool_callables=tool_callables,
        detector=_noop_detector,
        invoke_planner_llm=planner_llm,
        invoke_composer_llm=None,
        result_data_extractor=extractor,
        admin_alert_fn=None,
        user_tz=user_tz,
        correction_target_override=correction_target,
    )

    # 7. Persist mode='shadow' — отдельная запись для offline сравнения
    _persist_r39_journal_row(
        session=session,
        run_id=run_id,
        tenant_id=tenant_id,
        mode="shadow",
        result=result,
    )


def _noop_detector(_text: str, _called_tools: set[str]) -> bool:
    """Shadow detector всегда возвращает False (не алертим shadow'у)."""
    return False


# ─── Shadow dry-run callables ────────────────────────────────────────


def build_shadow_dry_run_callables() -> dict[str, Callable[..., dict[str, Any]]]:
    """Mock tools без реальных side effects.

    Возвращают такой же формат dict как ``parse_ok_segment`` для
    соответствующего tool name — это позволяет process_turn / first_line
    рендерить full message без падений.
    """
    def fake_schedule(**kwargs: Any) -> dict[str, Any]:
        return {
            "entity_id": "shadow_rem",
            "trigger_iso": kwargs.get("trigger_iso", ""),
            "raw_ok": "ok:scheduled:shadow_rem:" + str(kwargs.get("trigger_iso", "")),
            "status_token": "scheduled",
        }

    def fake_update(**kwargs: Any) -> dict[str, Any]:
        rid = kwargs.get("reminder_id", "shadow_rem")
        return {
            "entity_id": rid,
            "trigger_iso": kwargs.get("trigger_iso", ""),
            "raw_ok": f"ok:updated:{rid}:" + str(kwargs.get("trigger_iso", "")),
            "status_token": "updated",
        }

    def fake_cancel(**_kwargs: Any) -> dict[str, Any]:
        return {"raw_ok": "ok:cancelled"}

    def fake_save_recipe(**_kwargs: Any) -> dict[str, Any]:
        return {
            "entity_id": "shadow_rec",
            "raw_ok": "ok:saved:shadow_rec",
            "status_token": "saved",
        }

    def fake_add_shopping_items(**kwargs: Any) -> dict[str, Any]:
        items = kwargs.get("items") or []
        count = len(items) if isinstance(items, list) else 0
        return {
            "items_added_count": count,
            "raw_ok": f"ok:added:{count}:ids=[]",
        }

    def fake_complete_task(**kwargs: Any) -> dict[str, Any]:
        tid = kwargs.get("task_id", "shadow_tsk")
        return {
            "entity_id": tid,
            "raw_ok": f"ok:completed:{tid}",
            "status_token": "completed",
        }

    factory: dict[str, Callable[..., dict[str, Any]]] = {
        "schedule_reminder": fake_schedule,
        "update_reminder": fake_update,
        "cancel_reminder": fake_cancel,
        "save_recipe": fake_save_recipe,
        "add_shopping_items": fake_add_shopping_items,
        "complete_task": fake_complete_task,
    }
    # Возвращаем только REQUIRED_TOOLS (на случай если REQUIRED_TOOLS
    # расширится — fail-fast по KeyError, легко найти)
    return {name: factory[name] for name in REQUIRED_TOOLS}


# ─── Default session factory (late import чтобы избежать circular) ───


def _default_session_factory() -> Callable[[], Any] | None:
    """Lazy import default session factory из sreda.db.session."""
    try:
        from sreda.db.session import get_session_factory
        return get_session_factory()
    except Exception:
        logger.exception("R-39 shadow: get_session_factory unavailable")
        return None
