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

from sreda.db.models.tool_operations import ToolOperationResult

logger = logging.getLogger(__name__)


class IdempotencyArgsMismatch(Exception):
    """Тот же operation_id, но args_hmac РАЗОШЁЛСЯ — внутренняя ошибка (коллизия ключа /
    баг canonicalization), НЕ user-facing (план #163: metric+alert+fallback, не «конфликт»)."""


def compute_args_hmac(args: Any, *, secret: str) -> str:
    """HMAC-SHA256 от canonical JSON аргументов (sort_keys → стабильно). secret — серверный
    (не в коде/логах). Сырые args НЕ хранить (ПД); только этот hmac."""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


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

    committed + тот же args_hmac → сохранённый payload, мутации НЕТ.
    committed + иной args_hmac → IdempotencyArgsMismatch (внутренняя ошибка).
    нет строки / pending → claim → mutate_fn() → payload+committed → commit.
    """
    existing = (
        session.query(ToolOperationResult)
        .filter(ToolOperationResult.operation_id == operation_id)
        .one_or_none()
    )
    if existing is not None and existing.status == "committed":
        if existing.args_hmac != args_hmac:
            logger.warning("idempotent_ops: args_hmac mismatch op=%s family=%s",
                           operation_id, operation_family)
            raise IdempotencyArgsMismatch(operation_id)
        return existing.stable_return_payload  # exact-replay без мутации

    now = datetime.now(timezone.utc)
    if existing is None:
        existing = ToolOperationResult(
            id=f"tor_{uuid4().hex}", tenant_id=tenant_id, user_id=user_id,
            operation_family=operation_family, tool_name=tool_name,
            operation_id=operation_id, args_hmac=args_hmac, status="pending",
            created_at=now, updated_at=now)
        session.add(existing)
        session.flush()  # занять operation_id (unique) ДО мутации (claim-first)

    # выполнить мутацию (в тесте — инъецированная no-op; реальная мутация сервиса — Фаза 3).
    payload = mutate_fn()
    existing.stable_return_payload = payload
    existing.status = "committed"
    existing.updated_at = datetime.now(timezone.utc)
    session.commit()
    return payload
