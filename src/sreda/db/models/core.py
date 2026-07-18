from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, event, text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 152-ФЗ Часть 2 (2026-04-28): tenant.name содержит Telegram
    # first/last name юзера — это PII. Шифруется через EncryptedString;
    # в дампе БД лежит base64-шифр. ORM прозрачно расшифровывает на read.
    name: Mapped[str] = mapped_column(EncryptedString())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    # Approval gate (2026-04-23, MVP-костыль до подписок). NULL = заявка
    # принята, но ещё не одобрена модератором; сообщения silent-drop'ятся
    # в telegram_webhook. Одобрение — в админке /admin/users. Существующие
    # тенанты помечены NOW() миграцией при накатывании колонки, так что
    # живые пользователи не ломаются.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Dual-channel (2026-05-01): 'telegram' | 'max'. Юзер выбирает канал
    # при регистрации тарифа на sredaspace.ru. NULL = legacy (Telegram-only,
    # все 12 существующих тенантов до миграции). Handler'ы трактуют NULL
    # как 'telegram' для back-compat.
    preferred_channel: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    # #187 soft-delete (2026-06-22): NULL = активен; не-null = помечен удалённым
    # (обратимо — restore снимает). Источник истины рубильника — is_tenant_active()
    # в services/tenant_lifecycle.py. В таблицы данных признак НЕ добавляем (один флаг).
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        # Partial-индекс ТОЛЬКО по удалённым (для админ-листинга удалённых тенантов).
        # Гейт is_tenant_active идёт по PK; ~99% строк deleted_at=NULL → full-index
        # бесполезен. where-clause обязан быть sa_text(), не bare-строкой.
        Index(
            "ix_tenants_deleted_at",
            "deleted_at",
            postgresql_where=sa_text("deleted_at IS NOT NULL"),
            sqlite_where=sa_text("deleted_at IS NOT NULL"),
        ),
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))


class TenantFeature(Base):
    __tablename__ = "tenant_features"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Plaintext Telegram chat_id, зашифрован через EncryptedString
    # (152-ФЗ обезличивание Часть 1, 2026-04-27). В дампе БД лежит
    # base64-шифр, не PII. Worker'ы получают plaintext через ORM read
    # (TypeDecorator расшифровывает) — нужно для вызова `sendMessage`.
    # Lookup по chat_id идёт через `tg_account_hash` (HMAC-SHA256),
    # см. `services/tg_account_hash.py` + `find_user_by_chat_id`.
    telegram_account_id: Mapped[str | None] = mapped_column(
        EncryptedString(), nullable=True,
    )
    # Hash от plaintext chat_id для O(1) lookup'а без расшифровки
    # всех записей. Backfill миграцией 0027. Unique — один tg-аккаунт
    # = один user. None для legacy/seed юзеров без telegram_account_id.
    tg_account_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True,
    )
    # MAX user identifier (parallel to telegram_account_id, 2026-05-01).
    # Plain (НЕ encrypted) — MAX user_id это просто числовой ID, как
    # Telegram tg_account_hash. Indexed для O(1) lookup'а в onboarding.
    # NULL для TG-only юзеров.
    max_account_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )
    # MAX chat_id — DM chat между ботом и юзером (parallel to MAX user_id).
    # Phase 1 (migration 0039, 2026-05-04): probe показал что в MAX
    # `recipient` для send_message = ``{"chat_id": ...}``, не user_id.
    # При первом incoming update'е сохраняем оба идентификатора:
    # ``user.user_id`` → max_account_id, ``chat_id`` → max_chat_id.
    # Indexed для outbox lookup. NULL legacy/migrated rows.
    max_chat_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    # bot_key последнего inbound'а от этого юзера (#109, migration 0054).
    # Захватывается в `ensure_telegram_user_bundle` на каждом TG inbound.
    # Async producers (reminder / proactive / onboarding workers) читают
    # это поле через `resolve_outbox_routings`, чтобы доставлять нотификации
    # на ТЕКУЩИЙ бот юзера, а не на тот, где напоминание было создано.
    # Нужно для миграции с `sreda01_bot` (key `sreda`) на новый
    # `sreda_home_bot` (key `sreda_home`): без этого pending reminders
    # уходили на старый бот и терялись. NULL = legacy/non-migrated → producers
    # используют свои существующие fallback'и (back-compat).
    last_bot_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )

    __table_args__ = (
        # Partial unique: один MAX-аккаунт = один User (миграция
        # 20260718_0085; audit 2026-07-18 svc-inbound #1 / cross-concurrency
        # FC-2). Collision-check в `channel_linking.consume_link` идёт без
        # блокировки — только БД-констрейнт превращает гонку в громкий
        # IntegrityError вместо тихого дубля (дубль = AmbiguousExternalIdentity
        # и молчаливый drop MAX-входа навсегда). Partial: TG-only юзеры с
        # NULL не ограничены. where-clause обязан быть sa_text(), не строкой.
        Index(
            "uq_users_max_account_id",
            "max_account_id",
            unique=True,
            postgresql_where=sa_text("max_account_id IS NOT NULL"),
            sqlite_where=sa_text("max_account_id IS NOT NULL"),
        ),
    )


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # 152-ФЗ Часть 2: payload_json может содержать task title / args
    # с PII. Шифруется через EncryptedString.
    payload_json: Mapped[str] = mapped_column(EncryptedString(), default="{}")
    # Required for retention cleanup (spec 41). Indexed because the cleanup
    # job filters by ``status IN (...) AND created_at < cutoff``.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # 152-ФЗ Часть 2: payload_json содержит сгенерированный LLM текст
    # ответа бота — это контент переписки. Шифруется EncryptedString.
    payload_json: Mapped[str] = mapped_column(EncryptedString())
    # Required for retention cleanup (spec 41): 30 days for sent,
    # 60 days for failed — all keyed off creation time since we don't
    # store a separate ``sent_at`` yet.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    # Deferred delivery (Phase 2). ``NULL`` means "send now". The outbox
    # delivery worker picks up rows where ``scheduled_at IS NULL OR
    # scheduled_at <= now``. Used by the quiet-hours enforcement to bump
    # non-urgent messages past the user's quiet window.
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Which skill produced this reply. ``NULL`` means "platform core"
    # (help/status/subscriptions). Used by quiet-hours enforcement to
    # look up per-skill ``notification_priority`` in
    # ``tenant_user_skill_configs``.
    feature_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Recipient user (Phase 2d). The delivery worker uses this to resolve
    # per-user profile + per-skill priority. ``NULL`` means "broadcast
    # or system-level" and skips the user-scoped policy entirely.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # ``True`` when this reply is a direct response to an inbound user
    # message (``action.inbound_message_id is not None``). Interactive
    # deliveries bypass quiet-hours — users get replies to their own
    # commands immediately, always.
    is_interactive: Mapped[bool] = mapped_column(Boolean, default=False)
    # Phase 5-lite: reason the message was dropped by decide_to_speak
    # or muted by skill config. Values: ``duplicate`` / ``throttle`` /
    # ``llm_filter`` (future) / ``muted`` / ``policy`` /NULL. Surfaces
    # in ``/stats`` so users see WHY the bot stayed silent.
    drop_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Phase 4a (second-tg-bot): which bot should deliver this outbox row.
    # Made NOT NULL in Phase 5 (migration 20260603_0052) once every
    # producer was updated to set this field.
    bot_key: Mapped[str] = mapped_column(
        # NOT NULL + legacy default ("sreda" = LEGACY_NULL_BOT_KEY): production
        # producers set bot_key EXPLICITLY (guarded by the allowlist/AST
        # enforcement test); this default is only a safety net so a non-producer
        # insert (tests, future code) can never violate NOT NULL.
        String(64), nullable=False, default="sreda",
        server_default=sa_text("'sreda'"), index=True
    )
    # #163 Фаза 4 — ключ идемпотентности доставки (с КАНАЛОМ+ботом): не более ОДНОГО
    # outbox-сообщения на (источник+канал+бот). Воркер напоминаний кладёт
    # f"{reminder_id}:{fired_trigger_iso}:{channel}:{bot_key}": эскалационные ре-пинги
    # (разный fired_trigger) и dual TG+MAX (разный канал) РАЗЛИЧАЮТСЯ; повтор той же тройки
    # (мультипроцесс/повтор-enqueue) → дедуп. Партиал (IS NOT NULL): прочие продюсеры
    # (без ключа) НЕ ограничены — аддитивно, ничего из легаси не схлопывается.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # #344 F5 — atomic-claim lease (аддитивно, миграция 20260711_0084). claim НЕ вводит
    # новых ЗНАЧЕНИЙ status: строка остаётся 'pending' во время lease. Воркер
    # клеймит строку атомарным UPDATE (claim_token = свой токен, lease_expires_at =
    # now+lease) и коммитит ДО доставки → второй воркер не выберет claimed-строку
    # (фильтр `claim_token IS NULL OR lease_expires_at < now`). Терминальные статусы
    # (sent/failed/muted/dropped) claim игнорируют. СТАРЫЙ воркер (после отката)
    # эти поля не читает и доставляет 'pending' как раньше → rollback-safe (§4).
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # NB: индекс объявлен в __table_args__ как ЧАСТИЧНЫЙ с ЯВНЫМ именем, совпадающим
    # с миграцией 20260711_0084 (ix_outbox_lease_expires_at). Без index=True на колонке — иначе
    # create_all создал бы второй, НЕчастичный индекс с другим именем (расхождение
    # metadata↔alembic, autogenerate-churn; R1 MINOR).
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_outbox_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=sa_text("idempotency_key IS NOT NULL"),
            sqlite_where=sa_text("idempotency_key IS NOT NULL"),
        ),
        # Предикат включает status='pending' (R2 MINOR): терминальные строки
        # (sent/failed/muted/dropped) со стухшим lease НЕ попадают в индекс, даже
        # если claim-поля на них не очистили → partial-index не пухнет доставленными.
        # Совпадает с фильтром claim-скана (status='pending' AND lease_expires_at<now).
        Index(
            "ix_outbox_lease_expires_at",
            "lease_expires_at",
            postgresql_where=sa_text("lease_expires_at IS NOT NULL AND status = 'pending'"),
            sqlite_where=sa_text("lease_expires_at IS NOT NULL AND status = 'pending'"),
        ),
    )


class SecureRecord(Base):
    __tablename__ = "secure_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    record_type: Mapped[str] = mapped_column(String(64), index=True)
    record_key: Mapped[str] = mapped_column(String(128), index=True)
    encrypted_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    channel_type: Mapped[str] = mapped_column(String(32), index=True)
    channel_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_key: Mapped[str] = mapped_column(String(64), index=True)
    external_update_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sender_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 152-ФЗ Часть 2: содержит входящие сообщения юзера (после
    # privacy_guard санитизации). Это контент переписки → шифруем.
    message_text_sanitized: Mapped[str | None] = mapped_column(
        EncryptedString(), nullable=True
    )
    contains_sensitive_data: Mapped[bool] = mapped_column(Boolean, default=False)
    secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="accepted", index=True)
    # processing_status — explicit lifecycle для мониторинга «inbound persisted
    # но processing crashed». Значения: ingested → processing_started →
    # processed (штатное завершение turn'а), либо ignored (pending user /
    # unsupported / service command — намеренный skip без processing'а).
    # Duplicate update_id — отдельный no-op (новая row не создаётся).
    processing_status: Mapped[str] = mapped_column(
        String(32), default="ingested", server_default="ingested", index=True,
    )
    # #127: ретеншн чистит чанками с фильтром по created_at — без индекса
    # каждый чанк = полный скан таблицы (миграция 20260611_0055).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        # Partial unique index: one inbound record per (channel_type, bot_key,
        # external_update_id) — prevents cross-bot dedup collisions when two
        # Telegram bots share the same update_id counter space (they're
        # independent per-bot sequences).  NULL update_ids are excluded so
        # synthetic / no-id events are still insertable freely.
        # NOTE: where-clauses must be sa.text() objects, not bare strings.
        Index(
            "ux_inbound_dedup_channel_bot_update",
            "channel_type",
            "bot_key",
            "external_update_id",
            unique=True,
            postgresql_where=sa_text("external_update_id IS NOT NULL"),
            sqlite_where=sa_text("external_update_id IS NOT NULL"),
        ),
    )


# 152-ФЗ обезличивание Часть 1 (2026-04-27): автоматически вычисляем
# `tg_account_hash` каждый раз, когда выставляется `telegram_account_id`.
# Это снимает с вызывающего кода (онбординг, тесты, seed) обязанность
# помнить про вторую колонку — достаточно записать chat_id, hash
# заполнится сам.
#
# Если salt не сконфигурирован (`SREDA_TG_ACCOUNT_SALT`), листенер
# падает RuntimeError — но это правильно: иначе lookup по hash будет
# всегда возвращать None и юзер «потеряется». Тесты подсовывают salt
# через conftest.
@event.listens_for(User.telegram_account_id, "set", retval=False)
def _user_telegram_account_id_set(  # noqa: ANN001 — SQLAlchemy event signature
    target, value, oldvalue, initiator,  # noqa: ARG001
):
    if value is None or value == "":
        target.tg_account_hash = None
        return
    if isinstance(value, str) and not value.strip():
        target.tg_account_hash = None
        return
    # Lazy import — services.tg_account_hash тянет settings,
    # которые при импорте models создают цикл.
    from sreda.services.tg_account_hash import hash_tg_account

    target.tg_account_hash = hash_tg_account(value)
