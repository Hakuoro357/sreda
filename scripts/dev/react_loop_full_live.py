"""Живой прогон ПОЛНОГО тулсета (#162): добранные семьи + формат Фредди.
Проверяет: покупки/рецепты/память (раньше «не умею») работают; списки построчно.

    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python scripts/dev/react_loop_full_live.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "C:/pro/sreda-wt-react-loop/src")
_SEC = Path("C:/pro/vex-assistant/sreda/.secrets")
_DB = Path(tempfile.gettempdir()) / "react_loop_full_live.db"
if _DB.exists():
    _DB.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef").decode("ascii")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "react_loop_full_salt")
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_SEC / "inception.txt")
os.environ.setdefault("SREDA_MIMO_API_KEY_FILE", str(_SEC / "mimo_api_key.txt"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings  # noqa: E402
get_settings.cache_clear()
from sreda.db.base import Base  # noqa: E402
from sreda.db.models import Assistant, Tenant, User, Workspace  # noqa: E402
from sreda.db.models.housewife_food import ShoppingListItem  # noqa: E402
from sreda.db.session import get_engine, get_session_factory  # noqa: E402
from sreda.runtime import react_loop  # noqa: E402
from sreda.services.llm import get_chat_llm  # noqa: E402

TENANT, USER = "tenant_max_90000001", "u1"


def _seed(session):
    session.add(Tenant(id=TENANT, name="T"))
    session.add(Workspace(id="w1", tenant_id=TENANT, name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id=TENANT, workspace_id="w1", name="Sreda"))
    session.add(User(id=USER, tenant_id=TENANT, telegram_account_id="900000001"))
    session.commit()


async def main() -> int:
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    _seed(session)
    llm = get_chat_llm(provider="inception-mercury2", settings=get_settings())

    async def turn(text, thread, mid):
        print(f"👤 {text}")
        r = await react_loop.handle_turn(
            session=session, tenant_id=TENANT, user_id=USER, thread_id=thread,
            llm=llm, user_text=text, inbound_message_id=mid, channel="max")
        print(f"🤖 {r}\n")
        return r

    def shopping():
        session.expire_all()
        return session.query(ShoppingListItem).filter(
            ShoppingListItem.tenant_id == TENANT, ShoppingListItem.status == "pending").all()

    results = []

    # 1. покупки (раньше «не умею»)
    await turn("добавь молоко и хлеб в список покупок", f"react:{TENANT}:s1", "m1")
    titles = {i.title.lower() for i in shopping()}
    ok1 = any("молок" in t for t in titles) and any("хлеб" in t for t in titles)
    results.append(("покупки: молоко+хлеб добавлены", ok1))

    # 2. показать список + формат (построчно)
    rep2 = await turn("покажи список покупок", f"react:{TENANT}:s2", "m2")
    ok2 = ("молок" in rep2.lower()) and ("\n" in rep2)  # построчно
    results.append(("список покупок показан построчно (\\n)", ok2))

    # 3. память (раньше «не умею»)
    rep3 = await turn("запомни: я люблю чай без сахара", f"react:{TENANT}:s3", "m3")
    ok3 = bool(rep3) and "потеряла контекст" not in rep3.lower()
    results.append(("память: факт принят без сбоя", ok3))

    # 4. рецепт (раньше «не умею»)
    rep4 = await turn("сохрани рецепт омлета: взбить 3 яйца, посолить, жарить 5 минут",
                      f"react:{TENANT}:s4", "m4")
    ok4 = bool(rep4) and "потеряла контекст" not in rep4.lower()
    results.append(("рецепт: омлет принят без сбоя", ok4))

    # 5. напоминание + формат показа построчно
    await turn("напомни завтра в 8 принять витамины", f"react:{TENANT}:s5", "m5")
    rep5b = await turn("покажи мои напоминания", f"react:{TENANT}:s5b", "m5b")
    ok5 = "витамин" in rep5b.lower()
    results.append(("напоминание создано и показано", ok5))

    print("=" * 60)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} — {name}")
    allok = all(ok for _, ok in results)
    print(f"ИТОГ: {'PASS' if allok else 'FAIL'}")
    session.close()
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
