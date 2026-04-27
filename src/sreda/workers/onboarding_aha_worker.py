"""Onboarding Aha worker — проактивные сообщения для только-что-одобренных тенантов.

Часть C плана v2. Запускается из ``job_runner`` раз в тик (вместе с
``housewife_reminder_worker``). Реализует **Aha-2**: если у свежего
подписчика в ``FamilyMember.notes`` есть упоминание диеты/аллергии,
через ~24ч после approval бот сам предлагает составить меню с учётом
этих особенностей.

Aha-1 (утреннее напоминание) и Aha-6 (voice-disambiguation) не
требуют отдельного worker'а — они случаются естественно через
``schedule_reminder``/``reply_with_buttons`` когда LLM следует
prompt-правилам.

Идемпотентность: каждое успешное срабатывание создаёт «sentinel»-
``FamilyReminder`` со ``source_memo='aha2:<tenant_id>'`` и сразу
``status='fired'``. Повторных вызовов этот маркер не допустит.

Анти-сталкер:
- сообщение не шлётся если approved_at старше 48ч (не наваливаемся на
  «пропустивших первый день»);
- не шлётся если уже был sentinel-маркер;
- шлётся в дневное окно 10:00–13:00 UTC (~13–16 MSK).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyMember, FamilyReminder

logger = logging.getLogger(__name__)

HOUSEWIFE_FEATURE_KEY = "housewife_assistant"

# Окно «через 24ч после approval, но не старше 48ч». Юзер должен
# успеть получить Aha-2 на следующий день, но не на третий.
AHA2_MIN_AGE = timedelta(hours=20)
AHA2_MAX_AGE = timedelta(hours=48)

# UTC-часы для отправки. 10-13 UTC = 13-16 MSK — удобное окно, не утро
# (когда все спешат) и не вечер (когда уже не до меню на неделю).
AHA2_SEND_HOUR_START_UTC = 10
AHA2_SEND_HOUR_END_UTC = 13

# Ключевые слова в ``FamilyMember.notes`` (EncryptedString, мы читаем
# расшифрованным). Простой keyword-match; LLM ничего не зовём.
_DIET_KEYWORDS: tuple[str, ...] = (
    "диет", "аллерг", "без ", "безлакт", "безмолоч", "безглютен",
    "лактоз", "глютен", "сахарн", "веган", "вегетар", "постн",
    "не ест", "нельзя",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class OnboardingAhaWorker:
    """Проактивный Aha-2: «запомнила диету — предложила меню»."""

    def __init__(self, session: Session) -> None:
        self.session = session

    async def process_pending(
        self, *, limit: int = 20, now: datetime | None = None,
    ) -> int:
        """Один тик. Возвращает сколько Aha-2 отправили в этот тик."""
        current = now or _utcnow()
        if not (AHA2_SEND_HOUR_START_UTC <= current.hour < AHA2_SEND_HOUR_END_UTC):
            # Не наше окно — ничего не делаем.
            return 0

        # Ищем тенантов одобренных 20-48 часов назад.
        lo = current - AHA2_MAX_AGE
        hi = current - AHA2_MIN_AGE
        tenants = (
            self.session.query(Tenant)
            .filter(
                Tenant.approved_at.isnot(None),
                Tenant.approved_at >= lo,
                Tenant.approved_at <= hi,
            )
            .limit(limit)
            .all()
        )

        sent = 0
        for tenant in tenants:
            try:
                if self._try_send_aha2(tenant, current):
                    sent += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "aha2: failed for tenant=%s, skipping tick",
                    tenant.id,
                )
                self.session.rollback()
                continue
        if sent:
            self.session.commit()
            logger.info("aha2: sent=%d", sent)
        return sent

    # ------------------------------------------------------------------

    def _try_send_aha2(self, tenant: Tenant, now: datetime) -> bool:
        # 1) Идемпотентность — sentinel уже есть?
        sentinel = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.tenant_id == tenant.id,
                FamilyReminder.source_memo == f"aha2:{tenant.id}",
            )
            .first()
        )
        if sentinel is not None:
            return False

        # 2) Ищем члена семьи с диетой/аллергией в notes.
        member = self._find_member_with_diet(tenant.id)
        if member is None:
            # Нет диет — Aha-2 неприменимо. Маркируем sentinel всё
            # равно, чтобы не перепроверять каждый тик.
            self._create_sentinel(tenant, now, diet_member_name=None)
            return False

        # 3) Узнаём chat_id для доставки.
        user, chat_id = self._resolve_user_and_chat(tenant.id)
        if not user or not chat_id:
            self._create_sentinel(tenant, now, diet_member_name=None)
            return False

        workspace_id = self._resolve_workspace_id(tenant.id)
        if not workspace_id:
            self._create_sentinel(tenant, now, diet_member_name=None)
            return False

        # 4) Формируем текст и кнопки.
        member_name = (member.name or "кто-то").strip()
        text = (
            f"🍽️ У {member_name} есть диета — "
            f"могу собрать меню на неделю под неё. "
            f"Подойдёт всем в семье."
        )

        # 5) Создаём токены для кнопок.
        from sreda.services.reply_buttons import ReplyButtonService

        pairs = ReplyButtonService(self.session).create_tokens(
            tenant_id=tenant.id,
            user_id=user.id,
            labels=[
                "Да, собери меню",
                "Не сейчас",
                "Покажи список блюд",
            ],
        )
        reply_markup: dict | None = None
        if pairs:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": f"btn_reply:{tok}"}]
                    for tok, label in pairs
                ],
            }

        # 6) Кладём в outbox. Доставку делает OutboxDeliveryWorker.
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=tenant.id,
            workspace_id=workspace_id,
            channel_type="telegram",
            feature_key=HOUSEWIFE_FEATURE_KEY,
            status="pending",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        if hasattr(OutboxMessage, "user_id"):
            outbox.user_id = user.id
        if hasattr(OutboxMessage, "is_interactive"):
            outbox.is_interactive = False
        self.session.add(outbox)

        # 7) Sentinel — чтобы второй раз не сработало.
        self._create_sentinel(tenant, now, diet_member_name=member_name)
        self.session.flush()
        return True

    def _create_sentinel(
        self, tenant: Tenant, now: datetime, *, diet_member_name: str | None,
    ) -> None:
        sentinel = FamilyReminder(
            id=f"rem_{uuid4().hex[:24]}",
            tenant_id=tenant.id,
            user_id=None,
            title=f"aha2-sentinel:{diet_member_name or 'n/a'}",
            trigger_at=now,
            next_trigger_at=None,
            recurrence_rule=None,
            status="fired",
            source_memo=f"aha2:{tenant.id}",
        )
        self.session.add(sentinel)
        self.session.flush()

    def _find_member_with_diet(self, tenant_id: str) -> FamilyMember | None:
        # Читаем всех членов семьи (EncryptedString расшифровывается
        # автоматически в `notes` при доступе). Ищем keyword-match.
        members = (
            self.session.query(FamilyMember)
            .filter(FamilyMember.tenant_id == tenant_id)
            .all()
        )
        for m in members:
            notes_text = (m.notes or "").lower()
            if not notes_text:
                continue
            for kw in _DIET_KEYWORDS:
                if kw in notes_text:
                    return m
        return None

    def _resolve_user_and_chat(
        self, tenant_id: str,
    ) -> tuple[User | None, str | None]:
        user = (
            self.session.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.telegram_account_id.is_not(None),
            )
            .order_by(User.id.asc())
            .first()
        )
        if user is None:
            return None, None
        return user, user.telegram_account_id

    def _resolve_workspace_id(self, tenant_id: str) -> str | None:
        ws = (
            self.session.query(Workspace)
            .filter(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        return ws.id if ws else None
