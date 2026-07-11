from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from sreda.db.models.core import Assistant, Tenant, TenantFeature, User, Workspace
from sreda.db.repositories.base import Repository


def _is_unique_violation(ex: Exception) -> bool:
    """UNIQUE/PK-конфликт (ожидаемый «уже есть») vs прочие integrity-ошибки
    (FK/NOT NULL/CHECK — настоящие баги, глушить нельзя). Кросс-диалектно:
    Postgres — SQLSTATE 23505; SQLite — текст 'UNIQUE constraint failed'."""
    orig = getattr(ex, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate == "23505"
    return "unique constraint failed" in str(orig or ex).lower()


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
    только savepoint + перехват UNIQUE-violation, работает под чистым INSERT-грантом.

    R2 (Codex high MAJOR-5): ``tenants`` и ``users`` вставляются ORM-объектами —
    их колонки ``name``/``telegram_account_id`` шифруются ``EncryptedString``
    (PII, 152-ФЗ); сырой SQL обошёл бы bind-процессор и записал плейнтекст.
    ``workspaces``/``assistants``/``tenant_features`` (plain String) — сырым INSERT.
    R2 (Codex high MAJOR-4): перехватывается ТОЛЬКО unique-violation; FK/NOT NULL/
    CHECK пробрасываются (иначе частичный битый бандл под видом «безобидного ретрая»).

    R2-2 (Codex high R2 MAJOR): ВЕСЬ бандл — в ОДНОМ savepoint (все 5 INSERT'ов
    атомарны). Любой UNIQUE-конфликт (гонка/ретрай/legacy-частичный бандл) →
    откат ВСЕГО бандла → created=False, НЕТ частичных строк (tenant/workspace без
    user). created определяется по факту вставки users (естественный unique
    tg_account_hash/max_account_id/PK ловит гонку двух первых сообщений).
    """
    from sqlalchemy.exc import IntegrityError

    now = datetime.now(timezone.utc)

    created = True
    try:
        with session.begin_nested():
            # ORM-объекты флашим ДО зависимых сырых INSERT'ов (autoflush может быть
            # off → сырой execute иначе выполнится раньше flush ORM → FK-violation).
            session.add(Tenant(id=tenant_id, name=tenant_name, created_at=now, approved_at=now))
            session.flush()  # tenants до workspaces/users (FK)
            session.execute(
                text("INSERT INTO workspaces (id, tenant_id, name) VALUES (:id, :t, :name)"),
                {"id": workspace_id, "t": tenant_id, "name": workspace_name},
            )
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
            session.flush()  # users до assistants (FK на workspace уже есть)
            session.execute(
                text(
                    "INSERT INTO assistants (id, tenant_id, workspace_id, name) "
                    "VALUES (:id, :t, :w, :name)"
                ),
                {"id": assistant_id, "t": tenant_id, "w": workspace_id, "name": assistant_name},
            )
            session.execute(
                text(
                    "INSERT INTO tenant_features (id, tenant_id, feature_key, enabled) "
                    "VALUES (:id, :t, 'core_assistant', :en)"
                ),
                {"id": f"{tenant_id}:core_assistant", "t": tenant_id, "en": True},
            )
    except IntegrityError as ex:
        if not _is_unique_violation(ex):
            raise  # FK/NOT NULL/CHECK — не «ретрай», настоящая ошибка
        created = False  # весь бандл откачен savepoint'ом — частичных строк нет
    # R2 (Codex high CRIT-3): НЕ commit'им здесь — весь identity-этап (гард +
    # бандл + грант) коммитится ОДИН раз вызывающим (provision_new_tenant_bundle).
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
