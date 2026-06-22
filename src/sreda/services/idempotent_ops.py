"""#163 субстрат прод-идемпотентности — общий helper durable-операций (Фаза 1в).

claim-first, exact-replay: на повтор того же ``operation_id`` со статусом ``committed`` и совпавшим
``args_hmac`` возвращаем сохранённый payload БЕЗ повторной мутации. Иначе — claim(pending) →
mutate_fn() → payload+committed → commit (per-operation транзакция; владелец commit'а — helper).

Фаза 1в: контракт exact-replay, проверка на инъецированной no-op мутации. Семантический межходовой
замок (advisory-lock + partial-unique) — Фаза 2; проводка в реальные сервисы/воркер — Фаза 3-4.

ПД: ``stable_return_payload`` — только tool-контрактный return (не полный snapshot), шифр на уровне
модели (JSONEncryptedString). ``args_hmac`` — HMAC-SHA256(secret) от canonical JSON, не сырые args.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from sreda.db.models.tool_operations import ToolOperationResult

logger = logging.getLogger(__name__)


class IdempotencyArgsMismatch(Exception):
    """Тот же operation_id, но args_hmac РАЗОШЁЛСЯ — внутренняя ошибка (коллизия ключа /
    баг canonicalization), НЕ user-facing (план #163: metric+alert+fallback, не «конфликт»)."""


class IdempotencyScopeMismatch(Exception):
    """Строка по operation_id принадлежит ДРУГОМУ tenant/user — анти-cross-tenant (внутренняя
    ошибка: глобальный operation_id обязан быть уникален; совпадение в чужом scope = баг/коллизия)."""


class IdempotencyInFlight(Exception):
    """Строка по operation_id есть, но НЕ committed (pending/failed) — исход неизвестен. НЕ
    перезапускаем mutate (анти-двойное-применение); вызывающий повторит/разрулит (Фаза 3)."""


def compute_args_hmac(args: Any, *, secret: str) -> str:
    """HMAC-SHA256 от canonical JSON аргументов (sort_keys → стабильно). secret — серверный
    (не в коде/логах). Сырые args НЕ хранить (ПД); только этот hmac."""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _lookup(session: Any, operation_id: str) -> ToolOperationResult | None:
    """Найти строку результата по operation_id (один источник для initial + post-race re-query)."""
    return (
        session.query(ToolOperationResult)
        .filter(ToolOperationResult.operation_id == operation_id)
        .one_or_none()
    )


def find_existing_pending_semantic(
    session: Any, model: Any, *, tenant_id: str, user_id: str | None, nhash: str,
) -> Any:
    """#163 Фаза 2b reuse: найти существующую *pending* строку с тем же semantic_key.

    Используется create-путями после конфликта partial-unique индекса (две одинаковые pending
    напоминания/задачи → вернуть существующую, «два повтора → 1 строка»). COALESCE-заглушка
    `__tenant_wide__` единообразна с индексом (общесемейные NULL user). Возвращает строку или None.
    """
    from sqlalchemy import func

    sentinel = "__tenant_wide__"
    return (
        session.query(model)
        .filter(
            model.tenant_id == tenant_id,
            func.coalesce(model.user_id, sentinel) == (user_id or sentinel),
            model.normalized_title_hash == nhash,
            model.status == "pending",
        )
        .order_by(model.created_at.asc())
        .first()
    )


def execute_idempotent_durable_op(
    session: Any,
    *,
    operation_id: str,
    tenant_id: str,
    user_id: str | None,
    operation_family: str,
    args_hmac: str,
    mutate_fn: Callable[[], Any],
    tool_name: str | None = None,
) -> Any:
    """exact-replay durable-операции. Возвращает payload (новый или сохранённый при повторе).

    committed + тот же args_hmac + тот же scope → сохранённый payload, мутации НЕТ.
    committed + иной args_hmac → IdempotencyArgsMismatch. чужой scope → IdempotencyScopeMismatch.
    pending/failed → IdempotencyInFlight (НЕ перезапускаем — анти-двойное-применение).
    нет строки → claim(pending) → mutate_fn() → payload+committed → commit (per-operation tx).
    """

    def _resolve(row: ToolOperationResult) -> Any:
        """Разрулить НАЙДЕННУЮ строку (по initial query ИЛИ после гонки-IntegrityError)."""
        # scope = tenant + user + operation_family (Codex R2): глобальный operation_id обязан быть
        # уникален; совпадение в чужом scope (вкл. иную семью) = коллизия/баг, не отдаём чужой payload.
        if (row.tenant_id != tenant_id
                or (row.user_id or None) != (user_id or None)
                or row.operation_family != operation_family):
            logger.warning("idempotent_ops: scope mismatch op=%s", operation_id)
            raise IdempotencyScopeMismatch(operation_id)
        if row.status != "committed":
            raise IdempotencyInFlight(operation_id)  # исход неизвестен → не перезапускаем
        if row.args_hmac != args_hmac:
            logger.warning("idempotent_ops: args_hmac mismatch op=%s family=%s",
                           operation_id, operation_family)
            raise IdempotencyArgsMismatch(operation_id)
        return row.stable_return_payload  # exact-replay без мутации

    existing = _lookup(session, operation_id)
    if existing is not None:
        return _resolve(existing)

    now = datetime.now(timezone.utc)
    row = ToolOperationResult(
        id=f"tor_{uuid4().hex}", tenant_id=tenant_id, user_id=user_id,
        operation_family=operation_family, tool_name=tool_name,
        operation_id=operation_id, args_hmac=args_hmac, status="pending",
        created_at=now, updated_at=now)
    try:
        with session.begin_nested():  # SAVEPOINT: гонка INSERT (unique) не отравит внешнюю tx
            session.add(row)
            session.flush()  # занять operation_id (unique) ДО мутации (claim-first)
    except IntegrityError:
        # racer успел вставить тот же operation_id между нашим query и flush → перечитать и
        # разрулить (replay / mismatch / in-flight), не дублируя мутацию.
        existing = _lookup(session, operation_id)
        if existing is None:
            # IntegrityError НЕ от нашего uq(operation_id) (Фаза 3: иной constraint) — НЕ маскируем
            # «строкой не найдено»; ре-райзим исходный (зеркало billing.py). Настоящий баг виден.
            raise
        return _resolve(existing)

    # claim наш → выполнить мутацию (в тесте no-op; реальная мутация сервиса — Фаза 3).
    payload = mutate_fn()
    row.stable_return_payload = payload
    row.status = "committed"
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return payload
