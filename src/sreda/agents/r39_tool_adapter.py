"""R-39 adapter layer для housewife tools.

Реальные `housewife_chat_tools` возвращают строки:

- ``"ok:scheduled:rem_X:2026-05-17T14:00:00+03:00"`` — успех schedule
- ``"ok:updated:rem_X:..."`` — успех update
- ``"ok:cancelled"`` — успех cancel
- ``"ok:saved:rec_X"`` / ``"ok:duplicate:rec_X"`` — успех save_recipe
- ``"ok:added:N:ids=[...]"`` — успех add_shopping_items (N = count, **не** entity_id)
- ``"ok:completed:tsk_X"`` — успех complete_task
- ``"error: ..."`` — ошибка
- ``"skipped:past:{iso}:late_by_Nmin"`` — schedule на прошлое время
- ``"skipped:other:..."`` — другой skip

R-39 executor ожидает callable который **бросает exception** для FAILURE
и возвращает dict для SUCCESS. Этот модуль конвертирует строки tools
в правильную форму:

- ``"ok:..."`` → возврат dict с разобранными полями (entity_id, trigger_iso, ...)
- ``"error:..."`` / ``"skipped:..."`` → raise ``R39ToolFailure`` со
  структурным ``error_code`` для template selection в renderer'е
  (см. ``ToolContract.failure_template_variants_by_code``).

Plus utility ``_is_past_iso`` для preflight-проверки `update_reminder`
(который в отличие от `schedule_reminder` сам не проверяет past-date).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


# ─── Exception ────────────────────────────────────────────────────────


class R39ToolFailure(Exception):
    """Tool вернул error/skipped или раннюю валидацию provalil.

    Атрибуты ``error_code`` и ``error_message`` подхватываются
    ``executor._record_failure`` (Slice 1) — попадают в
    ``ToolJournalEntry.error_code`` и ``result_data["error_code"]``,
    оттуда renderer выбирает specific template по
    ``ToolContract.failure_template_variants_by_code[error_code]``.
    """

    def __init__(self, *, error_code: str, error_message: str, raw: str = ""):
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
        self.raw = raw


# ─── Main parser ──────────────────────────────────────────────────────


def parse_tool_result_or_raise(tool_name: str, raw: Any) -> dict[str, Any]:
    """Парсит результат tool-вызова и либо возвращает dict, либо raises.

    Args:
        tool_name: имя инструмента (schedule_reminder и т.п.).
        raw: что вернул tool — обычно строка `"ok:..."` / `"error:..."`,
            но может быть и dict (для не-housewife tools) или None.

    Returns:
        dict с полями для подстановки в шаблон (entity_id, trigger_iso,
        status_token, ...). Содержание зависит от tool — см.
        ``parse_ok_segment``.

    Raises:
        R39ToolFailure: если raw — `"error:..."` или `"skipped:..."`,
            либо неожиданный формат.
    """
    if raw is None:
        raise R39ToolFailure(
            error_code="empty_result",
            error_message="tool returned None",
            raw="",
        )

    if not isinstance(raw, str):
        # Не строка — считаем сырое значение payload'ом (для совместимости
        # с моками которые возвращают dict напрямую).
        return {"raw": raw}

    if raw.startswith("ok:"):
        return parse_ok_segment(tool_name, raw)

    if raw.startswith("skipped:past:"):
        # Особый случай: schedule_reminder вернул для past trigger_iso.
        return _raise_past_date(raw)

    if raw.startswith("skipped:"):
        return _raise_skipped(raw)

    if raw.startswith("error:"):
        return _raise_error(raw)

    # Ни ok, ни error, ни skipped — defensive: считаем FAILURE
    raise R39ToolFailure(
        error_code="unexpected_format",
        error_message=f"unexpected_tool_result: {raw[:120]}",
        raw=raw,
    )


# ─── Tool-specific ok segment parsing ────────────────────────────────


def parse_ok_segment(tool_name: str, raw: str) -> dict[str, Any]:
    """Tool-specific парсинг ok-строки.

    Codex R3 MAJ: ``add_shopping_items`` возвращает
    ``"ok:added:N:ids=[...]"`` — 3-й segment это **count**, не entity_id.
    Нельзя слепо брать ``parts[2]`` как entity_id.

    Args:
        tool_name: schedule_reminder / update_reminder / cancel_reminder
            / save_recipe / add_shopping_items / complete_task / прочее.
        raw: строка вида ``"ok:scheduled:rem_X:iso"`` и т.п.

    Returns:
        dict с разобранными полями. Ключи зависят от tool:
            - schedule/update_reminder → entity_id, trigger_iso, raw_ok
            - cancel_reminder → raw_ok
            - save_recipe → entity_id, status_token (saved|duplicate), raw_ok
            - add_shopping_items → items_added_count, raw_ok (без entity_id!)
            - complete_task → entity_id, raw_ok
            - прочие → raw_ok
    """
    parts = raw.split(":", 3)
    payload: dict[str, Any] = {"raw_ok": raw}

    if tool_name in ("schedule_reminder", "update_reminder") and len(parts) >= 4:
        # ok:scheduled:rem_X:2026-...  /  ok:updated:rem_X:2026-...
        payload["status_token"] = parts[1]
        payload["entity_id"] = parts[2]
        payload["trigger_iso"] = parts[3]
        return payload

    if tool_name == "cancel_reminder":
        # ok:cancelled — id уже известен из args
        return payload

    if tool_name == "save_recipe" and len(parts) >= 3:
        # ok:saved:rec_X  /  ok:duplicate:rec_X
        payload["status_token"] = parts[1]
        payload["entity_id"] = parts[2]
        return payload

    if tool_name == "add_shopping_items" and len(parts) >= 3:
        # ok:added:N:ids=[...] — N это count, НЕ entity_id
        try:
            payload["items_added_count"] = int(parts[2])
        except (ValueError, IndexError):
            payload["items_added_count"] = 0
        # entity_id НЕ устанавливаем — для add_shopping_items это не имеет смысла
        return payload

    if tool_name == "complete_task" and len(parts) >= 3:
        # ok:completed:tsk_X
        payload["status_token"] = parts[1]
        payload["entity_id"] = parts[2]
        return payload

    # Default — отдаём raw_ok без дополнительной разборки
    return payload


# ─── Past-date guard ─────────────────────────────────────────────────


def is_past_iso(iso: str, *, grace_minutes: int = 2) -> bool:
    """True если ``iso`` раньше now минус grace_minutes.

    Используется R-39 adapter preflight'ом для ``update_reminder`` —
    реальный housewife service ``update_reminder`` в отличие от
    ``schedule_reminder`` НЕ имеет past-date check. Без этого guard
    LLM может emit-нуть update с past trigger_iso → пустое pending
    reminder в БД.

    Args:
        iso: ISO datetime строка (с offset или Z).
        grace_minutes: запас для NTP drift / network latency. По умолчанию 2.

    Returns:
        True если время в прошлом на ≥ grace_minutes минут.
        False если время в будущем / в пределах grace окна /
        строка не парсится (graceful).
    """
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        # Naive datetime — считаем UTC (как делает _normalize_iso_to_minute)
        dt = dt.replace(tzinfo=timezone.utc)
    threshold = datetime.now(timezone.utc) - timedelta(minutes=grace_minutes)
    return dt < threshold


# ─── Внутренние helpers ──────────────────────────────────────────────


def _raise_past_date(raw: str) -> dict[str, Any]:
    """Convert `skipped:past:...` → R39ToolFailure(past_date)."""
    payload = raw[len("skipped:past:"):]
    raise R39ToolFailure(
        error_code="past_date",
        error_message=payload or "trigger time already passed",
        raw=raw,
    )


def _raise_skipped(raw: str) -> dict[str, Any]:
    """Convert generic `skipped:...` → R39ToolFailure(skipped_other)."""
    payload = raw[len("skipped:"):]
    raise R39ToolFailure(
        error_code="skipped_other",
        error_message=payload or "tool skipped",
        raw=raw,
    )


def _raise_error(raw: str) -> dict[str, Any]:
    """Convert `error: ...` → R39ToolFailure с категоризированным error_code."""
    msg = raw[len("error:"):].strip()
    low = msg.lower()
    if "no user_id" in low:
        code = "no_user_id"
    elif "not found" in low:
        code = "entity_not_found"
    elif "cannot parse" in low or "trigger_iso" in low:
        code = "parse_failure"
    elif "internal" in low:
        code = "tool_internal"
    elif "empty" in low:
        code = "empty_input"
    else:
        code = "tool_error"
    raise R39ToolFailure(error_code=code, error_message=msg, raw=raw)
