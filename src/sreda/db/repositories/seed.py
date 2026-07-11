from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from sreda.db.models.core import Assistant, Tenant, TenantFeature, User, Workspace
from sreda.db.repositories.base import Repository


def insert_only_bundle(
    session: Session,
    *,
    tenant_id: str,
    tenant_name: str,
    workspace_id: str,
    workspace_name: str,
    user_id: str,
    telegram_account_id: str | None = None,
    max_account_id: str | None = None,
    max_chat_id: str | None = None,
    last_bot_key: str | None = None,
    assistant_id: str,
    assistant_name: str,
) -> bool:
    """#138 Ф5-5b: провижн НОВОГО юзера — строго INSERT ... ON CONFLICT DO NOTHING.

    Возвращает ``created`` — был ли юзер реально вставлен ЭТИМ вызовом (True) или
    строка уже существовала — ретрай/гонка (False). Вызывающий провижнит free-tier
    подписку ТОЛЬКО при ``created=True`` (иначе на гонке двух первых сообщений
    выдал бы дубль подписки; под identity-ролью SELECT для idempotency-проверки
    недоступен, поэтому решение принимается по факту вставки).

    Identity-роль имеет ТОЛЬКО INSERT на bundle-таблицы (0078): ни pre-SELECT
    (``session.get``), ни UPDATE-веток здесь быть не может — под identity они
    упадут permission denied. Идемпотентность/гонка (два первых сообщения
    одновременно, ретрай) — plain INSERT в savepoint'е с перехватом
    IntegrityError: конфликт по PK/unique гасится тихим no-op, существующие
    строки НЕ перезаписываются. Перепривязка существующего юзера (channel
    linking, смена bot_key) — ОТДЕЛЬНЫЙ tenant-фазный путь под tenant_session.

    ``approved_at`` ставится сразу в INSERT (онбординг авто-одобряет с
    2026-06-11) — UPDATE tenants после создания не нужен.

    ⚠️ НЕ ``ON CONFLICT DO NOTHING``: в Postgres arbiter-проверка конфликта
    требует SELECT-привилегию, которой у identity-роли НЕТ по замыслу (доказано
    red-suite: identity ON CONFLICT → permission denied). Идемпотентность —
    только savepoint+IntegrityError, работает под чистым INSERT-грантом.
    users вставляется ORM-объектом (EncryptedString telegram_account_id +
    event-листенер хеша), прочие — сырым INSERT; все в своих savepoint'ах.
    """
    from sqlalchemy.exc import IntegrityError

    now = datetime.now(timezone.utc)

    def _insert_ignore(sql: str, params: dict) -> bool:
        """Plain INSERT в savepoint; True — вставлено, False — конфликт (уже есть)."""
        try:
            with session.begin_nested():
                session.execute(text(sql), params)
            return True
        except IntegrityError:
            return False

    _insert_ignore(
        "INSERT INTO tenants (id, name, created_at, approved_at) "
        "VALUES (:id, :name, :now, :now)",
        {"id": tenant_id, "name": tenant_name, "now": now},
    )
    _insert_ignore(
        "INSERT INTO workspaces (id, tenant_id, name) VALUES (:id, :t, :name)",
        {"id": workspace_id, "t": tenant_id, "name": workspace_name},
    )
    # users — ORM (шифрование + event-хеш); флаг created определяется ЗДЕСЬ
    # (естественный unique tg_account_hash/PK ловит гонку двух первых сообщений).
    created = True
    try:
        with session.begin_nested():
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    telegram_account_id=telegram_account_id,
                    max_account_id=max_account_id,
                    max_chat_id=max_chat_id,
                    last_bot_key=last_bot_key,
                )
            )
            session.flush()
    except IntegrityError:
        created = False  # строка уже есть (ретрай/гонка) — НЕ перезаписываем
    _insert_ignore(
        "INSERT INTO assistants (id, tenant_id, workspace_id, name) "
        "VALUES (:id, :t, :w, :name)",
        {"id": assistant_id, "t": tenant_id, "w": workspace_id, "name": assistant_name},
    )
    _insert_ignore(
        "INSERT INTO tenant_features (id, tenant_id, feature_key, enabled) "
        "VALUES (:id, :t, 'core_assistant', :en)",
        {"id": f"{tenant_id}:core_assistant", "t": tenant_id, "en": True},
    )
    session.commit()
    return created


class SeedRepository(Repository):
    def ensure_tenant_bundle(
        self,
        *,
        tenant_id: str,
        tenant_name: str,
        workspace_id: str,
        workspace_name: str,
        user_id: str,
        telegram_account_id: str | None = None,
        max_account_id: str | None = None,
        assistant_id: str,
        assistant_name: str,
    ) -> None:
        """Idempotently create / update a tenant bundle.

        Принимает либо ``telegram_account_id``, либо ``max_account_id``
        (либо оба — для mixed-channel юзеров после channel linking).
        Хотя бы один должен быть задан — иначе раннее ошибка.
        """
        if not telegram_account_id and not max_account_id:
            raise ValueError(
                "ensure_tenant_bundle requires telegram_account_id "
                "or max_account_id"
            )

        now = datetime.now(timezone.utc)

        tenant = self.session.get(Tenant, tenant_id)
        if tenant is None:
            self.session.add(Tenant(id=tenant_id, name=tenant_name, created_at=now))

        workspace = self.session.get(Workspace, workspace_id)
        if workspace is None:
            self.session.add(Workspace(id=workspace_id, tenant_id=tenant_id, name=workspace_name))

        # 152-ФЗ обезличивание Часть 1: tg_account_hash заполнится сам
        # через event-листенер на User.telegram_account_id (см.
        # db/models/core.py). max_account_id хранится прямо (per
        # schema 0035, plain indexed column).
        user = self.session.get(User, user_id)
        if user is None:
            self.session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    telegram_account_id=telegram_account_id,
                    max_account_id=max_account_id,
                )
            )
        else:
            if telegram_account_id is not None:
                user.telegram_account_id = telegram_account_id
            if max_account_id is not None:
                user.max_account_id = max_account_id

        self.session.flush()

        assistant = self.session.get(Assistant, assistant_id)
        if assistant is None:
            self.session.add(
                Assistant(
                    id=assistant_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    name=assistant_name,
                )
            )

        self._ensure_feature(tenant_id, "core_assistant", True)
        self.session.commit()

    def _ensure_feature(self, tenant_id: str, feature_key: str, enabled: bool) -> None:
        entity = (
            self.session.query(TenantFeature)
            .filter(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key == feature_key,
            )
            .one_or_none()
        )
        if entity is None:
            entity = TenantFeature(
                id=f"{tenant_id}:{feature_key}",
                tenant_id=tenant_id,
                feature_key=feature_key,
                enabled=enabled,
            )
            self.session.add(entity)
            return
        entity.enabled = enabled
