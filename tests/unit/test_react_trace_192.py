"""#192 Фаза A — данные: HMAC аргументов + модель react_turn_trace + ПД-шифрование.

Хуки start/pause/finish (Фаза B) тестируются отдельно. Здесь — фундамент: HMAC ≠ голый sha256,
модель пишется/читается, контент зашифрован в СЫРОМ столбце.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import text as _sql

from sreda.services import trace_hash


def test_trace_args_hash_is_hmac():
    """args_hash = HMAC с domain-separation, НЕ голый sha256 (иначе словарь по «молоко»)."""
    h = trace_hash.args_hmac(tenant_id="t1", tool_name="schedule_reminder", args={"text": "молоко"})
    assert h.startswith("v1:")  # версия + key_id в формате
    # НЕ равен голому sha256 canonical args (словарная уязвимость R1)
    plain = hashlib.sha256(
        trace_hash.canonical_json({"text": "молоко"}).encode("utf-8")).hexdigest()
    assert plain not in h, "args_hash не должен быть голым sha256 аргументов"
    # детерминирован
    assert h == trace_hash.args_hmac(tenant_id="t1", tool_name="schedule_reminder",
                                     args={"text": "молоко"})
    # domain-separation: другой тенант/инструмент → другой хэш
    assert h != trace_hash.args_hmac(tenant_id="t2", tool_name="schedule_reminder",
                                     args={"text": "молоко"})
    assert h != trace_hash.args_hmac(tenant_id="t1", tool_name="add_task",
                                     args={"text": "молоко"})


def test_trace_model_roundtrip_and_pii_ciphertext(db_session):
    """Модель пишется/читается; ORM расшифровывает; СЫРОЙ столбец = ciphertext (ПД не в открытом виде)."""
    from sreda.db.models import ReactTurnTrace

    secret = "напомни купить молоко завтра в 9"
    db_session.add(ReactTurnTrace(
        id="rt1", tenant_id="t1", user_id="u1", thread_id="th1", turn_key="react:tg:t1:m1",
        status="in_progress", origin_user_text=secret))
    db_session.commit()
    db_session.expire_all()

    row = db_session.get(ReactTurnTrace, "rt1")
    assert row.origin_user_text == secret  # ORM расшифровывает
    assert row.status == "in_progress"
    assert row.confirm_state == "none"  # дефолт

    # СЫРОЙ столбец (в обход type decorator) — ciphertext, не открытый текст
    raw = db_session.execute(
        _sql("SELECT origin_user_text FROM react_turn_trace WHERE id='rt1'")).scalar()
    assert raw is not None
    assert raw != secret, "ПД должна быть зашифрована в столбце (raw != plaintext)"
    assert secret not in raw


def test_trace_expression_unique_index_exists(db_session):
    """Expression-unique индекс создан (миграция/метадата) — основа upsert по coalesce(user_id,'')."""
    from sreda.db.models import ReactTurnTrace

    idx_names = {ix.name for ix in ReactTurnTrace.__table__.indexes}
    assert "uq_react_turn_trace_scope" in idx_names
