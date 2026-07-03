"""#285 Фаза A: TurnPolicy — per-turn политика инструментов (SHADOW-режим).

Единый путь ReAct (план chatfact-unify-final): один граф/промпт-путь + per-turn
детерминированная ToolPolicy. Фаза A — ТЕНЬ: объект строится из решений СУЩЕСТВУЮЩЕГО
сплита (#197 intent / #221 домены / #256 таймауты / #197 капы) и логируется side-by-side;
исполнением НЕ управляет (byte-identical — пин-тесты). Начнёт управлять в Фазе B
(двухъярусный write-гейт: allowed_write | confirm_only_write).

ИНВАРИАНТ SHADOW: этот модуль НИЧЕГО не мутирует — build_turn_policy чистая функция;
запись только в отдельный state-канал turn_policy_json (legacy-каналы router_allowed_*
не трогаются — контракт отката, plans/285-phase0-inventory.md §2 строка (б)).
"""

from __future__ import annotations

import json

# Пин web-only набора сплита (= react_preflight._WEB_ONLY_TOOL_NAMES; синхронность держит
# пин-тест — прямой импорт не нужен, набор стабилен с #197).
WEB_ONLY_TOOL_NAMES = ("web_search", "fetch_url", "get_weather")

POLICY_VERSION = 1


def build_turn_policy(
    *,
    intent: str | None,
    router_allowed_read: list[str] | None,
    router_allowed_write: list[str] | None,
    chat_timeout_sec: float,
    task_timeout_sec: float,
    chat_provider: str,
    search_caps: dict[str, int] | None,
) -> dict:
    """Выразить решения сплита ЯВНЫМ объектом полиси (shadow: структурирование, не выбор).

    intent: эффективный интент сплита (None = preflight OFF → легаси task-путь).
    router_allowed_*: legacy-каналы #221 (None = фильтр не применяется).
    search_caps: тугие капы сплита для этого интента ({tool: cap} | None вне chat/fact).
    Возвращает JSON-сериализуемый dict; без ПД (только режимы/домены/имена инструментов).
    """
    is_chatfact = intent in ("chat", "fact")
    return {
        "v": POLICY_VERSION,
        "source": "split-shadow",
        "intent": intent,
        "prompt_variant": "chat_fact" if is_chatfact else "task",
        # chat/fact: жёсткий web-only код-гейт сплита (_bind_for); task/OFF: доменные каналы #221
        # (None = без фильтра). В Фазе B здесь появятся ярусы (allowed_write | confirm_only_write).
        "web_scope_only": is_chatfact,
        "allowed_read_domains": (None if is_chatfact else router_allowed_read),
        "allowed_write_domains": ([] if is_chatfact else router_allowed_write),
        "provider_profile": {
            "variant": "chat_fact" if is_chatfact else "task",
            "provider_hint": chat_provider if is_chatfact else "planner",
            "timeout_sec": chat_timeout_sec if is_chatfact else task_timeout_sec,
        },
        "search_budget": dict(search_caps) if search_caps else None,
        # guard сплита: выключен на chat/fact (:2406/:2428), легаси-recovery на task
        "guard_scope": "off" if is_chatfact else "legacy",
    }


def dumps_policy(policy: dict) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True)
