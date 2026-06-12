"""#135 — память планов, срез 1: сбор эталонов + тень (план final, R4).

Жёсткие инварианты (чеклист в issue #135):
- тенанты вне гейта: ноль записей/trace/embeddings, промпт байт-в-байт;
- сырьё НЕ хранится — только typed-allowlist «форма» плана (плейсхолдеры
  вместо пользовательских аргументов), property-тест «нет PII»;
- сбор и тень — ЦЕЛИКОМ вне request path (отсоединённая задача после
  ответа, своя сессия, глобальный семафор=1, ограниченный повтор);
- рендер в промпт в этом срезе НЕ существует (срез 4 — по команде
  владельца).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sreda.plan_library")

# плейсхолдер ЛЮБОГО пользовательского литерала в форме
ARG_PLACEHOLDER = "<arg>"
_REF_RE = re.compile(r"^\$\{s\d+(\.[A-Za-z_][A-Za-z0-9_]*)*\}$")
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# локальный шов задач (#133-паттерн: тесты патчат именно его)
_create_task = asyncio.create_task

# фоновые задачи библиотеки: не съедаем event loop/пул БД (план r4)
_BG_SEMAPHORE = asyncio.Semaphore(1)
_BG_TIMEOUT_SEC = 20.0

CANDIDATE_TTL_DAYS = 90
APPROVED_TTL_DAYS = 180


def _redact_value(v: Any) -> Any:
    """Литерал → плейсхолдер; ссылки ${sN.field} — структурная связь,
    остаются (allowlist-паттерн); контейнеры — рекурсивно."""
    if isinstance(v, str):
        return v if _REF_RE.match(v) else ARG_PLACEHOLDER
    if isinstance(v, bool) or v is None:
        return v  # тип-сигнал, не PII
    if isinstance(v, (int, float)):
        return ARG_PLACEHOLDER
    if isinstance(v, list):
        return [_redact_value(x) for x in v[:5]]
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in sorted(v.items())
                if _IDENT_RE.match(str(k))}
    return ARG_PLACEHOLDER


def _compose_form(compose: Any) -> dict:
    kind = str(getattr(compose, "kind", None) or
               (compose or {}).get("kind", ""))[:16]
    out: dict[str, Any] = {"kind": kind}
    for attr in ("template_id", "llm_prompt_key"):
        val = (getattr(compose, attr, None)
               if not isinstance(compose, dict) else compose.get(attr))
        if val is not None and _IDENT_RE.match(str(val)):
            out[attr] = str(val)
    return out


def project_plan_form(plan: Any) -> dict:
    """Каноническая redacted-форма плана. Чистая функция; никакие
    пользовательские литералы не проходят (property-тест)."""
    actions = getattr(plan, "actions", None) or {}
    steps = []
    for sid in sorted(actions):
        a = actions[sid]
        tool = str(getattr(a, "tool", ""))
        if not _IDENT_RE.match(tool):
            tool = ARG_PLACEHOLDER
        outcomes = []
        for oc in (getattr(a, "expected_outcomes", None) or []):
            match = getattr(oc, "match", None) or {}
            status = match.get("status") if isinstance(match, dict) else None
            outcomes.append({
                "status": (str(status) if status is not None
                           and _IDENT_RE.match(str(status)) else ARG_PLACEHOLDER),
                "next": getattr(oc, "next", None) is not None,
                "compose": _compose_form(getattr(oc, "compose", None) or {}),
            })
        steps.append({
            "id": sid if re.match(r"^s\d+$", sid) else ARG_PLACEHOLDER,
            "tool": tool,
            "args_shape": _redact_value(dict(getattr(a, "args", None) or {})),
            "depends_on": [d for d in (getattr(a, "depends_on", None) or [])
                           if re.match(r"^s\d+$", str(d))],
            "intent_group": (str(getattr(a, "intent_group", ""))
                             if _IDENT_RE.match(
                                 str(getattr(a, "intent_group", "")))
                             else ARG_PLACEHOLDER),
            "outcomes": outcomes,
        })
    return {
        "v": 1,
        "clarity": (str(getattr(plan, "clarity", ""))
                    if _IDENT_RE.match(str(getattr(plan, "clarity", "")))
                    else ARG_PLACEHOLDER),
        "steps": steps,
        "compose": _compose_form(getattr(plan, "compose", None) or {}),
    }


def build_form_tags(form: dict) -> list[str]:
    """Теги формы — ТОЛЬКО из allowlist-словаря (никаких свободных строк)."""
    steps = form.get("steps", [])
    tags = []
    n = len(steps)
    tags.append("single" if n == 1 else ("chain" if n > 1 else "empty"))
    if any(len(s.get("outcomes", [])) > 1 for s in steps):
        tags.append("branching")
    if any(s.get("depends_on") for s in steps):
        tags.append("dependent")
    if form.get("clarity") == "needs_clarification":
        tags.append("clarify")
    tools = sorted({s["tool"] for s in steps if s.get("tool")
                    and s["tool"] != ARG_PLACEHOLDER})
    tags.extend(tools[:8])
    return tags


def is_library_enabled(tenant_id: str) -> bool:
    from sreda.config.settings import get_settings
    try:
        raw = getattr(get_settings(), "plan_library_enabled_tenants_raw", None)
        enabled = frozenset(
            t.strip() for t in (raw or "").split(",") if t.strip())
        return tenant_id in enabled
    except Exception:  # noqa: BLE001 — гейт fail-closed
        return False


def schedule_candidate_recording(
    *, tenant_id: str, run_id: str, plan: Any, exec_outcome: str,
    fallback_used: str | None, breakdown_shown: bool,
) -> None:
    """Вызывается из planner_chat ПОСЛЕ формирования ответа. За гейтом;
    вся работа — в отсоединённой задаче (request path не трогаем)."""
    if not is_library_enabled(tenant_id):
        return  # вне гейта: ноль побочек (чеклист п.1)
    if exec_outcome != "completed" or fallback_used or breakdown_shown:
        return  # строгие признаки кандидата (план: только чистый успех)
    try:
        form = project_plan_form(plan)
        tags = build_form_tags(form)
    except Exception:  # noqa: BLE001 — проекция не валит ход
        logger.warning("plan_library: проекция формы не удалась run=%s",
                       run_id, exc_info=True)
        return
    _create_task(
        _record_and_shadow(tenant_id=tenant_id, run_id=run_id,
                           form=form, tags=tags),
        name=f"plan_library:{run_id}",
    )


async def _record_and_shadow(*, tenant_id: str, run_id: str,
                             form: dict, tags: list[str]) -> None:
    try:
        async with _BG_SEMAPHORE:
            await asyncio.wait_for(
                asyncio.to_thread(_record_sync, tenant_id, run_id, form, tags),
                timeout=_BG_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("plan_library: запись run=%s превысила таймаут — дроп",
                       run_id)
    except Exception:  # noqa: BLE001
        logger.warning("plan_library: запись run=%s не удалась",
                       run_id, exc_info=True)


def _record_sync(tenant_id: str, run_id: str, form: dict,
                 tags: list[str]) -> None:
    """Своя сессия (request-сессия уже закрыта); идемпотентно по run_id;
    после записи — теневой отбор (оракул по форме текущего хода)."""
    from sreda.db.models.plan_library import PlanLibraryEntry
    from sreda.db.session import get_session_factory
    session = get_session_factory()()
    try:
        exists = (session.query(PlanLibraryEntry)
                  .filter_by(tenant_id=tenant_id, run_id=run_id).first())
        if exists is None:
            import uuid
            session.add(PlanLibraryEntry(
                id=f"plib_{uuid.uuid4().hex[:24]}",
                tenant_id=tenant_id, run_id=run_id,
                status="candidate", source="prod_turn",
                form_json=json.dumps(form, ensure_ascii=False,
                                     sort_keys=True),
                form_tags=json.dumps(tags, ensure_ascii=False),
                outcome_json=json.dumps({"outcome": "completed"}),
                registry_version="planner-chat-v1",
            ))
            session.commit()
        shadow = _shadow_select(session, tenant_id, form, tags,
                                exclude_run_id=run_id)
        logger.info(
            "plan_library: candidate run=%s записан; тень: %s",
            run_id,
            json.dumps(shadow, ensure_ascii=False)[:300],
        )
    finally:
        session.close()


def _shadow_select(session, tenant_id: str, form: dict, tags: list[str],
                   *, exclude_run_id: str, k: int = 2) -> dict:
    """Тень: «что подмешали БЫ» — только approved, совместимый реестр,
    пересечение тегов (v1 без векторов: embedding-добор отдельным
    воркером позже; форма-теги дают консервативный отбор)."""
    from sreda.db.models.plan_library import PlanLibraryEntry
    rows = (session.query(PlanLibraryEntry)
            .filter_by(tenant_id=tenant_id, status="approved")
            .order_by(PlanLibraryEntry.created_at.desc())
            .limit(200).all())
    me = set(tags)
    scored = []
    for r in rows:
        if r.run_id == exclude_run_id:
            continue
        try:
            their = set(json.loads(r.form_tags or "[]"))
        except ValueError:
            continue
        overlap = len(me & their)
        if overlap:
            scored.append((overlap, r.id))
    scored.sort(reverse=True)
    return {"approved_total": len(rows),
            "would_mix": [eid for _, eid in scored[:k]]}


def cleanup_plan_library(session, *, now: datetime | None = None) -> int:
    """TTL всем статусам (план r4; вызывается из retention_cleanup)."""
    from datetime import timedelta
    from sqlalchemy import and_, delete, or_
    from sreda.db.models.plan_library import PlanLibraryEntry
    now = now or datetime.now(timezone.utc)
    stmt = delete(PlanLibraryEntry).where(or_(
        and_(PlanLibraryEntry.status.in_(("candidate", "retired", "anti")),
             PlanLibraryEntry.created_at < now - timedelta(
                 days=CANDIDATE_TTL_DAYS)),
        and_(PlanLibraryEntry.status == "approved",
             PlanLibraryEntry.created_at < now - timedelta(
                 days=APPROVED_TTL_DAYS)),
    ))
    result = session.execute(stmt)
    return max(0, getattr(result, "rowcount", 0) or 0)
