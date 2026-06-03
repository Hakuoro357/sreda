"""FreeTierCounter — счётчик LLM-вызовов в день для free-tier.

Часть A плана v2 нового клиентского пути. Лимит =
``usage_ledger.SREDA_FREE_LLM_DAILY`` turn'ов в день (single source); на
превышении — отлуп с кнопками «Оформить подписку» / «Напомнить утром» /
«Понятно». Подписчики (``housewife_assistant`` в
``active_feature_keys``) не трогаются — без лимитов.

Что НЕ считается:
- Callback rem_done / rem_snooze (служебка).
- Проактивные сообщения worker'а (не юзер инициировал).
- Scripted-ответы pending-бота (нет LLM).

Что считается:
- Text-turn обычного чата (1 turn = 1 инкремент, вне зависимости от
  числа LLM-итераций внутри tool-loop'а).
- Voice-turn (после STT транскрипт = обычный text-turn).
- btn_reply callback (приравнивается к text-message).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.free_tier import FreeTierUsage
from sreda.services.agent_capabilities import active_feature_keys
from sreda.services.usage_ledger import SREDA_FREE_LLM_DAILY

logger = logging.getLogger(__name__)

# Single source of truth = ``usage_ledger.SREDA_FREE_LLM_DAILY`` (the Phase 2
# gate). This legacy FreeTierCounter skips ALL subscribed users — sreda_free,
# paid AND grandfathered all hold an active feature_key, so ``is_subscribed``
# is True for everyone who reaches it — i.e. it is effectively dead in the live
# handler path. We derive (not re-hardcode) so there is no second daily cap to
# drift out of sync (Codex limit-review R1, both reviewers).
FREE_TIER_DAILY_LIMIT = SREDA_FREE_LLM_DAILY  # LLM-turn'ов в сутки без подписки


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> date:
    return _utcnow().date()


class FreeTierCounter:
    """Dispatcher'-level шлюз для лимита бесплатных вызовов."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Основной сценарий
    # ------------------------------------------------------------------

    def is_subscribed(self, *, tenant_id: str, feature_key: str | None) -> bool:
        """True если у тенанта активная подписка на этот скил."""
        if not tenant_id or not feature_key:
            return False
        return feature_key in active_feature_keys(self.session, tenant_id)

    def increment_and_check(
        self,
        *,
        tenant_id: str,
        user_id: str,
        feature_key: str | None = None,
    ) -> tuple[int, bool]:
        """Атомарно: +1 к счётчику сегодняшнего дня, возвращает
        ``(new_count, is_over_limit)``.

        Подписанные юзеры — счётчик не трогает, возвращает (0, False).
        Лимит ПРЕВЫШЕН когда new_count > FREE_TIER_DAILY_LIMIT.

        Каждый chat-turn должен вызывать этот метод ОДИН раз перед
        LLM. Если is_over_limit=True — отдаём отлуп и НЕ вызываем LLM.
        """
        if self.is_subscribed(tenant_id=tenant_id, feature_key=feature_key):
            return (0, False)
        if not tenant_id or not user_id:
            # Дефенсивно: без user_id счётчик не строится, пропускаем
            # (это служебный путь, логов и без того много).
            return (0, False)

        today = _today_utc()
        row = self._get_or_create(tenant_id, user_id, today)
        row.llm_calls = (row.llm_calls or 0) + 1
        row.updated_at = _utcnow()
        self.session.flush()
        new_count = row.llm_calls
        return (new_count, new_count > FREE_TIER_DAILY_LIMIT)

    def usage_today(
        self, *, tenant_id: str, user_id: str,
    ) -> int:
        """Текущий счётчик на сегодня (для дебага / для pre-paywall digest)."""
        today = _today_utc()
        row = (
            self.session.query(FreeTierUsage)
            .filter(
                FreeTierUsage.tenant_id == tenant_id,
                FreeTierUsage.user_id == user_id,
                FreeTierUsage.day == today,
            )
            .one_or_none()
        )
        return row.llm_calls if row else 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_create(
        self, tenant_id: str, user_id: str, day: date,
    ) -> FreeTierUsage:
        row = (
            self.session.query(FreeTierUsage)
            .filter(
                FreeTierUsage.tenant_id == tenant_id,
                FreeTierUsage.user_id == user_id,
                FreeTierUsage.day == day,
            )
            .one_or_none()
        )
        if row is not None:
            return row
        row = FreeTierUsage(
            id=f"ftu_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            day=day,
            llm_calls=0,
            updated_at=_utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row
