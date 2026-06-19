"""Прототип нового разговорного цикла: ReAct + interrupt (HITL) + InMemorySaver.

Доказывает на кейсе «удали разминку» (3 совпадения):
  list_reminders → ask_human(«какое из N?») → выбор → ask_human(«точно удалить X?»)
  → cancel_reminder(id) — БЕЗ самописной машины состояний, на родном LangGraph.

LLM = Mercury (inception-mercury2) — заодно проверяем нативный tool-calling.
Состояние — InMemorySaver (RAM, ПД на покое нет → шифрование тут не нужно).
Прод НЕ тронут (локальная SQLite, синтетика). Сеть нужна (живой Mercury).

Запуск из worktree:
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python scripts/dev/react_loop_smoke.py
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

# --- env ДО импорта sreda ---------------------------------------------------
_REPO = Path(__file__).resolve().parents[2]
# .secrets gitignored → их нет в worktree; берём из основного чекаута.
_SEC = Path("C:/pro/vex-assistant/sreda/.secrets")
_DB = Path(tempfile.gettempdir()) / "react_loop_smoke.db"
if _DB.exists():
    _DB.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode("ascii")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "react_loop_smoke_salt_not_prod")
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_SEC / "inception.txt")
os.environ.setdefault("SREDA_MIMO_API_KEY_FILE", str(_SEC / "mimo_api_key.txt"))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
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

TENANT = "t1"
_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _seed(session) -> None:
    session.add(Tenant(id=TENANT, name="T"))
    session.add(Workspace(id="w1", tenant_id=TENANT, name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id=TENANT, workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id=TENANT, telegram_account_id="42"))
    now = datetime.now(timezone.utc)
    seeds = [
        ("Разминка с гантелями утром", now + timedelta(days=1, hours=9)),
        ("Разминка вечером", now + timedelta(days=1, hours=19)),
        ("Разминка в обед", now + timedelta(days=2, hours=13)),
    ]
    for title, when in seeds:
        session.add(FamilyReminder(
            id=f"rem_{uuid4().hex[:20]}", tenant_id=TENANT, user_id="u1",
            title=title, trigger_at=when, next_trigger_at=when, status="pending",
        ))
    session.commit()


def _fmt(when: datetime) -> str:
    return f"{when.day} {_MONTHS[when.month]} в {when:%H:%M}"


def main() -> int:
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    _seed(session)

    def _active():
        session.expire_all()
        return (session.query(FamilyReminder)
                .filter(FamilyReminder.tenant_id == TENANT,
                        FamilyReminder.status == "pending").all())

    # --- инструменты (возвращают id — внутренний контекст LLM) ---------------
    @tool
    def list_reminders(title_match: str = "") -> str:
        """Показать активные напоминания пользователя (опц. фильтр по подстроке
        названия). Возвращает для каждого: id, название, время."""
        rows = [r for r in _active()
                if title_match.lower() in r.title.lower()]
        if not rows:
            return "Нет активных напоминаний по этому запросу."
        return "\n".join(
            f"- id={r.id} | {r.title} | {_fmt(r.trigger_at)}" for r in rows)

    @tool
    def cancel_reminder(reminder_id: str) -> str:
        """Отменить (удалить) конкретное напоминание по его id. Вызывать ТОЛЬКО
        после того, как пользователь подтвердил удаление именно этого."""
        r = session.get(FamilyReminder, reminder_id)
        if r is None or r.status != "pending":
            return f"Напоминание {reminder_id} не найдено или уже неактивно."
        r.status = "cancelled"
        session.commit()
        return f"Удалено: {r.title}."

    @tool
    def ask_human(question: str) -> str:
        """Задать пользователю уточняющий или подтверждающий вопрос и ДОЖДАТЬСЯ
        ответа. Используй для выбора из нескольких и для подтверждения перед
        удалением."""
        return str(interrupt(question))

    tools = [list_reminders, cancel_reminder, ask_human]
    tools_by_name = {t.name: t for t in tools}

    llm = get_chat_llm(provider="inception-mercury2", settings=get_settings())
    if llm is None:
        print("FAIL: get_chat_llm вернул None (нет ключа inception?)")
        return 1
    llm_tools = llm.bind_tools(tools)

    # Структура по prompt-guide Mercury 2: persona/style → tools → task →
    # few-shot (поз.+нег.) → КРИТИЧНЫЕ ПРАВИЛА В КОНЦЕ (Mercury сильнее весит
    # недавний контекст). XML-теги для разбора секций.
    SYS = (
        "<persona>\n"
        "Ты — Среда, ассистент в мессенджере. Помогаешь вести напоминания.\n"
        "</persona>\n\n"
        "<style>\n"
        "Отвечай по-русски, кратко, разговорно — без markdown, без списков-"
        "звёздочек, заголовков и таблиц. Один вопрос за раз. Не начинай ответ с "
        "«Отлично!», «Конечно!», «Разумеется!».\n"
        "</style>\n\n"
        "<tools>\n"
        "- list_reminders(title_match): активные напоминания (id, название, время).\n"
        "- cancel_reminder(reminder_id): удалить напоминание по id. НЕОБРАТИМО.\n"
        "- ask_human(question): задать пользователю вопрос и дождаться ответа.\n"
        "</tools>\n\n"
        "<current_task>\n"
        "Помочь пользователю найти и удалить нужное напоминание.\n"
        "</current_task>\n\n"
        "<examples>\n"
        "Правильно:\n"
        "Пользователь: «удали напоминание про зал»\n"
        "→ list_reminders(title_match=\"зал\"); если ровно одно — ask_human"
        "(«Точно удалить „…“?»); после «да» — cancel_reminder(id).\n"
        "Пользователь: «вечернее» (ответ на выбор)\n"
        "→ НЕ вызывать list_reminders снова (список уже есть в разговоре); "
        "ask_human(«Точно удалить „Разминка вечером — …“?»).\n\n"
        "Неправильно (так НЕ делай):\n"
        "- вызывать list_reminders повторно, когда список уже получен в этом "
        "разговоре;\n"
        "- вызывать cancel_reminder без предварительного «да» от пользователя;\n"
        "- спрашивать несколько вещей в одном сообщении.\n"
        "</examples>\n\n"
        "<rules>\n"
        "1. Перед cancel_reminder ВСЕГДА сначала ask_human с подтверждением "
        "«Точно удалить „<название> — <время>“?» и удаляй ТОЛЬКО после «да». "
        "«Нет» → уточни, какое именно.\n"
        "2. Если подходит НЕ ровно одно — ask_human, какое именно (варианты с "
        "временем).\n"
        "3. Минимум вызовов: если список уже получен в этом разговоре — НЕ "
        "запрашивай его снова, используй имеющийся.\n"
        "4. reminder_id бери из результата list_reminders, не выдумывай.\n"
        "5. Один вопрос за раз.\n"
        "</rules>"
    )

    # --- граф ReAct ----------------------------------------------------------
    def chat(state: MessagesState):
        resp = llm_tools.invoke([SystemMessage(SYS), *state["messages"]])
        return {"messages": [resp]}

    def run_tools(state: MessagesState):
        last = state["messages"][-1]
        out = []
        for tc in last.tool_calls:
            res = tools_by_name[tc["name"]].invoke(tc["args"])
            out.append(ToolMessage(content=str(res), name=tc["name"],
                                   tool_call_id=tc["id"]))
        return {"messages": out}

    def route(state: MessagesState):
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    g = StateGraph(MessagesState)
    g.add_node("chat", chat)
    g.add_node("tools", run_tools)
    g.add_edge(START, "chat")
    g.add_conditional_edges("chat", route, {"tools": "tools", END: END})
    g.add_edge("tools", "chat")
    app = g.compile(checkpointer=InMemorySaver())

    cfg = {"configurable": {"thread_id": "boris-test"}}
    before = len(_active())
    print(f"[до] активных напоминаний: {before}\n")

    # Симулируем диалог: первый ход + ответы на вопросы бота по очереди.
    user_turns = ["удали напоминание про разминку"]
    answers = ["по пн, ср и пт"  # выбор (бот должен переспросить → дать варианты)
               , "да"]            # подтверждение
    # ↑ список ответов на ask_human; берём по очереди
    ans_iter = iter(["вечернее", "да"])  # выбор по описанию + подтверждение

    def _print_msgs(result):
        for m in result["messages"]:
            if isinstance(m, AIMessage):
                if m.tool_calls:
                    for tc in m.tool_calls:
                        print(f"   ⚙ LLM→tool: {tc['name']}({tc['args']})")
                if (m.content or "").strip():
                    print(f"   🤖 {m.content.strip()[:200]}")

    print(f"👤 {user_turns[0]}")
    result = app.invoke({"messages": [HumanMessage(user_turns[0])]}, cfg)
    _print_msgs(result)

    state = app.get_state(cfg)
    step = 0
    while state.next and step < 6:
        step += 1
        q = state.tasks[0].interrupts[0].value
        print(f"\n   ❓ БОТ СПРАШИВАЕТ: {q}")
        try:
            ans = next(ans_iter)
        except StopIteration:
            ans = "да"
        print(f"👤 (ответ) {ans}")
        result = app.invoke(Command(resume=ans), cfg)
        _print_msgs(result)
        state = app.get_state(cfg)

    after = len(_active())
    final = result["messages"][-1].content if result.get("messages") else ""
    print("\n" + "=" * 60)
    print(f"ФИНАЛ: {final.strip()[:300]}")
    print(f"активных напоминаний: до={before} после={after}")
    cancelled = (session.query(FamilyReminder)
                 .filter(FamilyReminder.tenant_id == TENANT,
                         FamilyReminder.status == "cancelled").all())
    print(f"удалено: {[r.title for r in cancelled]}")
    ok = (after == before - 1) and len(cancelled) == 1
    print(f"\nИТОГ: {'PASS' if ok else 'FAIL'} "
          f"(ожидалось: удалено ровно 1 из {before})")
    session.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
