"""Phase 8 + R3 codex review — `_require_miniapp_auth` MAX branch tests.

Тестируем сам auth-dependency функцию напрямую, без TestClient/full app
(избегая `python-multipart` import error в test env'е).

Покрывается:
- channel='max' happy path with mocked validate_max_init_data
- 400 на unknown platform
- 401 на invalid initData
- Lazy provision создаёт MAX tenant
- Race condition fallback (IntegrityError → re-resolve)
- max_chat_id refresh на resolved-path
- Admin alert через BackgroundTasks (codex R2 fix)
- Default channel=telegram backward-compat
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="tenant_max_111", name="MaxUser"))
    sess.add(User(
        id="user_max_111", tenant_id="tenant_max_111",
        max_account_id="111", max_chat_id="22",
    ))
    # sreda_free тариф — прод-предпосылка (0041); провижн без него бросает (R2-2).
    from sreda.db.models.billing import SubscriptionPlan
    sess.add(SubscriptionPlan(
        id="plan_free", plan_key="sreda_free", feature_key="housewife_assistant",
        title="Free", description="", price_rub=0,
    ))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(autouse=True)
def _identity_resolve_uses_db(db_session, monkeypatch):
    """#138 Ф5-5b: резолв личности идёт под ОТДЕЛЬНОЙ identity-сессией
    (privileged_session), а не под сессией вызывающего — после флипа DSN это
    identity-роль. В юнит-тесте стабаем эту сессию на ту же sqlite-БД, где
    засеяны данные, чтобы _resolve_direct видел их (диалект sqlite → ORM-путь)."""
    from contextlib import contextmanager

    import sreda.db.session as dbs

    @contextmanager
    def _stub(arg):
        yield db_session

    monkeypatch.setattr(dbs, "privileged_session", _stub)
    monkeypatch.setattr(dbs, "tenant_session", _stub)


def _make_request(*, platform: str | None = "max", auth: str = "tma fake_init_data"):
    """Build a minimal FastAPI Request mock с query_params."""
    from starlette.datastructures import Headers, QueryParams

    request = MagicMock()
    request.headers = Headers({"authorization": auth, "user-agent": "test/1.0"})
    if platform is not None:
        request.query_params = QueryParams(f"platform={platform}")
    else:
        request.query_params = QueryParams("")
    request.url = MagicMock()
    request.url.path = "/miniapp/api/v1/summary"
    return request


def _mock_max_user(*, user_id="111", chat_id="22", first_name="Test"):
    from sreda.services.max_auth import MaxWebAppUser
    return MaxWebAppUser(
        max_user_id=user_id, max_chat_id=chat_id,
        first_name=first_name, username=None, start_param=None,
    )


def test_max_resolved_user_returns_context(db_session, monkeypatch):
    """Existing MAX user → context с channel='max', tenant_id, user_id."""
    from sreda.api.routes import miniapp as mi

    monkeypatch.setattr(
        mi, "validate_max_init_data",
        lambda *a, **k: _mock_max_user(),
    )
    monkeypatch.setattr(
        "sreda.api.routes.miniapp.get_settings",
        lambda: MagicMock(
            max_bot_token="MAX_TOK", telegram_bot_token="TG_TOK",
        ),
    )

    bg_tasks = BackgroundTasks()
    ctx = mi._require_miniapp_auth(
        request=_make_request(platform="max"),
        background_tasks=bg_tasks,
        session=db_session,
    )

    assert ctx.channel == "max"
    assert ctx.account_id == "111"
    assert ctx.tenant_id == "tenant_max_111"
    assert ctx.user_id == "user_max_111"


def test_unknown_platform_returns_400(db_session, monkeypatch):
    """?platform=whatsapp → 400."""
    from sreda.api.routes import miniapp as mi

    with pytest.raises(HTTPException) as exc:
        mi._require_miniapp_auth(
            request=_make_request(platform="whatsapp"),
            background_tasks=BackgroundTasks(),
            session=db_session,
        )
    assert exc.value.status_code == 400
    assert "unknown platform" in str(exc.value.detail).lower()


def test_max_invalid_init_data_returns_401(db_session, monkeypatch):
    """Bad initData → 401 invalid_init_data."""
    from sreda.api.routes import miniapp as mi
    from sreda.services.max_auth import MaxInitDataError

    def _raise(*a, **k):
        raise MaxInitDataError("bad sig")

    monkeypatch.setattr(mi, "validate_max_init_data", _raise)
    monkeypatch.setattr(
        "sreda.api.routes.miniapp.get_settings",
        lambda: MagicMock(max_bot_token="MAX_TOK"),
    )

    with pytest.raises(HTTPException) as exc:
        mi._require_miniapp_auth(
            request=_make_request(platform="max"),
            background_tasks=BackgroundTasks(),
            session=db_session,
        )
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_init_data"


def test_max_chat_id_refreshed_on_change(db_session, monkeypatch):
    """Если max_chat_id в initData != stored → update + commit (codex R1 #4)."""
    from sreda.api.routes import miniapp as mi

    monkeypatch.setattr(
        mi, "validate_max_init_data",
        lambda *a, **k: _mock_max_user(chat_id="99_NEW"),
    )
    monkeypatch.setattr(
        "sreda.api.routes.miniapp.get_settings",
        lambda: MagicMock(max_bot_token="MAX_TOK"),
    )

    ctx = mi._require_miniapp_auth(
        request=_make_request(platform="max"),
        background_tasks=BackgroundTasks(),
        session=db_session,
    )

    # Re-fetch user — chat_id обновился
    db_session.expire_all()
    user = db_session.get(User, "user_max_111")
    assert user.max_chat_id == "99_NEW"


def test_max_lazy_provision_for_new_user(db_session, monkeypatch):
    """Unknown max_account_id → ensure_max_user_bundle creates tenant."""
    from sreda.api.routes import miniapp as mi

    monkeypatch.setattr(
        mi, "validate_max_init_data",
        lambda *a, **k: _mock_max_user(user_id="999_NEW"),
    )
    monkeypatch.setattr(
        "sreda.api.routes.miniapp.get_settings",
        lambda: MagicMock(max_bot_token="MAX_TOK"),
    )

    bg_tasks = BackgroundTasks()
    ctx = mi._require_miniapp_auth(
        request=_make_request(platform="max"),
        background_tasks=bg_tasks,
        session=db_session,
    )

    assert ctx.account_id == "999_NEW"
    assert ctx.channel == "max"
    # Tenant должен быть создан
    assert db_session.get(Tenant, ctx.tenant_id) is not None
    # Admin alert должен быть scheduled (codex R2 fix — BackgroundTasks)
    assert len(bg_tasks.tasks) >= 1, "expected admin-alert task scheduled"


def test_max_race_condition_falls_back_to_resolve(db_session, monkeypatch):
    """codex R3 fix: parallel первый mini-app load — IntegrityError → resolve."""
    from sreda.api.routes import miniapp as mi

    monkeypatch.setattr(
        mi, "validate_max_init_data",
        lambda *a, **k: _mock_max_user(user_id="111"),  # existing user
    )
    monkeypatch.setattr(
        "sreda.api.routes.miniapp.get_settings",
        lambda: MagicMock(max_bot_token="MAX_TOK"),
    )

    # Force race: первый resolve возвращает None, ensure_max_user_bundle
    # бросает IntegrityError, второй resolve находит row (предположительно
    # созданную параллельным request'ом).
    call_count = {"resolve": 0}
    real_resolve = mi.resolve_tenant_from_max_account_id

    def _flaky_resolve(session, account_id):
        call_count["resolve"] += 1
        if call_count["resolve"] == 1:
            return None  # first call: not found
        return real_resolve(session, account_id)  # second: found by other req

    monkeypatch.setattr(mi, "resolve_tenant_from_max_account_id", _flaky_resolve)

    def _raise_integrity(*a, **k):
        raise IntegrityError("dup PK", None, None)

    monkeypatch.setattr(mi, "ensure_max_user_bundle", _raise_integrity)

    ctx = mi._require_miniapp_auth(
        request=_make_request(platform="max"),
        background_tasks=BackgroundTasks(),
        session=db_session,
    )

    assert ctx.channel == "max"
    assert ctx.tenant_id == "tenant_max_111"
    assert call_count["resolve"] == 2, "expected fallback re-resolve"


def test_default_platform_telegram_backward_compat(db_session, monkeypatch):
    """Без ?platform= → defaults to telegram (legacy curl/тесты)."""
    from sreda.api.routes import miniapp as mi
    from sreda.services.telegram_auth import TelegramInitDataError

    # TG validator падает (no real signature), но мы проверяем что dispatch
    # пошёл в TG branch (не MAX) — error должен быть 401.
    def _raise(*a, **k):
        raise TelegramInitDataError("test bad sig")

    # Phase 7: miniapp now uses validate_telegram_init_data_any_bot (multi-bot)
    monkeypatch.setattr(mi, "validate_telegram_init_data_any_bot", _raise)
    monkeypatch.setattr(
        "sreda.api.routes.miniapp.get_settings",
        lambda: MagicMock(
            telegram_bot_token="TG_TOK",
            home_bot_token=None,
            telegram_bot_username=None,
            telegram_miniapp_shortname=None,
            home_bot_username=None,
            home_miniapp_shortname=None,
            home_bot_signup_open=True,
            system_default_bot_key="sreda",
            admin_bot_key="sreda",
        ),
    )

    with pytest.raises(HTTPException) as exc:
        mi._require_miniapp_auth(
            request=_make_request(platform=None),  # NO platform query
            background_tasks=BackgroundTasks(),
            session=db_session,
        )
    # 401 means we entered TG branch and validator was called (что хотели)
    assert exc.value.status_code == 401
