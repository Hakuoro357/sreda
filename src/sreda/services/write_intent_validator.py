"""R-30 option C (2026-05-15): soft validator для unsolicited writes.

После dispatch'а mutating tool_call'а — проверяем `user_text` последнего
сообщения юзера. Если он содержит read-verb pattern И НЕ содержит
write-verb pattern → это unsolicited write (classical R-20 confab class:
бот «сам решил» что юзер хочет write).

**Soft monitoring** — НЕ блокирует tool execution. Только:
- WARN log с trace_id, tenant, tool name, user_text excerpt
- INFO admin alert (dedup per (tool, UTC-date) — max 1 alert / 30 min / tool;
  ≈48 alerts/UTC-day/tool в worst-case при continuous confab loop).

Severity=INFO (а не P2) выбрана сознательно: R-30 C это measurement
не incident — мы хотим knowledge о frequency, не panic. 30-min window
balances signal vs alert spam.

Дополняет `WRITE_GUARD` (option A из R-30): A пытается предотвратить
unsolicited write через tool description, C ловит случаи когда A не
сработал. Боты не идеальны — мы хотим знать частоту, чтобы понять
need ли R-20 Stage 1 быстрее.

Roadmap:
- R-30 A (deployed 2026-05-15 commit 5032d40): tool description guard
- **R-30 C (this module)**: post-iter validator с alert
- R-20 Stage 1 (план approved, не реализован): write_intent_detector
  через regex+keyword → если recall<95% → small LLM classifier. Plus
  final-output validator с REJECT_WITH_REPAIR. R-30 C будет редундант
  с R-20 Stage 1 final-output validator's Logic D — можно отключить
  после deploy R-20.

Pattern-based classifier (regex, не LLM) — fast, deterministic, no
extra latency. False positives OK (legitimate write triggers alert но
работает); false negatives тоже OK (silently миссим — текущий baseline).
Цель — **измерить frequency**, не block.
"""

from __future__ import annotations

import re


# Read-verb pattern: pure «show/ask/list/find» intent.
# Designed для русского user_text + English fallback.
# Граница `\b` не работает идеально для кириллицы → используем
# простой substring match с lookaround на word-edge characters.
READ_VERB_RE = re.compile(
    r"(?<![а-яА-ЯёЁ\w])("
    # Русские read-глаголы
    r"покажи|показ[ыь]|"
    r"посмотр[иь]|посмотреть|глянь|"
    r"что у меня (в|на|с)|"
    r"что (есть|сейчас|там|тут|у меня) (в|на|с|по)|"
    r"что по (задачам|спискам|меню|покупкам|рецептам)|"
    r"что с (задачами|списком|меню|покупками|рецептами)|"
    r"какие|какой|какая|какое|"
    r"есть ли|есть что|есть (задачи|напоминания|рецепты|покупки|меню)|"
    r"прочитай|прочти|перечитай|"
    r"перечисли|выведи|выводи|"
    r"дай (список|меню|рецепт|задачи|напоминания|покупки)|"
    r"скинь|пришли|"
    r"сколько|когда|во сколько|"
    r"где (он|она|оно|они|это|у меня)|"
    r"найди|поищи|ищи|"
    r"открой(?! новый)|зайди в|"
    r"расскажи (про|о|об)|опиши|"
    # English fallback (mimo иногда отвечает английским)
    r"show|list|find|search|"
    r"what (is|are|do I)|where (is|are)|how many"
    r")(?![а-яА-ЯёЁ\w])",
    re.IGNORECASE | re.UNICODE,
)

# Write-verb pattern: explicit modification intent.
# Matches WRITE_GUARD list в housewife_chat_tools.py + extra domain verbs.
WRITE_VERB_RE = re.compile(
    r"(?<![а-яА-ЯёЁ\w])("
    # Русские write-глаголы (matched WRITE_GUARD)
    r"добав[ьи](те)?|допиши|"
    r"запиши|записать|занеси|занести|"
    r"поставь|поставить|"
    r"сохрани|сохранить|"
    r"создай|создать|"
    r"обнови|обновить|"
    r"измени|изменить|поменяй|поменять|"
    r"удали|удалить|сотри|стереть|"
    r"отмени|отменить|"
    r"напомни|напоминай|напоминать|"
    r"запланируй|спланируй|"
    r"отметь|отметить|"
    r"перенеси|перенести|"
    r"вычеркни|вычеркнуть|"
    r"купи|закинь|"
    r"положи (в |на )|"
    r"составь|"
    r"внеси|"
    # English fallback
    r"add|save|create|update|delete|remove|cancel|"
    r"remind|schedule|mark|move|cross out"
    r")(?![а-яА-ЯёЁ\w])",
    re.IGNORECASE | re.UNICODE,
)


# Frozen set всех mutating tools из housewife_chat_tools.build_housewife_tools.
# Держим здесь (не в test_write_guard.py) чтобы handlers.py / executors могли
# импортировать без тестовой зависимости. Synced с WRITE_TOOLS_EXPECTED
# в test_write_guard — test_consistency_with_write_tools_expected ниже
# проверяет equality.
HOUSEWIFE_MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    # reminders
    "schedule_reminder", "update_reminder", "cancel_reminder",
    # onboarding (state-changing)
    "onboarding_answered", "onboarding_deferred", "onboarding_complete",
    # shopping
    "add_shopping_items", "mark_shopping_bought", "remove_shopping_items",
    "update_shopping_item", "update_shopping_items_category",
    "clear_bought_shopping",
    # recipes
    "save_recipe", "save_recipes_batch", "delete_recipe",
    "update_recipe",  # #210: правка рецепта на месте
    # menu
    "plan_week_menu", "update_menu_item", "generate_shopping_from_menu",
    "clear_menu",
    # family
    "add_family_members", "update_family_member", "remove_family_member",
    # tasks
    "add_task", "update_task", "complete_task", "uncomplete_task",
    "cancel_task", "delete_task", "attach_reminder", "detach_reminder",
    # checklists (incl R-30 culprit add_checklist_items)
    "create_checklist", "add_checklist_items", "move_task_to_checklist",
    "mark_checklist_item_done", "delete_checklist_item", "archive_checklist",
    "update_checklist_item",  # #210: правка пункта на месте
    # R-33 (2026-05-15): task↔checklist link tools
    "link_task_to_checklist", "unlink_task",
})


def classify_user_intent(user_text: str) -> dict[str, bool]:
    """Returns ``{has_write_verb, has_read_verb}`` flags from ``user_text``.

    Both can be True (compound «покажи и добавь рубашку» = legitimate
    multi-intent turn). Both False — neither read nor write verb detected
    (e.g., chitchat «привет» или free-form text без явных verbs).

    Empty / whitespace-only → both False.
    """
    if not user_text or not user_text.strip():
        return {"has_write_verb": False, "has_read_verb": False}
    return {
        "has_write_verb": bool(WRITE_VERB_RE.search(user_text)),
        "has_read_verb": bool(READ_VERB_RE.search(user_text)),
    }


def alert_if_unsolicited_write(
    *,
    user_text: str,
    tool_name: str,
    result_str: str,
    tenant_id: str,
    feature_key: str | None,
    iter_num: int,
    user_id: str | None,
    trace_id: str | None,
) -> bool:
    """R-30 C integration helper: detect + log + alert unsolicited write.

    Called from ``handlers._dispatch_tool_calls_batch`` loop. Best-effort:
    exceptions swallowed (validator МЕНЕЕ critical чем turn execution).

    Returns True if alert was triggered (для test assertions / metrics
    в caller). False — either not unsolicited, or some exception path.

    Wraps the full WARN log + ``send_admin_alert`` flow с severity=INFO
    (30min dedup window, max 48 alerts/tool/day). Dedup key includes
    today's date — keys roll over per UTC day.

    Codex R1 R-30 C review extracted from inline handlers.py block для
    testability (Codex MINOR 3) + cleaner side-effect scope.
    """
    try:
        if not is_unsolicited_write(user_text, tool_name=tool_name):
            return False

        # Lazy imports — only on the rare alert path. Avoid pulling
        # admin_alerts (heavyweight: DB session, threading) для chitchat
        # turns без unsolicited writes.
        import logging
        from datetime import datetime, timezone
        from sreda.services.admin_alerts import send_admin_alert

        # UTC date для дедупликации (Codex R2 MINOR): host-local date
        # сдвигает rollover на host timezone; UTC даёт reproducible
        # bucket'ы независимо от deploy timezone.
        _utc_date = datetime.now(timezone.utc).date()

        logger = logging.getLogger(__name__)
        logger.warning(
            "CHAT_UNSOLICITED_WRITE tenant=%s user=%s feature=%s iter=%d "
            "tool=%s trace=%s user_text=%r result=%r",
            tenant_id, user_id or "?", feature_key, iter_num,
            tool_name, trace_id,
            (user_text or "")[:120],
            (result_str or "")[:120],
        )
        send_admin_alert(
            severity="INFO",
            title=f"Unsolicited write: {tool_name}",
            body=(
                f"Tool «{tool_name}» вызван без write-verb в user_text.\n\n"
                f"User said: {(user_text or '')[:300]!r}\n"
                f"Result: {(result_str or '')[:200]}\n"
                f"Trace: {trace_id}"
            ),
            dedupe_key=(
                f"unsolicited_write:{tool_name}:{_utc_date.isoformat()}"
            ),
            extra_context={
                "tenant": tenant_id,
                "feature": feature_key,
                "iter": iter_num,
                "tool": tool_name,
                "trace_id": trace_id,
            },
        )
        return True
    except Exception:  # noqa: BLE001 — validator MUST not crash turn
        import logging
        logging.getLogger(__name__).exception(
            "R-30 C alert_if_unsolicited_write failed (best-effort, swallowed)",
        )
        return False


def is_unsolicited_write(
    user_text: str,
    *,
    tool_name: str,
    mutating_tool_names: frozenset[str] = HOUSEWIFE_MUTATING_TOOL_NAMES,
) -> bool:
    """True если detected unsolicited write pattern:
    - ``tool_name`` ∈ ``mutating_tool_names`` (бот сделал write)
    - ``user_text`` содержит read-verb
    - ``user_text`` НЕ содержит write-verb

    Compound («покажи и добавь») → False (write-verb присутствует,
    legitimate multi-intent).

    Free-form text без verbs («сегодня жарко») → False (не read-intent,
    может быть legitimate context-based action — не R-30 class).

    Empty / non-mutating tool → False.
    """
    if not user_text or tool_name not in mutating_tool_names:
        return False
    intent = classify_user_intent(user_text)
    return intent["has_read_verb"] and not intent["has_write_verb"]
