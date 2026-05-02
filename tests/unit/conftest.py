"""Shared pytest fixtures for unit tests.

Provides a deterministic default ``SREDA_ENCRYPTION_KEY`` so tests that
touch encrypted columns (``AssistantMemory.content``,
``TenantUserSkillConfig.skill_params_json``, etc.) don't have to set the
env var themselves. Tests that need a specific key value (encryption
rotation cases, etc.) use ``monkeypatch.setenv`` inside the test and
override this default.

Also provides ``seed_telegram_user`` — shared factory for the
Tenant + Workspace + User + TenantUserProfile boilerplate that's
repeated across 5+ test files. New tests should use this factory
instead of inlining ``session.add(Tenant(...)); session.add(...)``.
Existing tests can be migrated gradually.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class SeededTelegramUser:
    """Result of ``seed_telegram_user`` — opaque ids returned to caller.

    Test code reads these to pass into payload builders / assertions.
    Frozen so a test that mutates the dataclass gets a clear error.
    """

    chat_id: str
    tenant_id: str
    workspace_id: str
    user_id: str
    profile_id: str | None  # None when profile=False


def seed_telegram_user(
    session: "Session",
    *,
    chat_id: str = "100000003",
    tenant_id: str = "tenant_1",
    workspace_id: str | None = None,
    user_id: str = "user_1",
    tenant_name: str = "Test Tenant",
    workspace_name: str = "Test Workspace",
    approved: bool = True,
    profile: bool = True,
    profile_id: str = "tup_test",
    profile_display_name: str = "Test User",
    profile_address_form: str = "ty",
) -> SeededTelegramUser:
    """Create Tenant + Workspace + User + (optional) TenantUserProfile.

    Replaces the 6-10 lines of ``session.add(Tenant(...))`` boilerplate
    that's been copy-pasted into webhook / long-poll / memory tests.

    Defaults match the historical pattern in ``test_telegram_webhook.py``
    so tests can adopt this factory without re-writing assertions.

    Args:
        session: SQLAlchemy session, must already have schema applied.
        chat_id: Telegram chat_id (string). Default historical fixture.
        approved: When True, sets ``Tenant.approved_at = now()``. When
            False, leaves it NULL — for testing the pending-approval gate.
        profile: When True, also creates a TenantUserProfile with
            ``display_name`` / ``address_form`` filled in. Tests that
            exercise the wizard / pending-bot path should set False.

    Returns:
        ``SeededTelegramUser`` with all created ids — pass these into
        payloads or query filters.

    Note: the caller is responsible for ``session.commit()`` after.
    Returning before commit lets the caller add additional rows in the
    same transaction.
    """
    # Imports are local — keeps conftest top-level light and avoids
    # circular-import surprises when tests are collected before app code.
    from sreda.db.models.core import Tenant, User, Workspace
    from sreda.db.models.user_profile import TenantUserProfile

    if workspace_id is None:
        workspace_id = f"workspace_tg_{chat_id}"

    approved_at = datetime.now(timezone.utc) if approved else None

    session.add(
        Tenant(id=tenant_id, name=tenant_name, approved_at=approved_at)
    )
    session.add(
        Workspace(id=workspace_id, tenant_id=tenant_id, name=workspace_name)
    )
    session.add(
        User(id=user_id, tenant_id=tenant_id, telegram_account_id=chat_id)
    )

    actual_profile_id: str | None = None
    if profile:
        actual_profile_id = profile_id
        session.add(
            TenantUserProfile(
                id=profile_id,
                tenant_id=tenant_id,
                user_id=user_id,
                display_name=profile_display_name,
                address_form=profile_address_form,
            )
        )

    return SeededTelegramUser(
        chat_id=chat_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        profile_id=actual_profile_id,
    )


_DEFAULT_TEST_KEY = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode("ascii")


@pytest.fixture(autouse=True)
def _default_encryption_key(monkeypatch):
    """Set a stable dummy AES-256 key for the whole test suite.

    Required because encrypted ORM columns (``EncryptedString``) call
    into ``services.encryption.get_encryption_service`` on every
    read/write — without a configured key the call raises
    ``EncryptionConfigError`` and the test crashes long before the
    assertion.

    ``monkeypatch`` makes the env var live only for the duration of one
    test, so parallelism and teardown stay clean. The fixture uses
    ``setdefault`` semantics — tests that already set their own key
    through their own monkeypatch calls win (pytest applies their
    patches AFTER this fixture yields).
    """
    # Only inject if not already set by an outer fixture / env.
    if not os.environ.get("SREDA_ENCRYPTION_KEY"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _DEFAULT_TEST_KEY)
    # Same for the key_id — default matches the value used in tests
    # that historically hard-coded "primary".
    if not os.environ.get("SREDA_ENCRYPTION_KEY_ID"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "primary")
    # 152-ФЗ обезличивание Часть 1 (2026-04-27): hash tg_id требует
    # salt'а; иначе services.tg_account_hash.hash_tg_account падает
    # RuntimeError'ом, и любой test, который проходит через
    # find_user_by_chat_id или ensure_telegram_user_bundle, ломается
    # при импорте. Стабильный test-salt — детерминирует hash.
    if not os.environ.get("SREDA_TG_ACCOUNT_SALT"):
        monkeypatch.setenv(
            "SREDA_TG_ACCOUNT_SALT",
            "test-salt-for-unit-tests-do-not-use-in-prod",
        )

    # Clear the LRU-cached EncryptionService / Settings so the new env
    # vars take effect for this test's session.
    from sreda.config.settings import get_settings
    from sreda.services.encryption import get_encryption_service

    get_settings.cache_clear()
    get_encryption_service.cache_clear()
    yield
    get_settings.cache_clear()
    get_encryption_service.cache_clear()


@pytest.fixture(autouse=True)
def _clear_module_level_loop_bound_state():
    """Clear module-level state that's tied to a specific asyncio event loop.

    Background (пункт 8.1 tomorrow-plan): pytest-asyncio creates a fresh
    event loop per test. But several modules cache loop-bound objects
    (asyncio.Lock, httpx.AsyncClient with its own loop) at module level.
    When the next test creates a NEW loop, those cached objects belong
    to the DEAD loop — `lock.acquire()` blocks forever, `client.post()`
    raises RuntimeError 'attached to a different loop'.

    Concrete leak observed 2026-05-02:
    `tests/unit/test_telegram_long_poll.py` exercises
    `handle_telegram_update` which populates
    `services.telegram_inbound._TENANT_LOCKS["tenant_1"]` with an
    asyncio.Lock bound to that test's loop. Subsequent
    `tests/unit/test_telegram_webhook.py` runs handle_telegram_update
    again for the same tenant — picks up the stale Lock — its
    `_process_approved_turn` background task blocks on `async with
    tenant_lock` forever — `_wait_for` test helper times out at 5s.

    Fix: clear both module dicts after each test. Loop-bound objects
    become unreachable, GC reclaims them, next test creates fresh ones.
    `httpx.AsyncClient` instances in `_CLIENT_POOL` should ideally be
    `await client.aclose()`'d, but that requires a running loop here
    and the warning at GC time ("Unclosed client session") is benign in
    tests — accepted noise vs the alternative of test deadlocks.
    """
    yield
    # Imports are local — each module costs ~10-50ms first time, and
    # we only pay it for tests that actually touched these modules.
    try:
        from sreda.services.telegram_inbound import _TENANT_LOCKS
        _TENANT_LOCKS.clear()
    except ImportError:
        pass
    try:
        from sreda.integrations.telegram.client import _CLIENT_POOL
        _CLIENT_POOL.clear()
    except ImportError:
        pass
