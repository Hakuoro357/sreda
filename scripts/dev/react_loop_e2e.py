"""Полный локальный e2e нового цикла — симуляция ТЕЛЕГРАМ-входа.

Отличие от react_loop_smoke.py: тут проверяется ИНТЕГРАЦИОННАЯ ФОРМА —
функция-вход ``handle_turn(thread_id, text)`` (будущий телеграм-обработчик),
которую зовём ОТДЕЛЬНО на КАЖДОЕ сообщение (как отдельные вебхуки). Состояние
переживает между сообщениями через singleton-граф + InMemorySaver (как один
uvicorn-процесс на проде). Решение resume-vs-свежий — по ``graph.get_state().next``
(паттерн wassim249 get_response). Прод НЕ тронут; живой Mercury; локальная SQLite.

    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python scripts/dev/react_loop_e2e.py
"""
from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

_REPO = Path(__file__).resolve().parents[2]
_SEC = Path("C:/pro/vex-assistant/sreda/.secrets")  # gitignored, из основного чекаута
_DB = Path(tempfile.gettempdir()) / "react_loop_e2e.db"
if _DB.exists():
    _DB.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef").decode("ascii")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "react_loop_e2e_salt_not_prod")
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_SEC / "inception.txt")
os.environ.setdefault("SREDA_MIMO_API_KEY_FILE", str(_SEC / "mimo_api_key.txt"))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from langchain_core.messages import (  # noqa: E402
    HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from sreda.config.settings import get_settings  # noqa: E402
get_settings.cache_clear()
from sreda.db.base import Base  # noqa: E402
from sreda.db.models import Assistant, Tenant, User, Workspace  # noqa: E402
from sreda.db.models.housewife import FamilyReminder  # noqa: E402
from sreda.db.session import get_engine, get_session_factory  # noqa: E402
from sreda.services.llm import get_chat_llm  # noqa: E402

TENANT = "tenant_max_90000001"  # синтетический id (audit 2026-07-18: реальные id не коммитим)
_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]

SYS = (
    "<persona>\nТы — Среда, ассистент в мессенджере. Помогаешь вести напоминания.\n</persona>\n\n"
    "<style>\nОтвечай по-русски, кратко, разговорно — без markdown, без списков-звёздочек, "
    "заголовков и таблиц. Один вопрос за раз. Не начинай ответ с «Отлично!», «Конечно!», "
    "«Разумеется!».\n</style>\n\n"
    "<tools>\n- list_reminders(title_match): активные напоминания (id, название, время).\n"
    "- cancel_reminder(reminder_id): удалить напоминание по id. НЕОБРАТИМО.\n"
    "- ask_human(question): задать пользователю вопрос и дождаться ответа.\n</tools>\n\n"
    "<current_task>\nПомочь пользователю найти и удалить нужное напоминание.\n</current_task>\n\n"
    "<examples>\nПравильно:\nПользователь: «удали напоминание про зал»\n"
    "→ list_reminders(title_match=\"зал\"); если ровно одно — ask_human(«Точно удалить „…“?»); "
    "после «да» — cancel_reminder(id).\nПользователь: «вечернее» (ответ на выбор)\n"
    "→ НЕ вызывать list_reminders снова (список уже есть); ask_human(«Точно удалить "
    "„Разминка вечером — …“?»).\n\nНеправильно (так НЕ делай):\n"
    "- вызывать list_reminders повторно, когда список уже получен в этом разговоре;\n"
    "- вызывать cancel_reminder без предварительного «да»;\n"
    "- спрашивать несколько вещей сразу.\n</examples>\n\n"
    "<rules>\n1. Перед cancel_reminder ВСЕГДА сначала ask_human с подтверждением "
    "«Точно удалить „<название> — <время>“?» и удаляй ТОЛЬКО после «да». «Нет» → уточни.\n"
    "2. Если подходит НЕ ровно одно — ask_human, какое именно (варианты с временем).\n"
    "3. Минимум вызовов: если список уже получен в разговоре — НЕ запрашивай снова.\n"
    "4. reminder_id бери из результата list_reminders, не выдумывай.\n5. Один вопрос за раз.\n</rules>"
)


def _fmt(w: datetime) -> str:
    return f"{w.day} {_MONTHS[w.month]} в {w:%H:%M}"


def _seed(session) -> None:
    session.add(Tenant(id=TENANT, name="T"))
    session.add(Workspace(id="w1", tenant_id=TENANT, name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id=TENANT, workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id=TENANT, telegram_account_id="900000001"))
    now = datetime.now(timezone.utc)
    for title, dt in [
        ("Разминка с гантелями утром", now + timedelta(days=1, hours=9)),
        ("Разминка вечером", now + timedelta(days=1, hours=19)),
        ("Разминка в обед", now + timedelta(days=2, hours=13)),
    ]:
        session.add(FamilyReminder(id=f"rem_{uuid4().hex[:20]}", tenant_id=TENANT,
                    user_id="u1", title=title, trigger_at=dt, next_trigger_at=dt,
                    status="pending"))
    session.commit()


def build_react_graph(session):
    """Собирает ReAct-граф (chat⇄tools) + InMemorySaver. Инструменты замкнуты
    на session/tenant (как build_housewife_tools в проде)."""
    def _active():
        session.expire_all()
        return (session.query(FamilyReminder).filter(
            FamilyReminder.tenant_id == TENANT,
            FamilyReminder.status == "pending").all())

    @tool
    def list_reminders(title_match: str = "") -> str:
        """Показать активные напоминания (опц. фильтр по подстроке названия).
        Возвращает id, название, время каждого."""
        rows = [r for r in _active() if title_match.lower() in r.title.lower()]
        if not rows:
            return "Нет активных напоминаний по этому запросу."
        return "\n".join(f"- id={r.id} | {r.title} | {_fmt(r.trigger_at)}" for r in rows)

    @tool
    def cancel_reminder(reminder_id: str) -> str:
        """Отменить напоминание по id. Вызывать ТОЛЬКО после подтверждения «да»."""
        r = session.get(FamilyReminder, reminder_id)
        if r is None or r.status != "pending":
            return f"Напоминание {reminder_id} не найдено или неактивно."
        r.status = "cancelled"
        session.commit()
        return f"Удалено: {r.title}."

    @tool
    def ask_human(question: str) -> str:
        """Задать пользователю вопрос и дождаться ответа (уточнение/подтверждение)."""
        return str(interrupt(question))

    tools = [list_reminders, cancel_reminder, ask_human]
    by_name = {t.name: t for t in tools}
    llm_tools = get_chat_llm(provider="inception-mercury2", settings=get_settings()).bind_tools(tools)

    def chat(state: MessagesState):
        return {"messages": [llm_tools.invoke([SystemMessage(SYS), *state["messages"]])]}

    def run_tools(state: MessagesState):
        out = []
        for tc in state["messages"][-1].tool_calls:
            res = by_name[tc["name"]].invoke(tc["args"])
            out.append(ToolMessage(content=str(res), name=tc["name"], tool_call_id=tc["id"]))
        return {"messages": out}

    def route(state: MessagesState):
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    g = StateGraph(MessagesState)
    g.add_node("chat", chat)
    g.add_node("tools", run_tools)
    g.add_edge(START, "chat")
    g.add_conditional_edges("chat", route, {"tools": "tools", END: END})
    g.add_edge("tools", "chat")
    return g.compile(checkpointer=InMemorySaver())


def handle_turn(graph, thread_id: str, user_text: str) -> str:
    """ВХОД (будущий телеграм-обработчик): одно входящее сообщение пользователя.

    Решение resume-vs-свежий — по состоянию графа этого треда (как wassim249
    get_response). Возвращает текст для пользователя: либо вопрос (если граф
    встал на interrupt), либо финальный ответ."""
    cfg = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(cfg)
    if snap.next:  # граф на паузе → это ответ юзера на заданный вопрос
        result = graph.invoke(Command(resume=user_text), cfg)
    else:          # свежий ход
        result = graph.invoke({"messages": [HumanMessage(user_text)]}, cfg)
    snap = graph.get_state(cfg)
    if snap.next:  # снова пауза → отдать пользователю вопрос
        return str(snap.tasks[0].interrupts[0].value)
    last = result["messages"][-1]
    return (getattr(last, "content", "") or "").strip()


def main() -> int:
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    _seed(session)

    graph = build_react_graph(session)  # singleton (как процесс uvicorn)
    thread = f"tg:{TENANT}:900000001"

    def _count():
        session.expire_all()
        return session.query(FamilyReminder).filter(
            FamilyReminder.tenant_id == TENANT,
            FamilyReminder.status == "pending").count()

    before = _count()
    print(f"[до] активных: {before}\n")

    # Имитация ТЕЛЕГРАМА: каждое сообщение — отдельный вызов handle_turn.
    incoming = ["удали напоминание про разминку", "вечернее", "да"]
    for msg in incoming:
        print(f"👤 {msg}")
        reply = handle_turn(graph, thread, msg)
        print(f"🤖 {reply}\n")

    after = _count()
    cancelled = session.query(FamilyReminder).filter(
        FamilyReminder.tenant_id == TENANT,
        FamilyReminder.status == "cancelled").all()
    print("=" * 60)
    print(f"активных: до={before} после={after} | удалено: {[r.title for r in cancelled]}")
    ok = after == before - 1 and len(cancelled) == 1 and cancelled[0].title == "Разминка вечером"
    print(f"ИТОГ: {'PASS' if ok else 'FAIL'} (ожидалось: удалена ровно «Разминка вечером»)")
    session.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
