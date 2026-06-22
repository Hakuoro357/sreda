"""Outbox delivery worker (Phase 2d).

Polls the ``outbox_messages`` queue and routes each pending row through
the per-user delivery policy:

  * ``send``  → Telegram send + status='sent'
  * ``defer`` → set ``scheduled_at`` to end-of-quiet-window, leave pending
  * ``drop``  → status='muted' (user set ``priority=mute`` for this skill)

Runs in the same polling loop as the skill-platform processor. The
cadence is defined by ``Settings.job_poll_interval_seconds``.

Note: interactive replies (replies to user commands) are already sent
inline by ``node_persist_replies`` — they arrive at the worker with
``status='sent'`` or ``'pending'`` (delivery retry). The worker just
retries those pending rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from sreda.config.bot_registry import (
    LEGACY_NULL_BOT_KEY,
    TelegramBotRegistry,
    telegram_client_for,
)
from sreda.db.models.core import OutboxMessage
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.max.client import MaxClient, MaxDeliveryError
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.delivery_policy import DeliveryKind, decide_delivery
from sreda.services import trace

logger = logging.getLogger(__name__)


class OutboxDeliveryWorker:
    def __init__(
        self,
        session: Session,
        telegram_client: TelegramClient | None = None,
        max_client: "MaxClient | None" = None,
        registry: TelegramBotRegistry | None = None,
    ) -> None:
        self.session = session
        self.telegram = telegram_client
        # MAX channel client (Phase 6 of MAX integration sprint).
        # None — MAX channel delivery будет skip'аться (fallback chain
        # попробует TG если у юзера есть и telegram_account_id).
        self.max = max_client
        # Phase 4a: bot registry for multi-bot routing.  When set,
        # _send_now resolves the client via telegram_client_for(row.bot_key).
        # None falls back to self.telegram (backward-compat / tests that
        # don't wire a registry).
        self.registry = registry

    async def process_pending_messages(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> int:
        now_utc = now or datetime.now(timezone.utc)
        rows = (
            self.session.query(OutboxMessage)
            .filter(
                OutboxMessage.status == "pending",
                OutboxMessage.channel_type.in_(("telegram", "max")),
                or_(
                    OutboxMessage.scheduled_at.is_(None),
                    OutboxMessage.scheduled_at <= now_utc,
                ),
            )
            .order_by(OutboxMessage.created_at.asc())
            .limit(limit)
            .all()
        )
        processed = 0
        for row in rows:
            await self._process_one(row, now_utc=now_utc)
            processed += 1
        return processed

    async def _process_one(self, row: OutboxMessage, *, now_utc: datetime) -> None:
        # #187 soft-delete — fencing (дверь #9): тенант мог быть удалён ПОСЛЕ
        # постановки строки в outbox. Проверяем В ТОЙ ЖЕ TX прямо перед внешней
        # отправкой; удалён → терминируем без send (тот же drop_reason что drain).
        from sreda.services.tenant_lifecycle import is_tenant_active

        if not is_tenant_active(self.session, row.tenant_id):
            row.status = "dropped"
            row.drop_reason = "tenant_deleted"
            self.session.commit()
            return

        profile_dict, skill_config_dict = self._load_user_context(row)
        decision = decide_delivery(
            profile=profile_dict,
            skill_config=skill_config_dict,
            feature_key=row.feature_key,
            is_interactive=bool(row.is_interactive),
            now_utc=now_utc,
        )

        if decision.kind == DeliveryKind.drop:
            row.status = "muted"
            self.session.commit()
            return
        if decision.kind == DeliveryKind.defer:
            row.scheduled_at = decision.defer_until_utc
            # status stays 'pending'; worker will re-check after defer.
            self.session.commit()
            return

        # Send path
        await self._send_now(row)

    def _load_user_context(
        self, row: OutboxMessage
    ) -> tuple[dict | None, dict | None]:
        if not row.user_id:
            return None, None
        repo = UserProfileRepository(self.session)
        profile = repo.get_profile(row.tenant_id, row.user_id)
        profile_dict: dict | None = None
        if profile is not None:
            profile_dict = {
                "timezone": profile.timezone,
                "quiet_hours": UserProfileRepository.decode_quiet_hours(profile),
            }
        skill_config_dict: dict | None = None
        if row.feature_key:
            config = repo.get_skill_config(
                row.tenant_id, row.user_id, row.feature_key
            )
            if config is not None:
                skill_config_dict = {
                    "notification_priority": config.notification_priority,
                    "token_budget_daily": config.token_budget_daily,
                }
        return profile_dict, skill_config_dict

    async def _send_now(self, row: OutboxMessage) -> None:
        # Phase 6 of MAX integration sprint (2026-05-04): branch by channel.
        # MAX и Telegram имеют разные SDK / payload форматы — разносим
        # на отдельные методы. Для будущей расширяемости (e.g. дополнительный
        # канал) этот dispatch расширяется одной веткой.
        if row.channel_type == "max":
            await self._send_now_max(row)
            return

        # Default: Telegram path.
        # Phase 4a: resolve client via registry when available so each row
        # is delivered through the bot that originally received the turn.
        # Falls back to self.telegram for backward-compat (legacy wiring,
        # tests that don't pass a registry).
        tg_client: TelegramClient | None
        if self.registry is not None:
            effective_bot_key = row.bot_key or LEGACY_NULL_BOT_KEY
            try:
                tg_client = telegram_client_for(effective_bot_key, self.registry)
            except KeyError:
                if row.bot_key is None:
                    # NULL bot_key: migration-window row — safe to fall back.
                    logger.warning(
                        "outbox delivery: NULL bot_key on row %s, "
                        "falling back to LEGACY_NULL_BOT_KEY",
                        row.id,
                    )
                    tg_client = telegram_client_for(LEGACY_NULL_BOT_KEY, self.registry)
                else:
                    # Explicit unknown key: corruption / stale config — fail
                    # closed, do NOT route through the wrong bot.
                    logger.error(
                        "outbox delivery: unknown bot_key %r on row %s "
                        "(not a NULL legacy row). Marking failed.",
                        row.bot_key,
                        row.id,
                    )
                    row.status = "failed"
                    row.drop_reason = "unknown_bot_key"
                    self.session.commit()
                    return
        else:
            tg_client = self.telegram

        if tg_client is None:
            # Dev/test path with no Telegram wired — just mark sent so
            # tests can assert policy without a client mock.
            row.status = "sent"
            self.session.commit()
            return
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            logger.exception("outbox delivery: bad payload_json for %s", row.id)
            row.status = "failed"
            self.session.commit()
            return

        # Extract end-to-end trace (if the uvicorn process stashed it
        # when enqueuing). Worker emits the final block here after the
        # send attempt so the block lands with the complete timing
        # including delivery latency. Removing it from ``payload`` BEFORE
        # the send means the user-facing Telegram message body doesn't
        # carry our internal bookkeeping — only ``chat_id``/``text``/
        # ``reply_markup``/``parse_mode`` keys are read downstream.
        trace_payload = payload.pop("_trace", None)

        try:
            send_response = await tg_client.send_message(
                chat_id=payload.get("chat_id"),
                text=payload.get("text", ""),
                reply_markup=payload.get("reply_markup"),
                parse_mode=payload.get("parse_mode"),
            )
            # Stage 9.1: capture TG-side message_id/date for ack-vs-final
            # ordering analysis. См. tomorrow-plan пункт 9.
            tg_msg_id: int | None = None
            tg_date: int | None = None
            result = send_response.get("result") if isinstance(send_response, dict) else None
            if isinstance(result, dict):
                mid = result.get("message_id")
                date_v = result.get("date")
                if isinstance(mid, int):
                    tg_msg_id = mid
                if isinstance(date_v, int):
                    tg_date = date_v

            # Feature-specific post-delivery (e.g. EDS photo sending)
            if row.feature_key:
                hook = get_feature_registry().get_delivery_hook(row.feature_key)
                if hook is not None:
                    try:
                        await hook(
                            session=self.session,
                            telegram_client=tg_client,
                            outbox_row=row,
                            payload=payload,
                        )
                    except Exception:
                        logger.warning(
                            "outbox delivery: delivery hook failed for %s (feature=%s)",
                            row.id,
                            row.feature_key,
                            exc_info=True,
                        )
            row.status = "sent"
            self._emit_trace(
                trace_payload,
                chat_id=payload.get("chat_id"),
                status="ok",
                tg_message_id=tg_msg_id,
                tg_date=tg_date,
            )
        except TelegramDeliveryError:
            logger.warning("outbox delivery: telegram error on %s, keeping pending", row.id)
            row.status = "pending"
            # Stays pending — worker retries next tick. Trace will be
            # emitted then. Don't emit now or we'd fire the same block
            # again on retry (idempotency is on a fresh context, which
            # the worker reconstructs each time).
        except Exception:
            logger.exception("outbox delivery: unexpected error on %s", row.id)
            row.status = "failed"
            self._emit_trace(
                trace_payload,
                chat_id=payload.get("chat_id"),
                status="failed",
            )
        self.session.commit()

    async def _send_now_max(self, row: OutboxMessage) -> None:
        """MAX channel send (Phase 6).

        Payload contract (mirror TG):
        - ``payload.chat_id`` — MAX chat_id (mandatory)
        - ``payload.text`` — message text
        - ``payload.format`` — optional ``markdown``/``html``
        - ``payload.attachments`` — optional list (inline keyboard etc.)

        Trace flow тот же как у TG: ``_emit_trace`` вызывается с
        chat_id и status. Stage 9.1 message_id/date capture — после probe
        Phase 0 знаем что MAX отдаёт ``body.mid`` per message; формат
        capture добавим в follow-up если нужно.
        """
        if self.max is None:
            # 2026-05-05 incident: prod ran без max_client (job_runner
            # forgot to wire его) и rows для МАКС'а тихо помечались
            # 'sent' — Boris думал bot отвечает, по факту message в
            # МАКС не уходил. Чтобы такое больше не повторялось:
            # отличаем dev/test (env без SREDA_MAX_BOT_TOKEN — OK,
            # тестам нужен dev-stub) от prod-misconfig (token задан
            # но клиент не дошёл до worker'а — alert).
            from sreda.config.settings import get_settings as _get_settings
            from sreda.services.admin_alerts import (
                alert_admin_async as _alert,
            )
            if _get_settings().max_bot_token:
                logger.critical(
                    "max outbox %s: max_client=None но "
                    "SREDA_MAX_BOT_TOKEN set — misconfiguration; "
                    "row помечена failed чтобы не врать о delivery",
                    row.id,
                )
                try:
                    await _alert(
                        f"🔴 MAX outbox misconfig: max_client=None в "
                        f"worker'е, row={row.id} помечена failed. "
                        f"Проверь job_runner.py wiring."
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("admin alert failed", exc_info=True)
                row.status = "failed"
            else:
                # Dev/test path — token не настроен в env, mark sent
                # для unit-тестов
                row.status = "sent"
            self.session.commit()
            return

        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            logger.exception("max outbox: bad payload_json for %s", row.id)
            row.status = "failed"
            self.session.commit()
            return

        trace_payload = payload.pop("_trace", None)
        ack_edit_message_id = payload.pop("_ack_edit_message_id", None)
        ack_final_already_visible = bool(
            payload.pop("_ack_final_already_visible", False)
        )
        chat_id = payload.get("chat_id")
        text = payload.get("text", "")

        # Reviewer CRITICAL-1: guard against missing chat_id. Без recipient
        # MAX API всё равно бросит 4xx, но лучше не делать сетевой вызов,
        # пометить failed и оставить trail в логах для диагностики.
        if chat_id is None:
            logger.error(
                "max outbox %s: missing chat_id в payload — "
                "outbound маршрутизация сломана",
                row.id,
            )
            row.status = "failed"
            self._emit_trace(
                trace_payload, chat_id=None, status="failed_no_recipient",
            )
            self.session.commit()
            return

        # Convert TG-style reply_markup → MAX attachments inline_keyboard.
        # Producers пишут TG schema (исторически), но MAX API ожидает
        # другую structure. Если payload.attachments уже set'нут MAX-native
        # — используем его; иначе конвертим reply_markup. Если reply_markup
        # пустой — text-only message (без кнопок).
        from sreda.integrations.max.client import (
            render_max_inline_keyboard_attachment,
        )
        attachments = payload.get("attachments")
        if not attachments:
            attachments = render_max_inline_keyboard_attachment(
                payload.get("reply_markup"),
            )

        try:
            if ack_edit_message_id:
                if ack_final_already_visible and not attachments:
                    row.status = "sent"
                    self._emit_trace(
                        trace_payload,
                        chat_id=chat_id,
                        status="sent_ack_already_visible",
                    )
                    self.session.commit()
                    return
                edit_sent = False
                for attempt in range(2):
                    try:
                        await self.max.edit_message(
                            str(ack_edit_message_id),
                            text=text,
                            attachments=attachments,
                        )
                        edit_sent = True
                        break
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "max outbox: ack edit failed on %s attempt=%d",
                            row.id,
                            attempt + 1,
                            exc_info=True,
                        )
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                if not edit_sent:
                    await self.max.send_message(
                        recipient={"chat_id": chat_id},
                        text=text,
                        format=payload.get("format"),
                        attachments=attachments,
                    )
                    try:
                        await self.max.delete_message(str(ack_edit_message_id))
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "max outbox: fallback ack delete failed on %s mid=%s",
                            row.id,
                            ack_edit_message_id,
                            exc_info=True,
                        )
            else:
                await self.max.send_message(
                    recipient={"chat_id": chat_id},
                    text=text,
                    format=payload.get("format"),
                    attachments=attachments,
                )
            row.status = "sent"
            self._emit_trace(
                trace_payload, chat_id=chat_id, status="ok",
            )
        except MaxDeliveryError:
            logger.warning(
                "max outbox: delivery error on %s, keeping pending", row.id,
            )
            row.status = "pending"
        except Exception:
            logger.exception("max outbox: unexpected error on %s", row.id)
            row.status = "failed"
            self._emit_trace(
                trace_payload, chat_id=chat_id, status="failed",
            )
        self.session.commit()

    @staticmethod
    def _emit_trace(
        trace_payload: dict | None,
        *,
        chat_id: object,
        status: str,
        tg_message_id: int | None = None,
        tg_date: int | None = None,
    ) -> None:
        """Render the accumulated end-to-end trace block. No-op if the
        outbox row wasn't tagged with a trace (reminders / EDS
        notifications / other non-conversation rows).

        ``tg_message_id`` / ``tg_date`` (Stage 9.1, см. tomorrow-plan
        пункт 9) — Telegram-side ids возвращённые ``sendMessage``.
        Лежат в final_meta трейса; ``ack.sent`` событие так же содержит
        свой ``tg_message_id`` через ``trace.step``. Сравнение даёт
        диагностику «ack приходит после реплая».
        """
        if not trace_payload:
            return
        try:
            ctx = trace.deserialize_from_outbox(trace_payload)
            final_meta: dict = {
                "chat": str(chat_id) if chat_id is not None else None,
                "status": status,
            }
            if tg_message_id is not None:
                final_meta["tg_message_id"] = tg_message_id
            if tg_date is not None:
                final_meta["tg_date"] = tg_date
            trace.emit_block(
                ctx,
                final_event_name="outbox.delivered",
                final_meta=final_meta,
            )
        except Exception:  # noqa: BLE001 — trace must never kill delivery
            logger.exception("outbox delivery: failed to emit trace block")
