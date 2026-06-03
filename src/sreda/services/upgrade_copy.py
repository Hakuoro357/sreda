"""Single source of truth для upgrade-prompt copy.

Phase 2 of free-tier-subscription plan. Когда free-tier юзер
исчерпывает дневной/месячный лимит LLM или voice — бот возвращает
один из этих текстов (вместо вызова LLM). Также используется
SignupAbuseGuard когда signup blocked, и EntitlementGate когда
tenant suspended.

Канонический contact placeholder — `@sreda_support`. Боря может
swap'нуть на URL `sredaspace.ru/pricing` one-line edit когда landing
появится.

Usage:
    from sreda.services.upgrade_copy import UPGRADE_COPY
    text = UPGRADE_COPY["llm_daily_or_monthly"]
"""

from __future__ import annotations

from sreda.services.usage_ledger import (
    SREDA_FREE_LLM_DAILY,
    SREDA_FREE_LLM_MONTHLY,
)


UPGRADE_CONTACT = "@sreda_support"


UPGRADE_COPY: dict[str, str] = {
    # Free tier quota exhaustion. Caps are interpolated from the single source
    # of truth (usage_ledger) so this copy can never drift from the actual gate
    # (Codex limit-review R1, both reviewers: stale "20" must not survive a cap
    # change).
    "llm_daily_or_monthly": (
        f"Лимит общения исчерпан. Если {SREDA_FREE_LLM_DAILY} ходов в день / "
        f"{SREDA_FREE_LLM_MONTHLY} в месяц мало — напиши {UPGRADE_CONTACT} про "
        f"расширенный тариф (включает безлимитный чат + веб-поиск)."
    ),
    "voice_daily_or_monthly": (
        f"Лимит голосовых исчерпан. Можем продолжить текстом — или "
        f"напиши {UPGRADE_CONTACT} про расширенный тариф."
    ),

    # EntitlementGate states
    "no_active_subscription": (
        f"Доступ временно ограничен. Свяжись с {UPGRADE_CONTACT}."
    ),
    "suspended": (
        f"Доступ ограничен. Свяжись с {UPGRADE_CONTACT}."
    ),

    # SignupAbuseGuard rejections
    "signups_closed": (
        "Регистрация временно закрыта. Загляни позже."
    ),
    "free_tier_full": (
        f"Бесплатных мест на сегодня нет. Напиши "
        f"{UPGRADE_CONTACT} — подключим расширенный тариф."
    ),
    "rate_limited": (
        "Слишком много попыток регистрации. Попробуй позже."
    ),
}
