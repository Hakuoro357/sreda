"""Route-level tests for the overview dashboard (#292, R1 fixes).

The central Phase A contract (high R1 MINOR → named test): ``GET /admin``
renders from the STORED snapshot — with every recompute/network helper
broken, the page must still return 200. Plus: normalizer coercion,
per-block fail-soft, protected upsert.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.admin import host_metrics as hm
from sreda.admin import overview_snapshot as ov
from sreda.admin import routes as admin_routes
from sreda.admin.auth import require_admin_token
from sreda.db.base import Base


@pytest.fixture()
def session_factory():
    # StaticPool: одно соединение на все потоки — иначе TestClient
    # (другой поток) получает НОВУЮ пустую in-memory БД.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # audit_log НЕ в sreda.db.models.__init__ и импортится лениво в роуте, поэтому
    # без явной регистрации таблицы нет → best-effort audit на GET /admin/ пишет
    # ERROR-трейс. Импортируем модель ДО create_all (как в tests/unit/conftest.py).
    import sreda.db.models.audit  # noqa: F401 — регистрирует таблицу audit_log
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def client(session_factory, monkeypatch):
    app = FastAPI()
    app.include_router(admin_routes.router)

    def _session_override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[require_admin_token] = lambda: "T"
    app.dependency_overrides[admin_routes._get_session] = _session_override
    # host-метрики без subprocess'ов в тестах
    monkeypatch.setattr(hm, "_systemctl", lambda args: None)
    monkeypatch.setattr(hm, "_read_text", lambda path: None)
    return TestClient(app)


def _boom(*a, **k):
    raise RuntimeError("must not be called on GET /admin")


def test_dashboard_renders_from_snapshot_without_recompute(
    client, session_factory, monkeypatch
):
    # снапшот подготовлен фоном (эмулируем)
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "llm_24h": {"calls": 7, "errors": 1, "error_rate_pct": 14.3, "slow": 0},
        "balances": [{"key": "openrouter", "label": "OpenRouter",
                      "status": "ok", "headline": "$12.40", "details": ""}],
    })
    s.close()
    # ВСЕ пересчётные/сетевые хелперы уронены — страница обязана жить
    monkeypatch.setattr(ov, "compute_overview", _boom)
    monkeypatch.setattr(ov, "refresh_overview", _boom)
    monkeypatch.setattr(
        "sreda.services.provider_balances.fetch_balances", _boom)
    monkeypatch.setattr(
        "sreda.admin.queries.get_cost_volume_summary", _boom)
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200
    assert "OpenRouter" in resp.text
    assert "снапшот обновлён" in resp.text


def test_dashboard_renders_when_no_snapshot(client):
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200
    assert "снапшот ещё не собран" in resp.text


def test_dashboard_full_snapshot_renders_all_sections(client, session_factory):
    # #297 (Codex high R1 MAJOR): позитивный smoke заполненных секций —
    # регресс шаблона (секция молча пропала под {% if %}) не пройдёт на 200-only.
    # 2026-07-03: адаптирован к фидбеку владельца — «медленные» из трейсов
    # (slow_turns вместо slow_recent), «Топ тенантов» УДАЛЁН, добавлены
    # виджеты «Пользователи»/«Покупки».
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "llm_24h": {"calls": 42, "errors": 2, "error_rate_pct": 4.8},
        "errors_recent": [{
            "at": "2026-07-02T10:15:00", "status": "failed", "error_code": "timeout",
            "task_type": "llm_call", "model": "mercury-2",
            "tenant_id": "t1", "feature_key": "housewife_assistant"}],
        "slow_turns": {"count_24h": 1, "recent": [{
            "at": "2026-07-02 11:00:03.100", "total_ms": 31500,
            "user_id": "user_tg_755682022", "channel": "max",
            "top_stage": "llm 28.0 с"}]},
        "users": {"total": 21, "new_today": 2, "new_7d": 5,
                  "active_24h": 8, "active_7d": 14, "active_30d": 19},
        "purchases": {"paid_tenants": 1, "orders_7d": 1, "sum_rub_7d": 500,
                      "orders_30d": 2, "sum_rub_30d": 800},
        "cost": {"day": {
            "priced_subtotal_usd": 1.23, "upper_subtotal_usd": 2.5, "calls": 40,
            "coverage_calls_pct": 95, "unpriced_calls": 2, "unpriced_tokens": 9000,
            "rows": [{"provider_key": "inception-mercury2", "model": "mercury-2",
                      "calls": 40, "prompt_tokens": 100, "completion_tokens": 10,
                      "priced": True, "est_usd": 1.23, "upper_usd": 2.5}],
            "unpriced_rows": [{"provider_key": "mimo", "model": "mimo-v2.5-pro",
                               "calls": 2, "prompt_tokens": 8000,
                               "completion_tokens": 1000, "priced": False}]}},
        "health": {"turns_total": 50, "runs_failed": 1, "inbound_stuck": 0,
                   "outbox_failed": 0, "breakdowns_shown": 2, "failures_total": 3},
        "balances": [{"key": "openrouter", "label": "OpenRouter", "status": "ok",
                      "headline": "$12.40", "details": ""}],
        # значения цепочки уникальны в payload'е (R2 субагент MINOR):
        # совпадение с model в errors/slow/cost не маскирует пропажу блока
        "llm_chain": {"primary": "chat-primary-x", "fallback": "osa-groq",
                      "planner": "planner-model-x"},
    })
    s.close()
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200
    html = resp.text
    # здоровье диалога
    assert "3 проблем" in html and "50 ходов" in html
    # деньги: priced-итог, покрытие, беспрайсовая строка
    assert "$1.23" in html and "покрытие 95" in html and "без прайса" in html
    # топ тенантов УБРАН (фидбек владельца 2026-07-03)
    assert "Топ тенантов" not in html
    # аудитория/активность (#304: единый виджет разбит на 2 плиточные карточки)
    # + покупки. Значение и подпись теперь в разных <div>. Заголовки карточек
    # рендерятся ВНЕ {% if snap.users %} (видны и при пустых данных), поэтому
    # пропажу данных под {% if %} ловят ЗНАЧЕНИЯ, а не заголовки; «+N» —
    # единственный со знаком «+» маркёр, уникальный для плиток Аудитории.
    assert "Аудитория" in html and "Активность" in html   # раздельные карточки (#304)
    assert ">+2<" in html and ">+5<" in html               # регистрации: +сегодня / +7 дней
    assert "оплативших тенантов" in html and "800 ₽" in html
    # активность DAU/WAU/MAU (ради этой секции была #304): подписи окон +
    # значения active_24h/7d/30d (внутри {% if %}) — секция не исчезнет молча (#307).
    # Формы «>подпись<» отсекают коллизию «сутки» с KPI-заголовком «Диалог за сутки».
    assert ">сутки<" in html and ">неделя<" in html and ">месяц<" in html
    assert ">8<" in html and ">14<" in html and ">19<" in html  # active 24h/7d/30d
    # балансы провайдеров
    assert "OpenRouter" in html and "$12.40" in html
    # ошибки и медленные за сутки (из трейсов)
    assert "timeout" in html and "10:15" in html
    assert "31.5 с" in html and "llm 28.0 с" in html
    # LLM-цепочка
    assert "chat-primary-x" in html and "osa-groq" in html
    assert "planner-model-x" in html


def test_dashboard_survives_garbage_snapshot(client, session_factory):
    # битый/устаревший payload (не та схема, мусорные типы) → 200, не 500
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "llm_24h": {"calls": "мусор", "errors": None},
        "cost": {"day": {"priced_subtotal_usd": "NaN-строка", "rows": "не список"}},
        "slow_recent": [{"latency_ms": "медленно"}, "не dict"],
        "balances": "тоже не список",
        "top_tenants": {"by_spend": [{"est_usd": "x"}]},
    })
    s.close()
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200


def test_llm_money_renders_from_snapshot(client, session_factory, monkeypatch):
    """Фаза B: /admin/llm-money читает ТОТ ЖЕ снапшот, ноль пересчёта."""
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "cost": {"day": {"priced_subtotal_usd": 0.61, "upper_subtotal_usd": 0.7,
                         "calls": 40, "coverage_calls_pct": 93,
                         "unpriced_calls": 0, "unpriced_tokens": 0,
                         "rows": [], "unpriced_rows": []}},
        "providers": [{
            "key": "inception", "label": "Inception (Mercury)",
            "balance": {"status": "not_supported",
                        "headline": "нет billing API", "details": "консоль"},
            "spend": {"day": {"est_usd": 0.15, "calls": 30, "unpriced_tokens": 0},
                      "month": {"est_usd": 3.2, "calls": 700, "unpriced_tokens": 0}},
            "errors_24h": {"calls": 30, "errors": 1, "rate_pct": 3.3},
            "models": [{"model": "mercury-2", "roles": "диалог",
                        "calls": 700, "priced": True, "est_usd": 3.2, "tokens": 0}],
        }, {
            "key": "yandex", "label": "Yandex (STT)", "balance": None,
            "spend": {"month": {"est_usd": 0.0, "calls": 60, "unpriced_tokens": 90000}},
            "errors_24h": {"calls": 5, "errors": 0, "rate_pct": 0.0},
            "models": [{"model": "stt-general", "roles": "распознавание речи",
                        "calls": 60, "priced": False, "est_usd": None, "tokens": 90000}],
        }],
    })
    s.close()
    monkeypatch.setattr(ov, "compute_overview", _boom)  # не пересчитывает
    resp = client.get("/admin/llm-money", params={"token": "T"})
    assert resp.status_code == 200
    html = resp.text
    assert "Inception (Mercury)" in html and "нет billing API" in html
    assert "mercury-2" in html and "диалог" in html
    assert "Yandex (STT)" in html and "распознавание речи" in html
    assert "90 000 ток." in html          # беспрайсовое — токенами, рус. разделитель
    assert "ошибок за 24 ч: 1" in html


def test_llm_money_survives_malformed_provider_spend(client, session_factory):
    """R2 medium: битый spend (нет est_usd, мусорные типы) → 200, не 500
    (нормализатор прошивает est_usd=0.0 через _num-default)."""
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "providers": [{"key": "x", "label": "X",
                       "spend": {"day": {"calls": 1}},          # без est_usd
                       "errors_24h": "мусор",
                       "models": [{"model": "m", "est_usd": "bad"}]},
                      "не dict"],
    })
    s.close()
    resp = client.get("/admin/llm-money", params={"token": "T"})
    assert resp.status_code == 200


def test_llm_money_renders_empty(client):
    resp = client.get("/admin/llm-money", params={"token": "T"})
    assert resp.status_code == 200
    assert "снапшот ещё не собран" in resp.text


def test_refresh_endpoint_delegates_and_redirects(client, monkeypatch):
    called = {}
    monkeypatch.setattr(
        ov, "refresh_overview",
        lambda sf, st: called.setdefault("yes", True) or True,
    )
    resp = client.post(
        "/admin/refresh-snapshot", params={"token": "T"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert called.get("yes") is True
    assert "refresh=err" not in resp.headers["location"]


def test_normalize_overview_coerces_garbage():
    norm = ov.normalize_overview({
        "llm_24h": {"calls": "x", "errors": 2, "error_rate_pct": "y"},
        "cost": {"day": {"priced_subtotal_usd": "z", "rows": [
            {"provider_key": 5, "calls": "7", "priced": 1, "est_usd": "bad"},
            "не dict",
        ]}},
        "errors_recent": [{"at": 123}, "мусор"],
        "slow_turns": {"count_24h": "x", "recent": [{"total_ms": "y"}, "не dict"]},
        "users": {"total": "x", "new_today": 2, "new_7d": None},
        "purchases": {"paid_tenants": True, "orders_7d": 1, "sum_rub_7d": "z",
                      "orders_30d": None, "sum_rub_30d": 800},
        "llm_chain": {"primary": 9},
    })
    assert norm["llm_24h"] == {"calls": 0, "errors": 2, "error_rate_pct": 0.0}
    assert norm["slow_turns"]["count_24h"] == 0
    assert norm["slow_turns"]["recent"][0]["total_ms"] == 0
    assert norm["users"] == {"total": 0, "new_today": 2, "new_7d": 0,
                             "active_24h": 0, "active_7d": 0, "active_30d": 0}
    assert norm["purchases"]["paid_tenants"] == 0  # bool не число
    assert norm["purchases"]["sum_rub_30d"] == 800
    day = norm["cost"]["day"]
    assert day["priced_subtotal_usd"] == 0.0
    assert day["rows"][0]["provider_key"] == ""  # coerced, не 500
    assert day["rows"][0]["est_usd"] is None      # не выдумываем деньги
    # R3: bool — подкласс int; est_usd=True НЕ должен стать $0.0/priced
    norm_b = ov.normalize_overview({
        "cost": {"day": {"rows": [{"priced": True, "est_usd": True}]}}})
    row_b = norm_b["cost"]["day"]["rows"][0]
    assert row_b["est_usd"] is None and row_b["priced"] is False
    assert len(norm["errors_recent"]) == 1 and norm["errors_recent"][0]["at"] == ""
    assert norm["llm_chain"]["primary"] == ""


def test_normalize_overview_keeps_valid_values():
    payload = {
        "llm_24h": {"calls": 4, "errors": 2, "error_rate_pct": 50.0, "slow": 1},
        "cost": {"day": {
            "priced_subtotal_usd": 0.61, "upper_subtotal_usd": 0.7,
            "calls": 10, "coverage_calls_pct": 93,
            "unpriced_calls": 1, "unpriced_tokens": 500,
            "rows": [{"provider_key": "p", "model": "m", "calls": 3,
                      "prompt_tokens": 1, "completion_tokens": 2,
                      "priced": True, "est_usd": 0.5, "upper_usd": 0.6}],
            "unpriced_rows": [],
        }},
    }
    norm = ov.normalize_overview(payload)
    assert norm["llm_24h"]["error_rate_pct"] == 50.0
    assert norm["cost"]["day"]["rows"][0]["est_usd"] == 0.5
    assert norm["cost"]["day"]["coverage_calls_pct"] == 93


def test_db_error_in_block_does_not_prevent_store(session_factory, monkeypatch):
    # R2 (high+medium MAJOR): сбойный SQL в блоке НЕ должен помешать
    # сохранению частичного снапшота (rollback в _safe).
    import sqlalchemy.exc

    def _sql_boom(*a, **k):
        raise sqlalchemy.exc.OperationalError("SELECT boom", {}, Exception("x"))

    # _cost_reports теперь зовёт get_spend_by_model (скользящие окна) —
    # мокаем её, чтобы уронить cost-блок (2026-07-03).
    monkeypatch.setattr(
        "sreda.admin.queries.get_spend_by_model", _sql_boom)
    monkeypatch.setattr(
        ov, "_balances_block", lambda settings: [])
    ok = ov.refresh_overview(
        session_factory,
        SimpleNamespace(chat_provider="p", chat_fallback_provider=""),
    )
    assert ok is True  # частичный снапшот СОХРАНЁН
    s = session_factory()
    loaded, at = ov.load_snapshot(s, ov.KEY_OVERVIEW)
    s.close()
    assert at is not None
    assert loaded["cost"] == {}          # сбойный блок деградировал
    assert "llm_24h" in loaded            # остальные собрались


def test_garbage_priced_row_renders_200(client, session_factory):
    # R2 (high MAJOR): priced=True без числа НЕ должен дать 500 —
    # нормализатор обязан сбросить priced.
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "cost": {"day": {
            "priced_subtotal_usd": 1.0, "upper_subtotal_usd": 1.0,
            "calls": 1, "coverage_calls_pct": 50,
            "unpriced_calls": 0, "unpriced_tokens": 0,
            "rows": [{"provider_key": "p", "model": "m", "calls": 1,
                      "prompt_tokens": 1, "completion_tokens": 1,
                      "priced": True, "est_usd": None}],
            "unpriced_rows": [],
        }},
    })
    s.close()
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200


def test_cost_transform_failure_does_not_kill_snapshot(session_factory, monkeypatch):
    """R1 Фазы B (high MAJOR): сбой ТРАНСФОРМАЦИИ отчёта (не запроса) —
    cost={}, остальной снапшот жив."""
    monkeypatch.setattr(ov, "_cost_block", _boom)
    monkeypatch.setattr(ov, "_balances_block", lambda st: [])
    s = session_factory()
    payload = ov.compute_overview(
        s, SimpleNamespace(chat_provider="p", chat_fallback_provider=""))
    s.close()
    assert payload["cost"] == {}
    assert "llm_24h" in payload  # остальное собралось


def test_normalize_poisoned_priced_flag():
    """R1 Фазы B (high MAJOR): priced=1/'yes' из битого снапшота НЕ должен
    показать доллары — только настоящий True + числовой est."""
    norm = ov.normalize_overview({
        "cost": {"day": {"rows": [
            {"priced": 1, "est_usd": 5.0},        # int-мусор
            {"priced": "yes", "est_usd": 5.0},    # строка-мусор
            {"priced": True, "est_usd": 5.0},     # честный
        ]}},
        "providers": [{"key": "x", "label": "X",
                       "models": [{"model": "m", "priced": 1, "est_usd": 5.0}]}],
    })
    rows = norm["cost"]["day"]["rows"]
    assert [r["priced"] for r in rows] == [False, False, True]
    assert norm["providers"][0]["models"][0]["priced"] is False


def test_compute_overview_per_block_failsoft(session_factory, monkeypatch):
    # уроненный блок → пустой дефолт, остальные блоки живут
    monkeypatch.setattr(ov, "_llm_24h_block", _boom)
    monkeypatch.setattr(
        ov, "_balances_block",
        lambda settings: [{"key": "k", "label": "L", "status": "ok",
                           "headline": "h", "details": ""}],
    )
    s = session_factory()
    payload = ov.compute_overview(
        s, SimpleNamespace(chat_provider="p", chat_fallback_provider=""))
    s.close()
    assert payload["llm_24h"] == {}          # деградировал
    assert payload["balances"][0]["key"] == "k"  # остальное собралось
    assert "cost" in payload
