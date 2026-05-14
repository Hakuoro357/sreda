"""R-18 regression: ReplyButtonService.resolve_token must COMMIT, not flush.

Production incident 2026-05-12 19:46 UTC — 2 connections idle in
transaction 48 минут на UPDATE reply_button_cache → DB pool starvation →
/admin/users 504 Gateway Timeout.

Root cause: `resolve_token` использовал `self.session.flush()` вместо
`self.session.commit()`. Flush отправлял UPDATE в DB (row lock acquired)
но транзакцию НЕ закрывал. Если caller exception'ил до commit'а или
session lifecycle ломался — row lock висел indefinitely.

Sibling methods `create_tokens` + `purge_expired` уже используют commit
(см. comment в create_tokens line 92-96).

Fix: change flush → commit в resolve_token. Эти тесты — regression guard.
"""

from __future__ import annotations

import inspect

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.reply_buttons import ReplyButtonCache
from sreda.services.reply_buttons import ReplyButtonService


def test_resolve_token_commits_persists_across_sessions() -> None:
    """После resolve_token, row.used_at должен быть видим из независимой
    session — доказывает что commit happened, не просто flush.

    With flush(): UPDATE видим только внутри той же transaction. Fresh
    session (новое connection) НЕ видит изменения до commit'а.
    With commit(): UPDATE durably committed, видим всем.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    # Step 1: create token (uses commit per fix in create_tokens)
    s1 = SessionFactory()
    try:
        svc1 = ReplyButtonService(s1)
        pairs = svc1.create_tokens(
            tenant_id="tenant_1",
            user_id="user_1",
            labels=["Имя"],
        )
        assert len(pairs) == 1
        token, label = pairs[0]
        assert label == "Имя"
    finally:
        s1.close()

    # Step 2: resolve token в SECOND session (simulates webhook flow)
    s2 = SessionFactory()
    try:
        svc2 = ReplyButtonService(s2)
        resolved = svc2.resolve_token(
            tenant_id="tenant_1",
            user_id="user_1",
            token=token,
        )
        assert resolved == "Имя"
    finally:
        s2.close()

    # Step 3: открыть THIRD fresh session — used_at должно быть видимо.
    # Если resolve_token использует flush() — used_at НЕ committed, и
    # fresh session видит row.used_at = None.
    s3 = SessionFactory()
    try:
        row = s3.get(ReplyButtonCache, token)
        assert row is not None, "token row должен сохраниться в DB"
        assert row.used_at is not None, (
            "После resolve_token, row.used_at должен быть committed. "
            "Fresh session видит None → resolve_token использует flush() "
            "вместо commit() (R-18 regression)."
        )
    finally:
        s3.close()


def test_resolve_token_source_uses_commit_not_flush() -> None:
    """Static check: source code resolve_token содержит session.commit(),
    не session.flush() для финального persistence.

    Защита от regression — partial revert может вернуть flush обратно.
    """
    src = inspect.getsource(ReplyButtonService.resolve_token)
    assert "self.session.commit()" in src, (
        "resolve_token должен вызывать self.session.commit() — иначе "
        "row lock leak (R-18 incident 2026-05-12)."
    )


def test_resolve_token_closes_transaction_behaviorally() -> None:
    """Codex r1 MINOR: static getsource check недостаточно — нужен
    behavioral test что transaction реально закрыта после resolve_token.

    Check via session.in_transaction() flag — после успешного commit'а
    SQLAlchemy autobegin отключён до следующей operation, in_transaction
    должно вернуть False (или fresh transaction которая еще не started).
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    s1 = SessionFactory()
    try:
        svc = ReplyButtonService(s1)
        pairs = svc.create_tokens(
            tenant_id="tenant_1",
            user_id="user_1",
            labels=["Имя"],
        )
        token, _ = pairs[0]
    finally:
        s1.close()

    s2 = SessionFactory()
    try:
        svc = ReplyButtonService(s2)
        resolved = svc.resolve_token(
            tenant_id="tenant_1",
            user_id="user_1",
            token=token,
        )
        assert resolved == "Имя"
        # После commit() SQLAlchemy 2.x session автоматически закрывает
        # current transaction (begin-on-demand). Следующая operation
        # начала бы новую. Проверяем что НЕТ открытой tx сейчас.
        assert not s2.in_transaction(), (
            "После resolve_token session всё ещё в открытой transaction — "
            "значит commit() не отработал или autobegin re-opened."
        )
    finally:
        s2.close()


def test_resolve_token_returns_none_on_already_used() -> None:
    """Regression sanity: повторный resolve того же token → None.

    Защищает от regression где commit-fix мог что-то сломать в логике.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    s1 = SessionFactory()
    try:
        svc = ReplyButtonService(s1)
        pairs = svc.create_tokens(
            tenant_id="tenant_1",
            user_id="user_1",
            labels=["Имя"],
        )
        token, _ = pairs[0]
    finally:
        s1.close()

    # First resolve — succeeds, marks used_at
    s2 = SessionFactory()
    try:
        svc = ReplyButtonService(s2)
        first = svc.resolve_token(
            tenant_id="tenant_1",
            user_id="user_1",
            token=token,
        )
        assert first == "Имя"
    finally:
        s2.close()

    # Second resolve — should return None (already used)
    s3 = SessionFactory()
    try:
        svc = ReplyButtonService(s3)
        second = svc.resolve_token(
            tenant_id="tenant_1",
            user_id="user_1",
            token=token,
        )
        assert second is None, (
            "Повторный resolve того же token должен вернуть None "
            "(защита от replay)."
        )
    finally:
        s3.close()
