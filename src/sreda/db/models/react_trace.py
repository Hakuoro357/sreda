"""#192: durable структурный трейс ХОДА ReAct-цикла (`react_turn_trace`).

Одна строка на ход (вопрос → граф → ответ). Заменяет ВРЕМЕННЫЙ `react_debug_turns` (#185): тот же
контент (зашифрован) + структурные поля (LLM/tool-вызовы, исход, статус жизненного цикла). Цель —
наблюдаемость для измерения #193–#197 и ловли «мерцающих» багов (краши видны как `in_progress`).

Жизненный цикл (status): `in_progress` (старт; остаётся при краше/потере finish-хука) →
`awaiting_confirm` (confirm-пауза, ждёт resume) → `done` (терминал; handled-ошибки тоже `done`).
Upsert по (tenant_id, coalesce(user_id,''), turn_key); все UPDATE conditional (терминал неизменен).

ПД: контент — EncryptedString; аргументы инструментов — ТОЛЬКО HMAC (`services.trace_hash`), сырьё
в таблице НЕ хранится. Аудит совершённых ДЕЙСТВИЙ — НЕ здесь (см. #163, `emit_event`/`audit_outbox`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


class ReactTurnTrace(Base):
    __tablename__ = "react_turn_trace"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # идентичность хода (скоуп = tenant + user; family-ключа нет — домочадцы под tenant/user)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # nullable: tenant-wide
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), default="react")
    turn_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # жизненный цикл: in_progress | awaiting_confirm | done
    status: Mapped[str] = mapped_column(String(16), default="in_progress")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # контент (ПД → шифруем, читать через ORM)
    origin_user_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    reply_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    # наблюдательная структура (НЕ деньги — деньги в skill_ai_executions #175)
    # llm_calls: [{phase, call_index, provider_key, model, latency_ms, retries, fallback_fired,
    #   primary_latency_ms?, fallback_latency_ms?}]  # #401: под-тайминги (опц.: попытка primary /
    #   вызов резерва; latency_ms = ИТОГ; поля опускаются на OFF/happy-path без резерва)
    llm_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # tool_calls: [{name, args_hash(HMAC), ok, result_kind, error_type, latency_ms}]
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # none | pending | confirmed | declined | redirected | expired (#320: брошенная пауза)
    confirm_state: Mapped[str] = mapped_column(String(16), default="none")
    # ok|clarification|tool_error|llm_error|timeout|max_iter|fallback_used|policy_blocked|safe_reply
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    passes: Mapped[int] = mapped_column(Integer, default=0)
    # #221 Ф3b: решение доменного роутера (БЕЗ ПД — только имена доменов/семей + confidence + флаги).
    # Задел под измерение shadow-расхождений (≤5%) и будущую петлю самообучения. NULL в disabled-режиме.
    # {mode, primary_domain, all_domains, classified, confidence, allowed_read, allowed_write,
    #  legacy_active, router_active, compound, cross_intent}
    # ВАЖНО для анализа (R1 субагент): legacy_active = ГИПОТЕТИЧЕСКИЙ legacy-набор семей; в execute
    # фактически загружено router_active (в shadow реально исполняется legacy_active). confidence
    # совмещает детерм. путь ("deterministic"/"not_run_in_shadow") и LLM ("high"/"low") — первичный
    # дискриминатор классификатора = флаг classifier_would_run, не строка confidence.
    routing_decision_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #285 Фаза A: снапшот TurnPolicy хода (shadow-сайдкар, БЕЗ ПД: режимы/домены/бюджеты).
    # NULL = флаг единого пути OFF / строки до Фазы A.
    turn_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #285 Фаза A: исход confirm-паузы "yes"|"no" (петля калибровки словаря: yes на ярусе (б) =
    # зафиксированный промах сигнала). NULL = паузы не было / до-A строки (confirm_state="confirmed"
    # да/нет НЕ различал — дыра, найденная инвентарём Фазы 0 §5.5).
    confirm_resolution: Mapped[str | None] = mapped_column(String(8), nullable=True)

    __table_args__ = (
        # R3/R4: обычный UNIQUE в Postgres допускает несколько NULL → tenant-wide двоился бы.
        # Expression-unique по coalesce(user_id,'') снимает это. ON CONFLICT таргетить тем же выражением.
        Index(
            "uq_react_turn_trace_scope",
            "tenant_id", text("coalesce(user_id, '')"), "turn_key",
            unique=True,
        ),
        Index("ix_react_trace_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_react_trace_turn_key", "turn_key"),
        Index("ix_react_trace_outcome", "outcome"),
        # детект застрявших: status='in_progress' старше N мин = краш; awaiting_confirm — ждёт юзера
        Index("ix_react_trace_status_created", "status", "created_at"),
    )
