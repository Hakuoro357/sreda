"""Channel-agnostic Telegram inbound handler.

Single durable-ingest entrypoint shared by both transport modes:

* The webhook route (``/webhooks/telegram/{bot_key}``) calls
  ``await handle_telegram_update(payload, bot_key=...)`` synchronously
  before returning 202. If we returned 202 first and persisted later,
  Telegram would consider the update delivered and never retry — we'd
  lose updates on any crash between ack and persist. Webhook timeout
  budget for the durable-ingest path is < 1 second (DB upsert + INSERT
  + ``asyncio.create_task``); LLM/voice/outbox work is offloaded to the
  detached background task.

* The long-poller (``sreda.workers.telegram_long_poll``) calls the same
  function for each update returned by ``getUpdates`` and only advances
  its in-DB offset after this function has returned (durable ingest →
  offset advance). If the poller crashes between the two commits, the
  next ``getUpdates`` re-delivers the same update; idempotency by
  ``external_update_id`` (= ``persist_telegram_inbound_event.is_duplicate``)
  swallows the dupe and processing continues from there.

Lifecycle of ``inbound_messages.processing_status`` is owned here:

  ingested → processing_started → processed   (approved-user happy path)
                ↘ exception                    (left unchanged → monitor catches)
  ignored                                      (pending tenant / unsupported / service command)

Duplicate update_id is a true no-op: we do not create or update a row;
the existing row keeps whatever status it already had.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from sreda.config.bot_registry import TelegramBotRegistry, telegram_client_for
from sreda.config.settings import get_settings
from sreda.db.models.core import InboundMessage, Tenant
from sreda.db.session import get_session_factory
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.services import trace
from sreda.services.ack_messages import pick_ack
from sreda.services.ack_progress import TelegramAckProgressController
from sreda.services.inbound_messages import persist_telegram_inbound_event
from sreda.services.onboarding import ensure_telegram_user_bundle
from sreda.services.telegram_bot import handle_telegram_interaction

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)


# 2026-04-30: per-tenant async lock — выделено в ``services.tenant_lock``
# 2026-05-05 (review R1: cross-module private import был fragile).
# Re-export для backward compat — старые тесты импортируют ``_TENANT_LOCKS``
# / ``_get_tenant_lock`` отсюда.
from sreda.services.tenant_lock import (  # noqa: E402,F401
    _TENANT_LOCKS,
    get_tenant_lock as _get_tenant_lock,
)

# #133: локальный шов для функционального харнеса — патчится
# именно этот символ модуля (подмена asyncio.create_task была бы
# глобальной для всего процесса).
_create_task = asyncio.create_task


async def _fire_and_forget_ack(
    client: TelegramClient, chat_id: str, text: str,
) -> int | None:
    """Send a one-word ack reply concurrently with main turn processing.

    Returns Telegram message_id of the ack (None on failure). Used to
    later call ``delete_message`` after delivering the real reply —
    clean-chat UX (one message per turn instead of two).
    """
    with trace.step("ack.sent", phrase=text) as meta:
        try:
            response = await client.send_message(chat_id=chat_id, text=text)
            meta["status"] = "ok"
            result = response.get("result") or {}
            mid = result.get("message_id")
            # Stage 9.1 (см. tomorrow-plan): TG-side identifiers для
            # диагностики «ack приходит после реплая». Если ack.message_id
            # < final.message_id — Telegram client-side sync, сетью не
            # лечится, нужен placeholder + editMessageText (9.2).
            # Если ack.message_id > final.message_id — реальный transport
            # HOL, нужен WireGuard / Go-egress (9.3).
            if isinstance(mid, int):
                meta["tg_message_id"] = mid
            tg_date = result.get("date")
            if isinstance(tg_date, int):
                meta["tg_date"] = tg_date
            return int(mid) if isinstance(mid, int) else None
        except TelegramDeliveryError as exc:
            logger.warning("ack delivery failed: %s", exc)
            meta["status"] = "failed"
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("ack task crashed: %s", exc)
            meta["status"] = "crashed"
            return None


async def _delete_ack_after_reply(
    client: TelegramClient, chat_id: str, message_id: int,
) -> None:
    """Best-effort delete of the ack message after the real reply lands."""
    try:
        await client.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramDeliveryError as exc:
        logger.debug(
            "ack delete failed status=%s msg_id=%s",
            exc.status_code, message_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("ack delete crashed", exc_info=True)


def _set_processing_status(
    session: Session, inbound_message_id: str, new_status: str,
) -> None:
    """Update processing_status on inbound_messages row + commit.

    Quiet on missing row (caller passed a stale id) — log and continue;
    the lifecycle is observability sugar, not correctness-critical.
    """
    inbound = session.get(InboundMessage, inbound_message_id)
    if inbound is None:
        logger.warning(
            "processing_status update skipped: inbound %s not found",
            inbound_message_id,
        )
        return
    inbound.processing_status = new_status
    session.commit()


def _voice_to_react_gate(
    message_type: str, *, is_new_user: bool, tenant_id, enabled_tenants,
) -> bool:
    """#166: голос флагованного тенанта транскрибируем ДО react-гейта.
    Чистое решение без побочек (тестируемо). Не-голос / новичок / не-флагованный → False."""
    return (
        message_type == "voice"
        and not is_new_user
        and tenant_id in enabled_tenants
    )


def _clean_transcript(message) -> str | None:
    """Нормализованный непустой текст расшифровки или None. Codex high R1:
    пробельная расшифровка («   ») НЕ должна уходить в ReAct как текст."""
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    text = text.strip() if isinstance(text, str) else ""
    return text or None


async def _process_approved_turn(
    *,
    bot_key: str,
    payload: dict,
    onboarding,
    inbound_message_id: str,
) -> None:
    """Run full approved-user processing detached from caller.

    2026-04-30 (incident voice-webhook timeout): voice + multi-iter LLM
    runs regularly take 16-20 seconds, exceeding Telegram's webhook
    timeout (~10s). Webhook handler returns 202 ASAP after persist; this
    function runs as a detached task with its own DB session.

    Per-tenant lock prevents 4-voice-burst write contention: multiple
    concurrent turns from the same user serialize.
    """
    tenant_lock = _get_tenant_lock(onboarding.tenant_id)
    if tenant_lock.locked():
        logger.info(
            "tenant turn queued behind in-flight: tenant=%s inbound=%s",
            onboarding.tenant_id, inbound_message_id,
        )
    async with tenant_lock:
        await _process_approved_turn_locked(
            bot_key=bot_key,
            payload=payload,
            onboarding=onboarding,
            inbound_message_id=inbound_message_id,
        )


async def _process_approved_turn_locked(
    *,
    bot_key: str,
    payload: dict,
    onboarding,
    inbound_message_id: str,
) -> None:
    """Inner — runs under the per-tenant lock.

    Updates ``processing_status`` lifecycle:
      - ``processing_started`` at first step (so monitor distinguishes
        «turn was attempted» from «turn never started»)
      - ``processed`` after the orchestrated turn completes successfully
      - on exception: status is left as-is so the
        ``unprocessed_inbound`` monitor probe can pick it up
    """
    SessionLocal = get_session_factory()
    bg_session: Session = SessionLocal()
    # #187 Phase 2b БАРЬЕР: держим session-scoped advisory-lock на тенанта весь
    # ход (взять первым делом, отпустить в finally). soft_delete_tenant берёт ТОТ
    # ЖЕ лок ДО флага+drain → ждёт этот in-flight ход и дренит после. На SQLite
    # (тесты) — no-op. Дополняет in-process tenant_lock (тот сериализует ходы в
    # одном процессе; advisory — cross-process против админ-delete). ExitStack —
    # симметричный unlock на любом пути выхода без переиндентации тела хода.
    from contextlib import ExitStack

    from sreda.services.tenant_lifecycle import tenant_advisory_lock
    _barrier = ExitStack()
    try:
        # #187 R2 MAJOR: enter_context ВНУТРИ try (зеркало MAX
        # ``_process_approved_turn``) — если захват advisory-лока бросит,
        # ``finally`` ниже всё равно закроет bg_session (раньше enter_context был
        # ДО try → исключение из захвата лока утекало bg_session).
        _barrier.enter_context(
            tenant_advisory_lock(bg_session, onboarding.tenant_id)
        )
        # #187 дверь #6 (A3 re-check ПОД локом): ingress-гейт (handle_telegram_update)
        # проверил активность ДО спавна хода, но между гейтом и захватом лока админ
        # мог soft_delete'нуть тенант. Закрываем delete-first гонку: soft_delete берёт
        # ТОТ ЖЕ лок и КОММИТИТ флаг под ним → если он успел ПЕРВЫМ, этот ход (взявший
        # лок после) обязан НЕ коммитить доменные мутации (drain их уже не застанет).
        # Лок на отдельном соединении; bg_session тут делает свежий SELECT → видит
        # durable deleted_at. На SQLite лок no-op, но re-check всё равно читает флаг.
        from sreda.services.tenant_lifecycle import is_tenant_active
        if not is_tenant_active(bg_session, onboarding.tenant_id):
            logger.info(
                "turn aborted: tenant %s soft-deleted mid-turn (re-check под advisory-локом)",
                onboarding.tenant_id,
            )
            # Codex MINOR: пометить inbound терминально ('ignored'), иначе монитор
            # unprocessed_inbound (NOT IN processed/ignored) ложно сочтёт корректно
            # отброшенный ход зависшим. Намеренный скип удалённого тенанта = ignored.
            _set_processing_status(bg_session, inbound_message_id, "ignored")
            return
        _set_processing_status(
            bg_session, inbound_message_id, "processing_started",
        )

        registry = TelegramBotRegistry.from_settings(get_settings())
        telegram_client = telegram_client_for(bot_key, registry)
        trace_ctx = trace.start_trace(
            user_id=onboarding.user_id,
            tenant_id=onboarding.tenant_id,
            channel="telegram",
        )
        message = payload.get("message") if isinstance(payload, dict) else None
        message_type = "unknown"
        if isinstance(message, dict):
            if isinstance(message.get("voice"), dict):
                voice = message["voice"]
                message_type = "voice"
                trace.record(
                    "webhook.received",
                    type="voice",
                    voice_duration_s=voice.get("duration"),
                )
            elif message.get("text"):
                message_type = "text"
                trace.record("webhook.received", type="text")
            else:
                message_type = "other"
                trace.record("webhook.received", type="other")
        elif isinstance(payload, dict) and payload.get("callback_query"):
            message_type = "callback"
            trace.record("webhook.received", type="callback")
        else:
            trace.record("webhook.received", type="unknown")

        # #166 B: тап по кнопке [Да]/[Нет] react-подтверждения → resume паузы того же треда.
        # answerCallbackQuery ПЕРВЫМ (идемпотентность кнопок); само действие идемпотентно в
        # цикле (operation_id + guard статуса) → повторный тап после resume = безвредный no-op.
        _cb = payload.get("callback_query") if isinstance(payload, dict) else None
        _cb_data = str(_cb.get("data") or "") if isinstance(_cb, dict) else ""
        if (_cb_data.startswith(("react:yes", "react:no"))
                and not onboarding.is_new_user
                and onboarding.tenant_id in get_settings().react_loop_enabled_tenants):
            from sreda.runtime import react_loop
            from sreda.services.llm import get_chat_llm
            _cb_id = str(_cb.get("id") or "")
            if _cb_id:
                try:
                    await telegram_client.answer_callback_query(_cb_id, text="")
                except Exception:  # noqa: BLE001
                    pass
            _s2 = get_settings()
            _prov2 = react_loop.react_provider(onboarding.tenant_id)  # #184 «Оса» per-tenant
            _llm2 = get_chat_llm(provider=_prov2, settings=_s2)
            # resume_only + expected_confirm_id: тап возобновляет ТОЛЬКО ту confirm-паузу,
            # к которой кнопка была привязана (id из callback_data). Устаревший/повторный/
            # чужой тап → пустой ответ (no-op), свежий ход не стартуем (R3 Codex MAJOR A/B).
            _reply2 = await react_loop.handle_turn(
                session=bg_session, tenant_id=onboarding.tenant_id,
                user_id=onboarding.user_id,
                thread_id=f"react:{onboarding.tenant_id}:{onboarding.chat_id}",
                llm=_llm2, user_text=react_loop.confirm_resume_text(_cb_data) or "",
                inbound_message_id=inbound_message_id, channel="telegram",
                resume_only=True,
                expected_confirm_id=react_loop.confirm_callback_id(_cb_data),
                provider_key=_prov2,  # #175 учёт расхода + #184 «Оса»
                fallback_llm=react_loop.react_fallback_llm(_prov2))  # #184 Оса-fallback
            trace.record("react_loop.resumed", chars=len(_reply2 or ""),
                         channel="telegram", noop=(not _reply2))
            if _reply2:  # непустой → пауза возобновлена (пустой = устаревший/чужой тап → no-op)
                # цепочка-confirm: следующий вопрос снова с [Да][Нет] (с id новой паузы)
                _cid2 = getattr(_reply2, "confirm_id", "")
                _kb2 = ({"inline_keyboard": [[
                    {"text": "Да", "callback_data": react_loop.confirm_callback_data("yes", _cid2)},
                    {"text": "Нет", "callback_data": react_loop.confirm_callback_data("no", _cid2)}]]}
                    if getattr(_reply2, "awaiting_confirm", False) else None)
                # правим ИСХОДНОЕ сообщение: результат + СНИМАЕМ старые кнопки. Telegram
                # editMessageText без reply_markup НЕ убирает клавиатуру → передаём пустой
                # inline_keyboard явно (субагент R2 MINOR), либо новые [Да][Нет] при цепочке.
                _kb_edit = _kb2 if _kb2 is not None else {"inline_keyboard": []}
                _orig_mid = (_cb.get("message") or {}).get("message_id")
                _done = False
                if _orig_mid is not None:
                    try:
                        await telegram_client.edit_message_text(
                            chat_id=str(onboarding.chat_id), message_id=_orig_mid,
                            text=_reply2, reply_markup=_kb_edit)
                        _done = True
                    except Exception:  # noqa: BLE001
                        _done = False
                if not _done:
                    try:
                        await telegram_client.send_message(
                            chat_id=str(onboarding.chat_id), text=_reply2, reply_markup=_kb2)
                    except Exception:  # noqa: BLE001
                        pass
            if trace_ctx is not None:
                trace.emit_block(trace_ctx)
            _set_processing_status(bg_session, inbound_message_id, "processed")
            return

        # #166 голос на ReAct: для флагованного тенанта транскрибируем голос ДО
        # гейта ТОЙ ЖЕ _maybe_transcribe_voice, что и старый путь (она держит
        # доступ/квоты/ошибки и инжектит расшифровку в message["text"]). Дальше
        # ведём как обычный текстовый react-ход. Зеркало MAX-пути
        # (_maybe_transcribe_max_voice до его гейта). Остальные тенанты не тронуты.
        if _voice_to_react_gate(
            message_type, is_new_user=onboarding.is_new_user,
            tenant_id=onboarding.tenant_id,
            enabled_tenants=get_settings().react_loop_enabled_tenants,
        ):
            from sreda.services.telegram_bot import _maybe_transcribe_voice
            payload = await _maybe_transcribe_voice(
                payload, session=bg_session,
                telegram_client=telegram_client, onboarding=onboarding,
            )
            if payload is None:
                # доступ/квота/STT-ошибка уже сообщены пользователю — ход завершён.
                # Эмитим trace ДО выхода (voice.download/transcribe уже записаны) —
                # иначе срез голосовых отказов теряет диагностику (R1 MAJOR Codex+
                # субагент). Статус ignored — паритет с MAX-путём (max_inbound) для
                # того же класса событий.
                if trace_ctx is not None:
                    trace.emit_block(trace_ctx)
                _set_processing_status(bg_session, inbound_message_id, "ignored")
                return
            message = payload.get("message") if isinstance(payload, dict) else None
            _vtext = _clean_transcript(message)
            if _vtext is None:
                # пустая/пробельная расшифровка: НЕ уводим в старый путь (квота уже
                # списана), мягко просим повторить и завершаем ход.
                try:
                    await telegram_client.send_message(
                        chat_id=str(onboarding.chat_id),
                        text="Не расслышала, повтори, пожалуйста.",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("empty-voice reply failed: %s", type(exc).__name__)
                if trace_ctx is not None:
                    trace.emit_block(trace_ctx)
                _set_processing_status(bg_session, inbound_message_id, "processed")
                return
            message["text"] = _vtext  # нормализованная расшифровка → react-ход
            message_type = "text"  # обычный react-гейт ниже

        # #66 ГЕЙТ решаем ДО ack: для react-ходов индикатор «печатает» НЕ создаём
        # (react отвечает сам через send_message, мимо outbox — иначе ack завис бы
        # / возможны двойные сообщения; Codex MAJOR).
        _react_text = message.get("text") if isinstance(message, dict) else None
        _use_react = (
            message_type == "text"
            and bool(_react_text)
            and not onboarding.is_new_user
            and onboarding.tenant_id in get_settings().react_loop_enabled_tenants
        )

        ack_task: asyncio.Task | None = None
        ack_progress_controller = None
        if (
            message_type in ("text", "voice")
            and not onboarding.is_new_user
            and not _use_react
        ):
            ack_text = pick_ack()
            ack_task = _create_task(
                _fire_and_forget_ack(
                    telegram_client, onboarding.chat_id, ack_text,
                ),
                name=f"ack:{onboarding.chat_id}",
            )
            if get_settings().ack_edit_telegram_enabled:
                ack_progress_controller = TelegramAckProgressController(
                    telegram_client=telegram_client,
                    chat_id=str(onboarding.chat_id),
                    ack_message_id_future=ack_task,
                    enabled=True,
                )

        try:
            # #66 ГЕЙТ-эксперимент: тенант из react_loop_enabled_tenants + текст →
            # новый LangGraph ReAct+interrupt-цикл (InMemory, single-process
            # поллер). Остальные тенанты/не-текст — прежним путём (нулевой регресс).
            if _use_react:
                from sreda.runtime import react_loop
                from sreda.services.llm import get_chat_llm

                _s = get_settings()
                _prov = react_loop.react_provider(onboarding.tenant_id)  # #184 «Оса» per-tenant
                _llm = get_chat_llm(provider=_prov, settings=_s)
                # ack v3: шлём «Секунду…» и редактируем ЕГО в финальный ответ
                # (одно сообщение, как раньше; напрямую через editMessageText, без
                # outbox → не зависнет). Любой сбой ack — не критичен.
                _ack_mid = None
                try:
                    _ack = await telegram_client.send_message(
                        chat_id=str(onboarding.chat_id), text="Секунду…",
                    )
                    _ack_mid = (_ack or {}).get("result", {}).get("message_id")
                except Exception:  # noqa: BLE001
                    _ack_mid = None
                _reply = await react_loop.handle_turn(
                    session=bg_session,
                    tenant_id=onboarding.tenant_id,
                    user_id=onboarding.user_id,
                    thread_id=f"react:{onboarding.tenant_id}:{onboarding.chat_id}",
                    llm=_llm,
                    user_text=_react_text,
                    inbound_message_id=inbound_message_id,
                    channel="telegram",
                    provider_key=_prov,  # #175 учёт расхода + #184 «Оса»
                    fallback_llm=react_loop.react_fallback_llm(_prov),  # #184 Оса-fallback
                )
                trace.record("react_loop.replied", chars=len(_reply or ""))
                # #166 B: на да/нет-подтверждение вешаем inline-кнопки [Да][Нет] с id
                # КОНКРЕТНОЙ паузы в callback_data (текст «да/нет» тоже работает — кнопки добавка).
                _cid = getattr(_reply, "confirm_id", "")
                _kb = ({"inline_keyboard": [[
                    {"text": "Да", "callback_data": react_loop.confirm_callback_data("yes", _cid)},
                    {"text": "Нет", "callback_data": react_loop.confirm_callback_data("no", _cid)}]]}
                    if getattr(_reply, "awaiting_confirm", False) else None)
                _edited = False
                if _ack_mid is not None:
                    try:
                        await telegram_client.edit_message_text(
                            chat_id=str(onboarding.chat_id),
                            message_id=_ack_mid, text=_reply, reply_markup=_kb,
                        )
                        _edited = True
                    except Exception:  # noqa: BLE001
                        _edited = False
                if not _edited:
                    # сбой edit → убираем «Секунду…», чтобы не висело + не дублировалось
                    if _ack_mid is not None:
                        try:
                            await telegram_client.delete_message(
                                chat_id=str(onboarding.chat_id), message_id=_ack_mid,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    await telegram_client.send_message(
                        chat_id=str(onboarding.chat_id), text=_reply, reply_markup=_kb,
                    )
            else:
                await handle_telegram_interaction(
                    bg_session,
                    bot_key=bot_key,
                    payload=payload,
                    telegram_client=telegram_client,
                    onboarding=onboarding,
                    inbound_message_id=inbound_message_id,
                    ack_progress_controller=ack_progress_controller,
                )
        except TelegramDeliveryError as exc:
            logger.warning(
                "Telegram delivery failed during turn processing: %s", exc,
            )
        finally:
            ack_message_id: int | None = None
            if ack_task is not None:
                if not ack_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(ack_task), timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "ack task still pending after main turn — abandoning",
                        )
                if ack_task.done() and not ack_task.cancelled():
                    try:
                        ack_message_id = ack_task.result()
                    except Exception:  # noqa: BLE001
                        ack_message_id = None

            reply_delivered_inline = (
                trace_ctx is not None and any(
                    e.step == "outbox.delivered" for e in trace_ctx.events
                )
            )
            if (
                ack_message_id is not None
                and ack_progress_controller is None
                and reply_delivered_inline
                and onboarding.chat_id is not None
            ):
                _create_task(
                    _delete_ack_after_reply(
                        telegram_client,
                        str(onboarding.chat_id),
                        ack_message_id,
                    ),
                    name=f"ack_del:{onboarding.chat_id}",
                )

            if trace_ctx is not None and not any(
                e.step == "outbox.enqueued" for e in trace_ctx.events
            ):
                trace.emit_block(trace_ctx)

        # Reached only on the success path (no re-raise above). Mark
        # processed so the unprocessed_inbound monitor stays quiet.
        _set_processing_status(bg_session, inbound_message_id, "processed")
    except Exception:  # noqa: BLE001
        logger.exception("background turn processing crashed")
    finally:
        # #187 Phase 2b: отпустить advisory-lock ДО close() сессии. Лок session-
        # scoped — закрытие соединения тоже снимет его, но явный unlock держит
        # симметрию с soft_delete_tenant и не зависит от порядка GC.
        _barrier.close()
        bg_session.close()


async def _handle_pending_tenant(
    session: Session,
    *,
    bot_key: str,
    payload: dict,
    onboarding,
) -> None:
    """Pending-tenant scripted-bot reply.

    The tenant has not been approved in /admin/users yet. Instead of
    silent-dropping, we run the scripted demo-bot (``pending_bot``) and
    answer with text + inline keyboard. After this returns the inbound
    is considered done (status = ``ignored``) — no LLM, no outbox, no
    background work.
    """
    settings = get_settings()
    if not (settings.telegram_bot_token and onboarding.chat_id):
        logger.info(
            "pending tenant %s — no bot token/chat_id, drop (update_id=%s)",
            onboarding.tenant_id, payload.get("update_id"),
        )
        return

    from sreda.services import pending_bot
    from sreda.services.pending_bot import (
        _BRANCHES as _PB_BRANCHES,  # noqa: F401  (lightweight import)
    )

    message = payload.get("message") if isinstance(payload, dict) else None
    callback_query = (
        payload.get("callback_query")
        if isinstance(payload, dict) else None
    )
    input_text: str | None = None
    is_callback = False
    if isinstance(callback_query, dict):
        cb_data = str(callback_query.get("data") or "")
        if pending_bot.is_pending_callback(cb_data):
            input_text = cb_data
            is_callback = True
        else:
            input_text = None
    elif isinstance(message, dict) and message.get("text"):
        input_text = str(message.get("text"))

    reply = pending_bot.match(input_text, is_callback=is_callback)
    registry = TelegramBotRegistry.from_settings(settings)
    pending_client = telegram_client_for(bot_key, registry)

    cb_id = (
        str(callback_query.get("id") or "")
        if isinstance(callback_query, dict) else ""
    )
    if cb_id:
        try:
            await pending_client.answer_callback_query(cb_id, text="")
        except TelegramDeliveryError:
            pass

    current_branch = "intro"
    if is_callback and input_text:
        raw = input_text.removeprefix("pb:").strip()
        if raw in _PB_BRANCHES:
            current_branch = raw
    keyboard = pending_bot.build_navigation_keyboard(current_branch)

    cb_message = (
        callback_query.get("message")
        if isinstance(callback_query, dict) else None
    )
    cb_msg_id = (
        cb_message.get("message_id")
        if isinstance(cb_message, dict) else None
    )

    edited = False
    if is_callback and cb_msg_id is not None:
        try:
            await pending_client.edit_message_text(
                chat_id=str(onboarding.chat_id),
                message_id=int(cb_msg_id),
                text=reply.text,
                reply_markup=keyboard,
            )
            edited = True
        except TelegramDeliveryError as exc:
            if exc.status_code == 400 and "not modified" in (str(exc) or "").lower():
                edited = True
            else:
                logger.info(
                    "pending: editMessageText failed status=%s — "
                    "fallback to send_message",
                    exc.status_code,
                )

    if not edited:
        try:
            await pending_client.send_message(
                chat_id=onboarding.chat_id,
                text=reply.text,
                reply_markup=keyboard,
            )
        except TelegramDeliveryError as exc:
            logger.warning(
                "pending_bot reply failed tenant=%s: %s",
                onboarding.tenant_id, exc,
            )


async def handle_telegram_update(
    payload: dict,
    *,
    bot_key: str = "sreda",
    background_tasks: "BackgroundTasks | None" = None,
) -> str:
    """Durable ingest of one Telegram update.

    Returns ``inbound_message_id`` once ``persist_telegram_inbound_event``
    has committed (the same id is also returned for the duplicate
    short-circuit path so the webhook can still respond with a stable
    request id).

    Idempotent on duplicate ``update_id``: a second call for the same
    update is a true no-op (no row created, no status changed).

    For approved tenants, schedules ``_process_approved_turn`` to run
    detached: when ``background_tasks`` is provided (webhook context)
    we use FastAPI's ``BackgroundTasks`` so the turn runs in the
    request lifecycle and TestClient await semantics work as expected;
    otherwise (long-poller context) we use ``asyncio.create_task`` and
    rely on the long-running event loop. Either way the durable-ingest
    path itself stays inside the webhook ~1s timeout budget.

    For pending tenants, runs the scripted ``pending_bot`` reply
    inline, then marks the inbound as ``ignored``.
    """
    settings = get_settings()
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        # #187 Phase 1 — soft-delete ingress guard (resolve → gate → provision).
        # Чистый резолв account→tenant (read-only, БЕЗ side-effects) ДО ensure/
        # welcome/обработки. Удалённый существующий тенант → ТИХИЙ no-op (нет
        # ensure, нет welcome, нет LLM). Новый юзер (резолв=None) → гард
        # пропускается, идёт обычная регистрация. is_tenant_active стоит ПЕРВЫМ
        # — до EntitlementGate/suspended (precedence: deleted > suspended, §13).
        # RuntimeError из резолва (соль не сконфигурена) → трактуем как «не
        # резолвится», НЕ как «удалён» (A16).
        from sreda.services.onboarding import _extract_chat_id
        from sreda.services.tenant_lifecycle import is_tenant_active
        from sreda.services.telegram_auth import resolve_tenant_from_telegram_id

        _guard_chat_id = _extract_chat_id(payload)
        if _guard_chat_id is not None:
            try:
                _resolved = resolve_tenant_from_telegram_id(session, str(_guard_chat_id))
            except RuntimeError:
                _resolved = None
            if _resolved is not None and not is_tenant_active(session, _resolved[0]):
                logger.info(
                    "telegram inbound: tenant %s is soft-deleted — silent drop "
                    "(no ensure/welcome/LLM)",
                    _resolved[0],
                )
                return None

        # Phase 2C: catch SignupBlocked from abuse guard inside
        # ensure_telegram_user_bundle. Send UPGRADE_COPY[reason] to user
        # and drop update (no inbound persisted, no LLM call).
        from sreda.services.signup_abuse import SignupBlocked
        from sreda.services.upgrade_copy import UPGRADE_COPY
        try:
            onboarding = ensure_telegram_user_bundle(session, payload, bot_key=bot_key)
        except SignupBlocked as exc:
            logger.info(
                "telegram inbound: signup blocked reason=%s — drop update",
                exc.reason,
            )
            chat_id = _extract_chat_id(payload)
            if chat_id and settings.telegram_bot_token:
                try:
                    _registry = TelegramBotRegistry.from_settings(settings)
                    client = telegram_client_for(bot_key, _registry)
                    await client.send_message(
                        chat_id=chat_id,
                        text=UPGRADE_COPY.get(exc.reason, UPGRADE_COPY["signups_closed"]),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("signup-blocked notify failed", exc_info=True)
            return None

        # Phase 2 fix 2026-05-08 (Codex MAJOR hardening): welcome
        # отправляется на основе `is_welcome_sent` флага в БД, не
        # `is_new_user`. Если HTTP-send падает, флаг остаётся False —
        # следующий inbound заретраит. Mark-sent ТОЛЬКО на успешный
        # send, через `mark_welcome_sent`.
        welcome_just_sent = False
        if (
            onboarding.tenant_id
            and onboarding.user_id
            and onboarding.chat_id
            and settings.telegram_bot_token
        ):
            from sreda.services.onboarding import (
                build_post_approve_keyboard_tg,
                build_post_approve_message,
                is_welcome_sent,
                mark_welcome_sent,
            )
            if not is_welcome_sent(session, onboarding.tenant_id, onboarding.user_id):
                try:
                    _registry = TelegramBotRegistry.from_settings(settings)
                    client = telegram_client_for(bot_key, _registry)
                    await client.send_message(
                        chat_id=onboarding.chat_id,
                        text=build_post_approve_message(),
                        reply_markup=build_post_approve_keyboard_tg(),
                    )
                    mark_welcome_sent(
                        session, onboarding.tenant_id, onboarding.user_id,
                    )
                    welcome_just_sent = True
                    logger.info(
                        "telegram inbound: post-approve welcome sent tenant=%s",
                        onboarding.tenant_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "telegram post-approve welcome failed for tenant=%s "
                        "(retry on next inbound)",
                        onboarding.tenant_id, exc_info=True,
                    )

        result = persist_telegram_inbound_event(
            session, bot_key=bot_key, payload=payload,
        )
        # persist_telegram_inbound_event commits internally — ingest is now
        # durable in the DB and the long-poller may safely advance offset.

        if result.is_duplicate:
            logger.info(
                "telegram inbound: duplicate update_id %s for bot %s — no-op",
                payload.get("update_id"), bot_key,
            )
            return result.inbound_message_id

        # 2026-05-09 fix (Boris feedback): если welcome ТОЛЬКО ЧТО отправлен
        # этой inbound-message — НЕ передаём её дальше в chat handler.
        # Иначе юзер получает double-reply на /start: welcome + LLM reply.
        # Welcome consumes the inbound; следующее сообщение юзера пойдёт
        # нормально в chat. Применяется ко ВСЕМ inbound types.
        if welcome_just_sent:
            logger.info(
                "telegram inbound: welcome consumed inbound — skip chat "
                "dispatch tenant=%s inbound_id=%s",
                onboarding.tenant_id, result.inbound_message_id,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        # Phase 2 (Codex CRITICAL fix 2026-05-07): EntitlementGate enforced
        # at handler entry — было: gate.check() вызывался в downstream
        # paths (runtime/voice/tools), но allowed=False не блокировался,
        # suspended tenants получали полный доступ. Теперь fail-closed
        # primary point: inbound persisted (audit), gate проверяется,
        # если blocked → отправляем UPGRADE_COPY и помечаем inbound
        # как ignored. Allowed tenants проходят далее без изменений.
        from sreda.services.entitlement_gate import EntitlementGate
        _gate = EntitlementGate(session).check(onboarding.tenant_id)
        if not _gate.allowed:
            logger.info(
                "telegram inbound: entitlement gate blocked tenant=%s "
                "reason=%s — drop turn, mark ignored",
                onboarding.tenant_id, _gate.reason,
            )
            if onboarding.chat_id and settings.telegram_bot_token:
                try:
                    _registry = TelegramBotRegistry.from_settings(settings)
                    client = telegram_client_for(bot_key, _registry)
                    await client.send_message(
                        chat_id=onboarding.chat_id,
                        text=UPGRADE_COPY.get(
                            _gate.reason,
                            UPGRADE_COPY["no_active_subscription"],
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "entitlement-blocked notify failed", exc_info=True,
                    )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        tenant = session.get(Tenant, onboarding.tenant_id)
        is_approved = tenant is not None and tenant.approved_at is not None

        if not is_approved:
            await _handle_pending_tenant(
                session, bot_key=bot_key, payload=payload, onboarding=onboarding,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        if not (settings.telegram_bot_token and onboarding.chat_id):
            # No bot token / chat_id → cannot run an approved turn.
            # Mark as ignored so the unprocessed_inbound monitor stays
            # quiet; this is a deliberate skip, not a crash.
            logger.info(
                "approved tenant %s — no bot token/chat_id, drop (update_id=%s)",
                onboarding.tenant_id, payload.get("update_id"),
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        inbound_message_id = result.inbound_message_id

    # Per-tenant feature flag: SREDA_MESSAGE_QUEUE_ENABLED_TENANTS lets
    # us route a specific tenant through the new `message_jobs` queue
    # without disturbing everyone else. Default empty → universal
    # inline path (current behaviour).
    from sreda.workers.message_queue import (
        DuplicateMessageJob,
        derive_thread_key,
        enqueue_message,
        is_queue_enabled_for,
    )

    if is_queue_enabled_for(onboarding.tenant_id):
        # Queue path — worker (job_runner tick) picks the job up,
        # re-derives onboarding via ensure_telegram_user_bundle, and
        # invokes _process_approved_turn from its own session.
        external_update_id = str(payload.get("update_id", ""))
        thread_key = derive_thread_key(
            tenant_id=onboarding.tenant_id,
            channel="telegram",
            external_chat_id=str(onboarding.chat_id),
        )
        enqueued_ok = False
        try:
            with SessionLocal() as session:
                with session.begin():
                    enqueue_message(
                        session,
                        tenant_id=onboarding.tenant_id,
                        thread_id=thread_key,
                        channel="telegram",
                        external_update_id=external_update_id,
                        message_payload={
                            "kind": "telegram_inbound",
                            "payload": payload,
                            "bot_key": bot_key,
                            "inbound_message_id": inbound_message_id,
                        },
                    )
            enqueued_ok = True
        except DuplicateMessageJob:
            # Cross-channel idempotency already swallowed by the
            # inbound dedup logic upstream — this branch is rare
            # (race between two simultaneous deliveries of the
            # same update_id). Treat as ack.
            logger.info(
                "queue: duplicate enqueue for update_id=%s tenant=%s — ack",
                external_update_id, onboarding.tenant_id,
            )
            enqueued_ok = True
        except Exception:  # noqa: BLE001
            # Transient DB hiccup, IntegrityError from a precheck-flush
            # race, or other unexpected enqueue failure. Code-review
            # 2026-05-25 MAJOR-2 added a fallback to the inline path —
            # but Codex follow-up (2026-05-25 MAJOR-3) flagged that as a
            # double-process risk: if INSERT did commit on this side AND
            # we fall to inline, the same inbound gets handled twice.
            # Defensive: re-query (channel, external_update_id) — if a
            # row already exists, the queue ate the message even though
            # we got an exception, so we ack instead of double-handling.
            logger.exception(
                "queue: enqueue failed for update_id=%s tenant=%s — "
                "verifying whether the row landed before fallback",
                external_update_id, onboarding.tenant_id,
            )
            from sqlalchemy import select as _select
            from sreda.db.models import MessageJob as _MessageJob

            with SessionLocal() as session:
                existing = session.execute(
                    _select(_MessageJob).where(
                        _MessageJob.channel == "telegram",
                        _MessageJob.external_update_id == external_update_id,
                    )
                ).scalar_one_or_none()
            if existing is not None:
                logger.warning(
                    "queue: enqueue raised but row IS present "
                    "(id=%s); ack to avoid double-process",
                    existing.id,
                )
                enqueued_ok = True
            # else: row really isn't there — fall through to inline
            # path as a safety net (zero-message-loss contract).
        if enqueued_ok:
            return inbound_message_id
        # else: fall through to inline path below as a safety net

    # Inline path (legacy / default). Detach the heavy turn from the
    # caller. `_process_approved_turn` opens its own DB session (the
    # session above has been closed by the `with` block). It is
    # observed only via the inbound row's processing_status:
    # `processing_started` once it runs, `processed` on success, or
    # unchanged (=> caught by monitor) if it crashes.
    if background_tasks is not None:
        background_tasks.add_task(
            _process_approved_turn,
            bot_key=bot_key,
            payload=payload,
            onboarding=onboarding,
            inbound_message_id=inbound_message_id,
        )
    else:
        _create_task(
            _process_approved_turn(
                bot_key=bot_key,
                payload=payload,
                onboarding=onboarding,
                inbound_message_id=inbound_message_id,
            ),
            name=f"approved_turn:{onboarding.tenant_id}:{inbound_message_id}",
        )
    return inbound_message_id
