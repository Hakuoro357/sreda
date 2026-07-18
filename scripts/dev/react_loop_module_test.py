"""Локальный тест ГЕЙТ-ПУТИ: гоняем РЕАЛЬНЫЙ модуль sreda.runtime.react_loop
(handle_turn) как телеграм — 3 отдельных сообщения, состояние через singleton.

Проверяет усиления: cancel сам подтверждает (interrupt внутри tool), idempotent,
tenant/user-guard. Прод НЕ тронут; живой Mercury; локальная SQLite.

    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python scripts/dev/react_loop_module_test.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# Импортируем sreda ИЗ WORKTREE (там новый react_loop.py), не из editable-install.
sys.path.insert(0, "C:/pro/sreda-wt-react-loop/src")

_SEC = Path("C:/pro/vex-assistant/sreda/.secrets")
_DB = Path(tempfile.gettempdir()) / "react_loop_module_test.db"
if _DB.exists():
    _DB.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef").decode("ascii")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "react_loop_module_salt_not_prod")
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_SEC / "inception.txt")
os.environ.setdefault("SREDA_MIMO_API_KEY_FILE", str(_SEC / "mimo_api_key.txt"))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings  # noqa: E402
get_settings.cache_clear()
from sreda.db.base import Base  # noqa: E402
from sreda.db.models import Assistant, Tenant, User, Workspace  # noqa: E402
from sreda.db.models.housewife import FamilyReminder  # noqa: E402
from sreda.db.session import get_engine, get_session_factory  # noqa: E402
from sreda.runtime import react_loop  # noqa: E402  ← РЕАЛЬНЫЙ модуль
from sreda.services.llm import get_chat_llm  # noqa: E402

TENANT, USER = "tenant_max_90000001", "u1"


def _seed(session):
    session.add(Tenant(id=TENANT, name="T"))
    session.add(Workspace(id="w1", tenant_id=TENANT, name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id=TENANT, workspace_id="w1", name="Sreda"))
    session.add(User(id=USER, tenant_id=TENANT, telegram_account_id="900000001"))
    now = datetime.now(timezone.utc)
    for title, dt in [
        ("Разминка с гантелями утром", now + timedelta(days=1, hours=9)),
        ("Разминка вечером", now + timedelta(days=1, hours=19)),
        ("Разминка в обед", now + timedelta(days=2, hours=13)),
    ]:
        session.add(FamilyReminder(id=f"rem_{uuid4().hex[:20]}", tenant_id=TENANT,
                    user_id=USER, title=title, trigger_at=dt, next_trigger_at=dt,
                    status="pending"))
    session.commit()


async def main() -> int:
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    _seed(session)
    llm = get_chat_llm(provider="inception-mercury2", settings=get_settings())

    def _count():
        session.expire_all()
        return session.query(FamilyReminder).filter(
            FamilyReminder.tenant_id == TENANT, FamilyReminder.status == "pending").count()

    thread = f"tg:{TENANT}:900000001"
    before = _count()
    print(f"[до] активных: {before}\n")

    incoming = ["удали напоминание про разминку", "вечернее", "да"]
    for msg in incoming:
        print(f"👤 {msg}")
        reply = await react_loop.handle_turn(
            session=session, tenant_id=TENANT, user_id=USER, thread_id=thread,
            llm=llm, user_text=msg)
        print(f"🤖 {reply}\n")

    after = _count()
    cancelled = session.query(FamilyReminder).filter(
        FamilyReminder.tenant_id == TENANT, FamilyReminder.status == "cancelled").all()
    print("=" * 60)
    print(f"активных: до={before} после={after} | удалено: {[r.title for r in cancelled]}")
    ok = after == before - 1 and len(cancelled) == 1 and cancelled[0].title == "Разминка вечером"
    print(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    session.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
