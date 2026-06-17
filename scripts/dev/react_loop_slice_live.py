"""Живой прогон СРЕЗА #162 (напоминания+задачи) на реальном Mercury через РЕАЛЬНЫЙ
sreda.runtime.react_loop.handle_turn. Прод НЕ тронут; локальная SQLite.

    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python scripts/dev/react_loop_slice_live.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, "C:/pro/sreda-wt-react-loop/src")

_SEC = Path("C:/pro/vex-assistant/sreda/.secrets")
_DB = Path(tempfile.gettempdir()) / "react_loop_slice_live.db"
if _DB.exists():
    _DB.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef").decode("ascii")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "react_loop_slice_salt_not_prod")
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_SEC / "inception.txt")
os.environ.setdefault("SREDA_MIMO_API_KEY_FILE", str(_SEC / "mimo_api_key.txt"))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings  # noqa: E402
get_settings.cache_clear()
from sreda.db.base import Base  # noqa: E402
from sreda.db.models import Assistant, Tenant, User, Workspace  # noqa: E402
from sreda.db.models.housewife import FamilyReminder  # noqa: E402
from sreda.db.models.tasks import Task  # noqa: E402
from sreda.db.session import get_engine, get_session_factory  # noqa: E402
from sreda.runtime import react_loop  # noqa: E402
from sreda.services.llm import get_chat_llm  # noqa: E402

TENANT, USER = "tenant_max_40921122", "u1"


def _seed(session):
    session.add(Tenant(id=TENANT, name="T"))
    session.add(Workspace(id="w1", tenant_id=TENANT, name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id=TENANT, workspace_id="w1", name="Sreda"))
    session.add(User(id=USER, tenant_id=TENANT, telegram_account_id="352612382"))
    session.commit()


async def main() -> int:
    get_engine.cache_clear(); get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    _seed(session)
    llm = get_chat_llm(provider="inception-mercury2", settings=get_settings())

    def rems():
        session.expire_all()
        return session.query(FamilyReminder).filter(
            FamilyReminder.tenant_id == TENANT, FamilyReminder.status == "pending").all()

    def tasks_all(status="pending"):
        session.expire_all()
        return session.query(Task).filter(
            Task.tenant_id == TENANT, Task.status == status).all()

    async def turn(text, thread, mid):
        print(f"👤 {text}")
        r = await react_loop.handle_turn(
            session=session, tenant_id=TENANT, user_id=USER, thread_id=thread,
            llm=llm, user_text=text, inbound_message_id=mid, channel="max")
        print(f"🤖 {r}\n")
        return r

    results = []

    # 1. создать напоминание
    th = f"react:{TENANT}:s1"
    await turn("напомни послезавтра в 8 утра принять таблетку", th, "m1")
    r1 = rems()
    ok1 = len(r1) == 1 and r1[0].operation_id is not None
    results.append(("создать напоминание (1 строка + operation_id)", ok1))

    # 2. создать задачу
    th = f"react:{TENANT}:s2"
    await turn("добавь задачу полить цветы в субботу", th, "m2")
    t2 = tasks_all()
    ok2 = len(t2) == 1 and t2[0].operation_id is not None
    results.append(("создать задачу (1 строка + operation_id)", ok2))

    # 3. показать задачи
    th = f"react:{TENANT}:s3"
    rep3 = await turn("покажи мои задачи", th, "m3")
    ok3 = "цвет" in (rep3 or "").lower()
    results.append(("список задач упоминает «цветы»", ok3))

    # 4. выполнить задачу
    th = f"react:{TENANT}:s4"
    await turn("отметь задачу про цветы выполненной", th, "m4")
    ok4 = len(tasks_all("completed")) == 1 and len(tasks_all("pending")) == 0
    results.append(("задача отмечена выполненной", ok4))

    # 5. удалить напоминание по описанию (confirm)
    th = f"react:{TENANT}:s5"
    await turn("удали напоминание про таблетку", th, "m5a")
    await turn("да", th, "m5b")
    ok5 = len(rems()) == 0
    results.append(("удаление напоминания с подтверждением", ok5))

    # 6. non-slice → мягкая деградация (не падать, упомянуть что умеет)
    th = f"react:{TENANT}:s6"
    rep6 = await turn("добавь молоко в список покупок", th, "m6")
    low = (rep6 or "").lower()
    ok6 = bool(rep6) and "потеряла контекст" not in low  # не упал в fallback
    results.append(("non-slice не роняет ход", ok6))

    print("=" * 60)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} — {name}")
    allok = all(ok for _, ok in results)
    print(f"ИТОГ: {'PASS' if allok else 'FAIL'}")
    session.close()
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
