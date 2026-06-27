# -*- coding: utf-8 -*-
"""#243 — free-tier llm_turns квота на текст-пути ReAct: зависимости гейта.

Гейт (telegram_inbound, блок `if _use_react:`) при исчерпании дневной квоты
free-юзера шлёт UPGRADE_COPY["llm_daily_or_monthly"] вместо LLM-вызова.

Покрытие:
- Контракт исчерпания `try_consume("llm_turns")` → False после лимита — уже
  тестируется в `test_phase2_services::test_usage_ledger_insert_path_quota_check`.
- Ключ UPGRADE_COPY, который шлёт гейт — здесь (переименование → KeyError в проде).
- Полный inbound-интеграционный тест (free + react-гейт + exhaust → UPGRADE_COPY,
  react НЕ вызван, inbound ignored) — отдельный follow-up (тяжёлая настройка
  react-предусловий: онбординг + react_loop_enabled + мок EntitlementGate/handle_turn).
"""
from __future__ import annotations


def test_upgrade_copy_llm_daily_key_exists() -> None:
    # Гейт #243 при превышении шлёт UPGRADE_COPY["llm_daily_or_monthly"] —
    # ключ обязан существовать (переименование → KeyError в боевом пути).
    from sreda.services.upgrade_copy import UPGRADE_COPY

    assert UPGRADE_COPY.get("llm_daily_or_monthly")
