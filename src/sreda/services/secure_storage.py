from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.core import SecureRecord
from sreda.services.encryption import decrypt_value, encrypt_value


def store_secure_json(
    session: Session,
    *,
    record_type: str,
    record_key: str,
    value: dict,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
) -> SecureRecord:
    encrypted_json = encrypt_value(json.dumps(value, ensure_ascii=False))
    record = SecureRecord(
        id=f"sec_{uuid4().hex[:24]}",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        record_type=record_type,
        record_key=record_key,
        encrypted_json=encrypted_json,
    )
    session.add(record)
    return record


def load_secure_json(record: SecureRecord) -> dict:
    return json.loads(decrypt_value(record.encrypted_json))
