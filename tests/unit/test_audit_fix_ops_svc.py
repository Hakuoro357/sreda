"""Регрессионные тесты аудит-фиксов 2026-07-18 (slug: ops-svc).

Покрывают ключевые находки svc-ops / svc-security ревью:

- audit_feed.relay_outbox  — retry-marking на сломанной строке (MAJOR #1)
- audit.audit_event        — commit-контракт (MINOR #3)
- trace.deserialize        — re-anchor at_ms (MINOR #2)
- operation_id             — key_id/legacy-кандидаты ротации (MINOR #4)
- rate_limiter             — eviction протухших ключей (sec MINOR #1)
- privacy_guard            — точные URL-маркеры (sec MINOR #2)
- tenant_lifecycle         — async advisory lock (sec MINOR #6)
- admin_login              — trusted XFF + last_seen_at (sec MINOR #5/#7)
- telegram_auth/max_auth   — auth_date из будущего (sec MINOR #3)
- time_phrase_validator    — 17:00 → «вечер» (ops MINOR #6)
- date_drift_validator     — точный relative carve-out (ops MINOR #7)
- reply_buttons            — 64-битные токены (ops MINOR #10)
- agent_capabilities       — общий entitlement-фильтр (ops MINOR #9)

Без сети и без Postgres: SQLite + in-memory, PG-пути помечены no-op.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register ALL tables on Base.metadata
from sreda.db.models import AuditOutboxEvent, UserDataChangeFeedEvent
from sreda.db.models.audit import AuditLog
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.reply_buttons import ReplyButtonCache
from sreda.services import admin_login as al
from sreda.services import agent_capabilities as ac
from sreda.services import operation_id
from sreda.services import trace as trace_mod
from sreda.services.admin_login import trusted_client_ip
from sreda.services.audit import audit_event
from sreda.services.audit_feed import emit_event, relay_outbox
from sreda.services.date_drift_validator import find_drifted_dates
from sreda.services.privacy_guard import RegexPrivacyGuard
from sreda.services.rate_limiter import InMemoryRateLimiter
from sreda.services.reply_buttons import ReplyButtonService
from sreda.services.telegram_auth import (
    TelegramInitDataError,
    validate_init_data,
)
from sreda.services.tenant_lifecycle import tenant_advisory_lock_async
from sreda.services.time_phrase_validator import classify_period


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _emit(session, op_id: str, *, occurred: datetime) -> None:
    emit_event(
        session,
        operation_id=op_id,
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="task",
        entity_id=None,
        action="created",
        payload={"title": "x"},
        occurred_at=occurred,
    )


# ---------------------------------------------------------------------------
# svc-ops MAJOR #1 — relay_outbox retry-marking на «отравленной» транзакции
# ---------------------------------------------------------------------------


def test_relay_outbox_marks_retry_and_continues_batch(session, monkeypatch):
    """Generic-сбой при переносе строки: строка помечается attempts/last_error,
    батч НЕ падает, остальные строки переносятся; следующий тик додренивает."""
    now = datetime.now(timezone.utc)
    _emit(session, "op_one", occurred=now)
    _emit(session, "op_two", occurred=now + timedelta(seconds=1))

    orig_delete = session.delete
    calls = {"n": 0}

    def flaky_delete(obj):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip")
        return orig_delete(obj)

    # Generic-сбой ВНУТРИ per-row savepoint (на delete, после INSERT в
    # feed) — тот же класс сбоя, что connection blip на flush: раньше
    # retry-маркировка делала flush на «отравленной» транзакции и роняла
    # весь батч.
    monkeypatch.setattr(session, "delete", flaky_delete)

    relayed = relay_outbox(session, batch_size=10)
    assert relayed == 1  # одна строка перенесена, одна помечена к ретраю

    remaining = session.execute(select(AuditOutboxEvent)).scalars().all()
    assert len(remaining) == 1
    stuck = remaining[0]
    assert stuck.attempts == 1
    assert stuck.last_error is not None and "db blip" in stuck.last_error
    assert stuck.last_attempt_at is not None

    # Строка НЕ потеряна и НЕ продублирована в feed.
    feed_rows = session.execute(select(UserDataChangeFeedEvent)).scalars().all()
    assert len(feed_rows) == 1
    assert feed_rows[0].operation_id != stuck.operation_id

    # Следующий тик (flush уже не падает) додренивает застрявшую строку.
    relayed2 = relay_outbox(session, batch_size=10)
    assert relayed2 == 1
    assert session.execute(select(AuditOutboxEvent)).scalars().all() == []
    assert len(session.execute(select(UserDataChangeFeedEvent)).scalars().all()) == 2


# ---------------------------------------------------------------------------
# svc-ops MINOR #3 — audit_event не коммитит/не откатывает чужую сессию
# ---------------------------------------------------------------------------


def test_audit_event_default_does_not_commit_foreign_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/audit.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s1 = factory()
    s2 = factory()
    try:
        row = audit_event(
            s1, actor_type="admin", actor_id="h", action="a.b",
        )
        assert row is not None
        # flush был (строка видна в своей сессии), commit — нет.
        assert s1.query(AuditLog).count() == 1
        assert s2.query(AuditLog).count() == 0

        audit_event(
            s1, actor_type="admin", actor_id="h", action="a.b", commit=True,
        )
        # commit=True коммитит ВСЮ сессию — видны обе строки (и первая,
        # висевшая во flush). В prod-контракте это и есть «caller владеет
        # сессией и хочет немедленную durability».
        assert s2.query(AuditLog).count() == 2
    finally:
        s1.close()
        s2.close()


def test_audit_event_rollback_only_when_committing(session, monkeypatch):
    rollbacks: list[str] = []

    def fake_rollback():
        rollbacks.append("rollback")

    def failing_add(_obj):
        raise RuntimeError("db down")

    monkeypatch.setattr(session, "rollback", fake_rollback)
    monkeypatch.setattr(session, "add", failing_add)

    # commit=False (default): чужую сессию НЕ откатываем (SQLite-клобберинг).
    assert audit_event(
        session, actor_type="admin", actor_id="x", action="a.b",
    ) is None
    assert rollbacks == []

    # commit=True: сессией владеет хелпер → rollback обязателен (PG aborted).
    assert audit_event(
        session, actor_type="admin", actor_id="x", action="a.b", commit=True,
    ) is None
    assert rollbacks == ["rollback"]


# ---------------------------------------------------------------------------
# svc-ops MINOR #2 — outbox.delivered не на 0ms
# ---------------------------------------------------------------------------


def test_deserialize_reanchors_final_event_after_last():
    payload = {
        "trace_id": "trace_anchor",
        "user_id": "u1",
        "tenant_id": "t1",
        "channel": "telegram",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "outcome": "ok",
        "events": [
            {"at_ms": 100, "step": "webhook.received", "duration_ms": 0, "meta": {}},
            {"at_ms": 250, "step": "llm.iter.0", "duration_ms": 5, "meta": {}},
        ],
    }
    ctx = trace_mod.deserialize_from_outbox(payload)
    trace_mod.emit_block(ctx, final_event_name="outbox.delivered")
    final = ctx.events[-1]
    assert final.step == "outbox.delivered"
    # Раньше было ≈0ms (до событий uvicorn); теперь — после последнего.
    assert final.at_ms >= 250


# ---------------------------------------------------------------------------
# svc-ops MINOR #4 — dedup-хеш: primary + legacy кандидаты (ротация)
# ---------------------------------------------------------------------------


def test_title_hash_candidates_cover_legacy_keys(monkeypatch):
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "primary-key-material")
    primary = operation_id.compute_normalized_title_hash("молоко")

    monkeypatch.setattr(
        operation_id,
        "_get_legacy_hmac_keys",
        lambda: [("old", b"legacy-key-material")],
    )
    cands = operation_id.compute_normalized_title_hash_candidates("молоко")
    assert cands[0] == primary
    assert len(cands) == 2
    assert cands[1] != primary  # хеш под старым ключом — для lookup старых строк

    # Без ротации — один кандидат, поведение ≡ одиночному хешу.
    monkeypatch.setattr(operation_id, "_get_legacy_hmac_keys", lambda: [])
    assert operation_id.compute_normalized_title_hash_candidates("молоко") == [primary]


# ---------------------------------------------------------------------------
# svc-security MINOR #1 — rate_limiter eviction
# ---------------------------------------------------------------------------


def test_rate_limiter_evicts_stale_keys():
    now = [1000.0]
    rl = InMemoryRateLimiter(
        max_requests=1, window_seconds=10, clock=lambda: now[0],
    )
    assert rl.check("scanner-1")
    assert rl.check("scanner-2")
    assert "scanner-1" in rl._log

    now[0] += 100.0  # все записи протухли
    for _ in range(64):  # 64-я проверка триггит amortized-sweep
        rl.check("fresh")
    assert "scanner-1" not in rl._log
    assert "scanner-2" not in rl._log
    assert "fresh" in rl._log


def test_rate_limiter_still_limits_after_sweep():
    now = [0.0]
    rl = InMemoryRateLimiter(
        max_requests=2, window_seconds=60, clock=lambda: now[0],
    )
    assert rl.check("k")
    assert rl.check("k")
    assert not rl.check("k")  # лимит держится


# ---------------------------------------------------------------------------
# svc-security MINOR #2 — privacy_guard точные маркеры URL
# ---------------------------------------------------------------------------


def test_privacy_guard_keeps_benign_urls():
    guard = RegexPrivacyGuard()
    res = guard.sanitize_text("почитай https://example.com/design/article")
    assert "https://example.com/design/article" in res.sanitized_text

    res = guard.sanitize_text("https://example.com/monkey/business")
    assert "[url]" not in res.sanitized_text


def test_privacy_guard_masks_secret_token_urls():
    guard = RegexPrivacyGuard()
    # Отдельный токен /sig/ в пути — маскируем.
    res = guard.sanitize_text("https://example.com/sig/track")
    assert "[url]" in res.sanitized_text
    # Любой URL с query — маскируем (поведение не изменилось).
    res = guard.sanitize_text("https://example.com/page?x=1")
    assert "[url]" in res.sanitized_text


# ---------------------------------------------------------------------------
# svc-security MINOR #6 — async advisory lock (SQLite no-op обвязка)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_advisory_lock_async_noop_on_sqlite(session):
    async with tenant_advisory_lock_async(session, "tenant-1"):
        pass  # no-op на SQLite, но обвязка (enter/exit) работает


# ---------------------------------------------------------------------------
# svc-security MINOR #5/#7 — admin_login: trusted XFF + last_seen_at
# ---------------------------------------------------------------------------


def test_trusted_client_ip_honours_proxy_flag():
    assert (
        trusted_client_ip("1.2.3.4, 5.6.7.8", "9.9.9.9", behind_trusted_proxy=True)
        == "1.2.3.4"
    )
    # Без trusted proxy XFF — client-controlled, доверяем только peer.
    assert (
        trusted_client_ip("1.2.3.4", "9.9.9.9", behind_trusted_proxy=False)
        == "9.9.9.9"
    )
    assert trusted_client_ip("", "9.9.9.9", behind_trusted_proxy=True) == "9.9.9.9"
    assert trusted_client_ip(None, None, behind_trusted_proxy=False) is None


def test_resolve_session_touches_last_seen_throttled(session):
    now0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    raw = al.mint_session(session, "tg1", now=now0)

    row = al.resolve_session(session, raw, now=now0 + timedelta(seconds=30))
    assert row is not None
    # Троттлинг: 30s < 60s — last_seen_at не переписываем.
    assert al._coerce_utc(row.last_seen_at) == now0

    row = al.resolve_session(session, raw, now=now0 + timedelta(hours=2))
    assert row is not None
    assert al._coerce_utc(row.last_seen_at) == now0 + timedelta(hours=2)


# ---------------------------------------------------------------------------
# svc-security MINOR #3 — auth_date из будущего
# ---------------------------------------------------------------------------

_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _make_tg_init_data(*, auth_date: int) -> str:
    user_json = json.dumps(
        {"id": 12345, "first_name": "Test", "username": "testuser"},
        separators=(",", ":"),
    )
    params = {"auth_date": str(auth_date), "user": user_json}
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(params.items())
    )
    secret_key = hmac.new(
        b"WebAppData", _BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    params["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urlencode(params)


def test_telegram_auth_date_in_future_rejected():
    init_data = _make_tg_init_data(auth_date=int(time.time()) + 3600)
    with pytest.raises(TelegramInitDataError, match="future"):
        validate_init_data(init_data, _BOT_TOKEN)


def test_telegram_auth_date_small_clock_skew_allowed():
    init_data = _make_tg_init_data(auth_date=int(time.time()) + 120)
    user = validate_init_data(init_data, _BOT_TOKEN)
    assert user.telegram_id == "12345"


# ---------------------------------------------------------------------------
# svc-ops MINOR #6 — 17:00 → «вечер»
# ---------------------------------------------------------------------------


def test_classify_period_evening_starts_at_17():
    assert classify_period(16) == "день"
    assert classify_period(17) == "вечер"
    assert classify_period(22) == "вечер"
    assert classify_period(11) == "утро"
    assert classify_period(23) == "ночь"


# ---------------------------------------------------------------------------
# svc-ops MINOR #7 — relative carve-out точный
# ---------------------------------------------------------------------------


def test_date_drift_relative_word_must_match_target():
    today = "2026-05-18"
    # Юзер сказал «завтра», бот записал ВЧЕРА — drift ДОЛЖЕН флагнуться
    # (прод-инцидент 2026-05-18; раньше любое слово оправдывало любую дату).
    findings = find_drifted_dates(
        "Записала: 17.05.2026, утро — сахар 10.2",
        iso_date_today=today,
        user_text="напомни завтра про лекарство",
    )
    assert len(findings) == 1
    assert findings[0]["days_ago"] == 1

    # Юзер сказал «вчера», бот записал вчера — carve-out работает.
    findings = find_drifted_dates(
        "Записала: 17.05.2026, утро — сахар 10.2",
        iso_date_today=today,
        user_text="Сахар вчера был 10,2",
    )
    assert findings == []


# ---------------------------------------------------------------------------
# svc-ops MINOR #10 — 64-битные callback-токены
# ---------------------------------------------------------------------------


def test_reply_button_tokens_are_64bit(session):
    svc = ReplyButtonService(session)
    pairs = svc.create_tokens(tenant_id="t1", user_id="u1", labels=["Да", "Нет"])
    assert [label for _, label in pairs] == ["Да", "Нет"]
    for token, _ in pairs:
        assert len(token) == 16  # 8 байт = 64 бита, String(16) PK
        assert all(c in "0123456789abcdef" for c in token)
    # Колонка String(16) вмещает токен целиком.
    row = session.get(ReplyButtonCache, pairs[0][0])
    assert row is not None and row.token == pairs[0][0]


# ---------------------------------------------------------------------------
# svc-ops MINOR #9 — общий entitlement-фильтр agent_capabilities
# ---------------------------------------------------------------------------


def test_active_feature_keys_and_voice_share_entitlement_filter(session):
    plan = SubscriptionPlan(
        id="plan_hw", plan_key="housewife", feature_key="housewife_assistant",
        title="HW", description="", price_rub=0,
    )
    session.add(plan)
    now = datetime.now(timezone.utc)
    # Активная (NULL active_until = unlimited).
    session.add(TenantSubscription(
        id="sub_active", tenant_id="t1", plan_id="plan_hw",
        feature_key="housewife_assistant", status="active", quantity=1,
        active_until=None,
    ))
    # Истёкшая — НЕ должна попадать.
    session.add(TenantSubscription(
        id="sub_expired", tenant_id="t2", plan_id="plan_hw",
        feature_key="housewife_assistant", status="active", quantity=1,
        active_until=now - timedelta(days=1),
    ))
    # Приостановленная по статусу — НЕ должна попадать.
    session.add(TenantSubscription(
        id="sub_paused", tenant_id="t3", plan_id="plan_hw",
        feature_key="housewife_assistant", status="paused", quantity=1,
        active_until=None,
    ))
    session.flush()

    assert ac.active_feature_keys(session, "t1") == {"housewife_assistant"}
    assert ac.active_feature_keys(session, "t2") == set()
    assert ac.active_feature_keys(session, "t3") == set()

    # Voice-гейт идёт по ТОМУ ЖЕ фильтру (manifest includes_voice зависит от
    # registry — здесь проверяем только общий отбор подписок).
    keys_t1 = {plan.feature_key for _s, plan in ac._iter_active_subscriptions(session, "t1")}
    assert keys_t1 == {"housewife_assistant"}
    assert list(ac._iter_active_subscriptions(session, "t2")) == []
