"""#133 — PlanExecuteHarness: функциональные тесты ВСЕГО тракта.

Полностью асинхронный харнес (план #133 final, R5): настоящий HTTP-слой
(ASGITransport), настоящие persist/онбординг/валидатор/executor/composer/БД;
замоканы только границы: канал, LLM планировщика (ContextVar-шов),
нижний вызов рта, внешняя сеть.
"""
from __future__ import annotations

import asyncio
import base64
import json
import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio


# --- запрет внешней сети на ВСЮ сюиту (чеклист п.8) ------------------------

# Субагент R1 MAJOR (доказано запуском): ProactorEventLoop (дефолт Windows)
# соединяется через ConnectEx МИМО socket.connect — форсируем selector-loop
# на всю сюиту, гард снова ловит async-соединения.
if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    @pytest.fixture(scope="session")
    def event_loop_policy():
        return asyncio.WindowsSelectorEventLoopPolicy()


_ALLOWED_HOSTS = ("127.0.0.1", "::1", "localhost", "test")


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    real_connect = socket.socket.connect
    real_gai = socket.getaddrinfo

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if str(host) not in _ALLOWED_HOSTS:
            raise AssertionError(f"внешняя сеть запрещена в functional: {addr!r}")
        return real_connect(self, addr)

    def guarded_gai(host, *a, **kw):
        # резолв чужих имён — тоже наружу (субагент: getaddrinfo не ловился)
        if str(host) not in _ALLOWED_HOSTS:
            raise AssertionError(f"DNS наружу запрещён в functional: {host!r}")
        return real_gai(host, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_gai)


# --- фейковый ТГ-клиент (повышен из test_housewife_persona_callbacks) ------

class FakeTelegramClient:
    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self.deletes: list[dict] = []
        self.answered: list[tuple[str, str | None]] = []
        self._mid = 1000

    async def send_message(self, **kw) -> dict:
        self._mid += 1
        self.sends.append(dict(kw, _mid=self._mid))
        return {"ok": True, "result": {"message_id": self._mid, "date": 1}}

    async def edit_message_text(self, **kw) -> dict:
        self.edits.append(dict(kw))
        return {"ok": True}

    async def delete_message(self, **kw) -> dict:
        self.deletes.append(dict(kw))
        return {"ok": True}

    async def answer_callback_query(self, callback_query_id, text=None) -> dict:
        self.answered.append((callback_query_id, text))
        return {"ok": True}

    @property
    def final_texts(self) -> list[str]:
        return [s.get("text", "") for s in self.sends]

    @property
    def user_visible_final(self) -> str:
        """Финальный видимый пользователю текст: ход финализирует ack
        ПРАВКОЙ сообщения (ack.final_edit) либо шлёт новое."""
        if self.edits:
            return self.edits[-1].get("text", "")
        return self.sends[-1].get("text", "") if self.sends else ""


# --- среда приложения --------------------------------------------------------

TENANT = "tenant_1"
CHAT_ID = "100000003"


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """Окружение: SQLite-файл на сценарий, кэши сброшены, гейты выставлены,
    фейк-канал и захваты подключены. Возвращает объект с ручками."""
    db = tmp_path / "func.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_PLANNER_ENABLED_TENANTS", TENANT)
    monkeypatch.setenv("SREDA_MESSAGE_QUEUE_ENABLED_TENANTS", "")  # = прод
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "100:test-token")
    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "f" * 64)
    monkeypatch.delenv("SREDA_ADMIN_TG_CHAT_ID", raising=False)
    # preflight требует сконфигурированный LLM ДО гейта планировщика;
    # ключ фиктивный — клиент только конструируется, вызовы перехвачены
    # (планировщик — ContextVar-шов, рот — стаб, сеть — блокировщик)
    monkeypatch.setenv("SREDA_MIMO_API_KEY", "func-test-not-a-key")
    # chat-скил приходит ПЛАГИНОМ (как на проде); реестр фич — lru_cache,
    # сбрасываем на сценарий, иначе протечка между тестами
    monkeypatch.setenv(
        "SREDA_FEATURE_MODULES", "sreda_feature_housewife_assistant.plugin",
    )

    from sreda.config.settings import get_settings
    from sreda.db.session import get_engine, get_session_factory
    from sreda.features.app_registry import get_feature_registry
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_feature_registry.cache_clear()

    from sqlalchemy import event
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401 — наполняет metadata ДО create_all
    import sreda.db.models.checklists  # noqa: F401 — #86: не в __init__
    engine = get_engine()

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # ход держит две сессии (inbound + runtime-граф): в WAL читатели
        # не блокируют писателя; busy_timeout страхует короткие гонки
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=15000")

    Base.metadata.create_all(engine)

    # сид: одобренный тенант с заполненным профилем (мимо онбординг-веток)
    from datetime import datetime, timezone
    from sreda.db.models.core import Tenant, User, Workspace
    from sreda.db.models.user_profile import TenantUserProfile
    from sreda.services.onboarding import mark_welcome_sent
    session = get_session_factory()()
    try:
        session.add(Tenant(id=TENANT, name="Func",
                           approved_at=datetime.now(timezone.utc)))
        session.add(Workspace(id="workspace_1", tenant_id=TENANT, name="W"))
        session.add(User(id="user_1", tenant_id=TENANT,
                         telegram_account_id=CHAT_ID))
        session.add(TenantUserProfile(
            id="tup_f", tenant_id=TENANT, user_id="user_1",
            display_name="Тест", address_form="ty",
        ))
        # подписка на chat-скил — без неё handlers отвечает апселлом
        # ДО гейта планировщика (механизм настоящий, не замокан)
        from datetime import timedelta
        from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
        now = datetime.now(timezone.utc)
        session.add(SubscriptionPlan(
            id="plan_f", plan_key="sreda_free",
            feature_key="housewife_assistant", title="Free", description="t",
            price_rub=0,
        ))
        session.flush()  # план до подписки (FK при включённом PRAGMA)
        session.add(TenantSubscription(
            id="sub_f", tenant_id=TENANT, plan_id="plan_f",
            feature_key="housewife_assistant", status="active",
            starts_at=now - timedelta(days=1),
            active_until=now + timedelta(days=30),
        ))
        session.flush()
        mark_welcome_sent(session, TENANT, "user_1")
        # онбординг-машина скила: complete, иначе ход остаётся за легаси
        # (гейт планировщика исключает follow-up-ходы — #120 R2 MEDIUM)
        from sreda.services.housewife_onboarding import (
            STATUS_COMPLETE, HousewifeOnboardingService,
        )
        ob = HousewifeOnboardingService(session)
        st = ob.get_raw_state(tenant_id=TENANT, user_id="user_1")
        st["status"] = STATUS_COMPLETE
        ob._persist(tenant_id=TENANT, user_id="user_1", state=st,
                    source="system")
        session.commit()
    finally:
        session.close()

    # биллинг-гейты: фокус на тракте, не на подписках/квотах. UsageLedger
    # вдобавок пишет SERIALIZABLE-апсертом из сессии графа — на SQLite это
    # тупик с открытой транзакцией хода (на проде Postgres MVCC).
    from sreda.services.entitlement_gate import EntitlementGate, GateResult
    monkeypatch.setattr(
        EntitlementGate, "check",
        lambda self, tenant_id: GateResult(
            allowed=True, reason="ok", plan_key="sreda_free",
            is_grandfathered=False),
    )
    from sreda.services.usage_ledger import UsageLedgerService
    monkeypatch.setattr(
        UsageLedgerService, "try_consume",
        lambda self, tenant_id, metric, amount, periods: True,
    )

    # фейк-канал в ОБЕ точки разрешения клиента (bind-at-import имена)
    fake_tg = FakeTelegramClient()
    import sreda.runtime.graph as graph_mod
    import sreda.services.telegram_inbound as ti
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: fake_tg)
    monkeypatch.setattr(graph_mod, "telegram_client_for", lambda *a, **k: fake_tg)

    # захват алертов в точках использования (прямые импорты)
    alerts: list[tuple] = []
    import sreda.runtime.planner_chat as pc
    import sreda.services.composer.breakdown_messages as bm
    monkeypatch.setattr(pc, "send_admin_alert",
                        lambda *a, **kw: alerts.append((a, kw)))
    monkeypatch.setattr(bm, "send_admin_alert",
                        lambda *a, **kw: alerts.append((a, kw)),
                        raising=False)
    # note_breakdown импортирует send_admin_alert ВНУТРИ функции из
    # admin_alerts (Codex R1 high) — глушим и первоисточник
    import sreda.services.admin_alerts as aa
    monkeypatch.setattr(aa, "send_admin_alert",
                        lambda *a, **kw: alerts.append((a, kw)))

    # стаб НИЖНЕГО вызова рта (реальный composer работает) — пишет, что
    # до него дошло, и возвращает детерминированный текст с фактами
    voice_calls: list[dict] = []
    import sreda.services.composer.llm_composer as lc

    def fake_voice(**kw):
        voice_calls.append(kw)
        data = kw.get("template_data") or {}
        seed = ""
        for a in (data.get("actions") or []):
            seed += str(a.get("user_visible_summary", ""))
        return SimpleNamespace(text=(seed or json.dumps(data, ensure_ascii=False))[:3500],
                               provider="fake", model="fake", latency_ms=1)

    monkeypatch.setattr(lc, "DEFAULT_LLM_COMPOSER", fake_voice)

    # перехват отсоединённых задач через ЛОКАЛЬНЫЙ шов
    tracked: list[asyncio.Task] = []

    def tracking_create_task(coro, **kw):
        t = asyncio.create_task(coro, **kw)
        tracked.append(t)
        return t

    monkeypatch.setattr(ti, "_create_task", tracking_create_task)
    import sreda.services.max_inbound as mi
    monkeypatch.setattr(mi, "_create_task", tracking_create_task)

    h = SimpleNamespace(
        tg=fake_tg, alerts=alerts, voice_calls=voice_calls,
        tracked_tasks=tracked, session_factory=get_session_factory,
        tenant=TENANT, chat_id=CHAT_ID,
    )
    yield h
    engine.dispose()  # Codex R1 (оба): WAL/SHM-хэндлы Windows
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_feature_registry.cache_clear()


# --- очередь записанных ответов планировщика --------------------------------

def make_planner_queue(*payloads):
    """Каждый элемент: dict (валидный план → json) или str (сырой мусор)."""
    from sreda.runtime.planner.llm import PlannerCallResult
    queue = [json.dumps(p, ensure_ascii=False) if isinstance(p, dict) else p
             for p in payloads]
    calls = {"n": 0}

    async def canned(prompt: str, **kw):  # noqa: ARG001
        i = min(calls["n"], len(queue) - 1)
        calls["n"] += 1
        return PlannerCallResult(
            raw_text=queue[i], latency_ms=5, provider="canned",
            model="canned", attempt_no=kw.get("attempt_no", calls["n"]),
            parsed_plan=None,
        )

    canned.calls = calls
    return canned


# --- прогон сценария ---------------------------------------------------------

@pytest_asyncio.fixture()
async def run_turn(harness):
    """Отправить текст в настоящий вебхук с заданной очередью ответов
    планировщика; дождаться хода (BackgroundTasks) и внутренних задач."""
    import httpx
    from sreda.main import create_app
    from sreda.runtime import planner_chat

    app = create_app()

    async def _run(text: str, planner_queue, update_id: int = 1):
        guard_called = {"v": False}

        async def guarded(prompt, **kw):
            guard_called["v"] = True
            return await planner_queue(prompt, **kw)

        token = planner_chat._planner_call_override.set(guarded)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/webhooks/telegram/sreda",
                    json={"update_id": update_id, "message": {
                        "message_id": update_id * 10,
                        "chat": {"id": int(harness.chat_id), "type": "private"},
                        "text": text,
                    }},
                )
            assert resp.status_code == 202, resp.text
        finally:
            planner_chat._planner_call_override.reset(token)
        # слой 2: доживаем отсоединённые задачи хода (ограниченно, явно)
        pending = [t for t in harness.tracked_tasks if not t.done()]
        if pending:
            done, not_done = await asyncio.wait(pending, timeout=5)
            assert not not_done, f"незавершённые задачи хода: {not_done}"
        # Codex R1 high: упавшая УЖЕ-done задача не должна проходить тихо
        for t in harness.tracked_tasks:
            assert not t.cancelled(), f"задача хода отменена: {t}"
            exc = t.exception()
            assert exc is None, f"задача хода упала: {t} -> {exc!r}"
        assert guard_called["v"], (
            f"записанный планировщик не был вызван; sends="
            f"{harness.tg.final_texts!r} edits="
            f"{[e.get('text') for e in harness.tg.edits]!r}"
        )
        return resp

    return _run


def db_session(harness):
    return harness.session_factory()()


def assert_happy_invariants(harness, *, must_contain=()):
    """Стандартные happy-инварианты (чеклист п.9)."""
    assert not harness.alerts, f"алерты на happy-пути: {harness.alerts}"
    final = harness.tg.user_visible_final
    assert final, "в канал не ушло ни одного сообщения"
    for needle in must_contain:
        assert needle in final, f"в ответе нет «{needle}»: {final!r}"
    for leak in ("${s", "list_checklists", "add_reminder", "None",
                 '"template_id"'):
        assert leak not in final, f"утечка «{leak}» в ответе: {final!r}"
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
    assert final not in BREAKDOWN_POOL, "happy-путь отдал «поломку»"
    # ровно ОДИН финальный ответ: либо одна правка ack, либо один non-ack send
    finals = len(harness.tg.edits) + max(0, len(harness.tg.sends) - 1)
    assert finals == 1, (
        f"финальных сообщений {finals} (edits={len(harness.tg.edits)}, "
        f"sends={len(harness.tg.sends)}) — должен быть ровно один"
    )
    # рот (нижний вызов) обязан был участвовать и видеть факты (#121)
    assert harness.voice_calls, "humanize-рот не вызывался на happy-пути"
    vc = harness.voice_calls[-1]
    assert vc.get("llm_prompt_key") == "humanize_result"
    vc_payload = json.dumps(vc.get("template_data", {}), ensure_ascii=False)
    for needle in must_contain:
        assert needle in vc_payload, f"факт «{needle}» не дошёл до рта"
    sess = db_session(harness)
    try:
        from sreda.db.models.core import InboundMessage, OutboxMessage
        rows = sess.query(InboundMessage).all()
        assert rows and all(r.processing_status == "processed" for r in rows)
        ob = sess.query(OutboxMessage).all()
        assert ob, "outbox пуст на happy-пути"
        assert all(o.status == "sent" for o in ob), (
            f"outbox-статусы: {[o.status for o in ob]}"
        )
        assert len(ob) == 1, f"дубль в outbox: {len(ob)} строк"
    finally:
        sess.close()
