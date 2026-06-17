"""Новый разговорный цикл (ReAct + interrupt/HITL) — гейт-эксперимент (#66).

Назначение: проверить на ОДНОМ тенанте (Борис) родной LangGraph-цикл вместо
самописного plan-execute. Узлы ``chat ⇄ tools`` + ``ask_human``/``interrupt()``
для уточнения и ПОДТВЕРЖДЕНИЯ перед удалением. Состояние — InMemorySaver
(RAM, ПД на покое НЕТ → шифрование тут не нужно; durable PostgresSaver — отдельный
шаг 0 с шифрующим serde). Остальные тенанты на гейт НЕ попадают (нулевой регресс).

Усиления (консультация Codex high, 2026-06-17):
- cancel_reminder САМ спрашивает подтверждение через ``interrupt()`` — удаление
  физически невозможно без прохождения через подтверждение (детерминированный
  guardrail, не доверяем намерению ЛЛМ).
- tenant/user-guard + идемпотентность (повторный cancel = «уже отменено»).
- ЛЛМ отдаём opaque ``ref``, не сырой id; финал — без id (scrub).
- TTL на pending + fallback «контекст потерян»; per-step trace без ПД.
- code до interrupt идемпотентен (g-032): мутация — только ПОСЛЕ подтверждения.

Граф пересобирается на запрос с инструментами под session ЭТОГО запроса, но
checkpointer — общий singleton на процесс (поллер однопроцессный — проверено).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt

logger = logging.getLogger("sreda.react_loop")

# Топология графа фиксирована: переиспользуем ОДИН checkpointer на множестве
# свежекомпилированных графов (Codex Q1) — имена узлов/каналов обязаны совпадать.
_TOPOLOGY_VERSION = "react-v1:chat,tools"
_CHECKPOINTER = InMemorySaver()  # singleton на процесс
# Единый источник правды о паузе — сам checkpoint (snap.next + snap.created_at),
# как в wassim249 (без параллельного словаря → нечему рассинхронизироваться).
# _THREAD_GEN — лишь СЕЛЕКТОР поколения thread_id: поднимаем при протухшем
# pending или ошибке, чтобы свежий ход шёл на ЧИСТЫЙ тред, а не приклеился к
# старой паузе (Codex CRITICAL 2026-06-17).
_THREAD_GEN: dict[str, int] = {}
_PENDING_TTL_SECONDS = 300  # 5 минут (решение владельца)

_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_YES = {"да", "ага", "угу", "удали", "удаляй", "подтверждаю", "yes", "верно", "точно"}
_NO = {"нет", "не надо", "отмена", "стоп", "no", "не удаляй"}

SYSTEM_PROMPT = (
    "<persona>\nТы — Среда, ассистент в мессенджере. Помогаешь вести напоминания.\n</persona>\n\n"
    "<style>\nОтвечай по-русски, кратко, разговорно — без markdown, списков-звёздочек, "
    "заголовков и таблиц. Один вопрос за раз. Не начинай ответ с «Отлично!», «Конечно!», "
    "«Разумеется!».\n</style>\n\n"
    "<tools>\n- list_reminders(title_match): активные напоминания (ref, название, время).\n"
    "- cancel_reminder(reminder_ref): удалить напоминание по ref. Инструмент САМ спросит "
    "подтверждение у пользователя — отдельно подтверждение НЕ запрашивай.\n"
    "- ask_human(question): задать пользователю уточняющий вопрос (какое из нескольких).\n</tools>\n\n"
    "<current_task>\nПомочь найти и удалить нужное напоминание.\n</current_task>\n\n"
    "<examples>\nПравильно:\nПользователь: «удали напоминание про зал»\n"
    "→ list_reminders(title_match=\"зал\"); если ровно одно — cancel_reminder(ref) "
    "(он сам подтвердит).\nПользователь: «вечернее» (ответ на выбор)\n"
    "→ НЕ вызывать list_reminders снова; cancel_reminder(ref вечернего).\n\n"
    "Неправильно (так НЕ делай):\n- вызывать list_reminders повторно, когда список уже есть;\n"
    "- спрашивать «точно удалить?» через ask_human — это делает сам cancel_reminder;\n"
    "- спрашивать несколько вещей сразу.\n</examples>\n\n"
    "<rules>\n1. Если по запросу подходит НЕ ровно одно — ask_human, какое именно "
    "(перечисли варианты с временем).\n"
    "2. Когда определился ровно один — вызови cancel_reminder(ref). Подтверждение он "
    "берёт сам; не дублируй.\n3. Минимум вызовов: список уже получен — не запрашивай снова.\n"
    "4. ref бери из результата list_reminders, не выдумывай.\n5. Один вопрос за раз.\n</rules>"
)


def _fmt(when: datetime) -> str:
    return f"{when.day} {_MONTHS[when.month]} в {when:%H:%M}"


def _is_yes(text: str) -> bool:
    t = (text or "").strip().lower().rstrip("!.")
    return any(t == y or t.startswith(y + " ") for y in _YES)


def build_reminder_tools(session: Any, tenant_id: str, user_id: str) -> list:
    """Тонкие инструменты над FamilyReminder, замкнутые на session/tenant/user
    ЭТОГО запроса. tenant+user-guard + идемпотентность + opaque ref + self-confirm."""
    from sreda.db.models.housewife import FamilyReminder

    def _active() -> list:
        session.expire_all()
        return (session.query(FamilyReminder)
                .filter(FamilyReminder.tenant_id == tenant_id,
                        FamilyReminder.user_id == user_id,
                        FamilyReminder.status == "pending")
                .order_by(FamilyReminder.trigger_at).all())

    @tool
    def list_reminders(title_match: str = "") -> str:
        """Показать активные напоминания пользователя (опц. фильтр по подстроке
        названия). Возвращает ref, название и время каждого."""
        rows = [r for r in _active() if title_match.lower() in (r.title or "").lower()]
        if not rows:
            return "Активных напоминаний по этому запросу нет."
        return "\n".join(f"- ref={r.id} | {r.title} | {_fmt(r.trigger_at)}" for r in rows)

    @tool
    def cancel_reminder(reminder_ref: str) -> str:
        """Удалить напоминание по ref. ВНУТРИ сам спрашивает у пользователя
        подтверждение и удаляет ТОЛЬКО при согласии. Отдельно подтверждать не нужно."""
        # tenant/user-guard: ref должен принадлежать этому пользователю.
        r = session.get(FamilyReminder, reminder_ref)
        if r is None or r.tenant_id != tenant_id or r.user_id != user_id:
            return "Такого напоминания у тебя нет."
        if r.status != "pending":  # идемпотентность
            return f"Напоминание «{r.title}» уже неактивно."
        title, when = r.title, r.trigger_at  # снимок ДО interrupt (идемпотентно)
        # --- guardrail: подтверждение ДО любой мутации (g-032) ---
        decision = interrupt(f"Точно удалить «{title} — {_fmt(when)}»?")
        if not _is_yes(str(decision)):
            return f"Хорошо, не удаляю «{title}». Скажи, какое тогда."
        # Перечитываем заново (узел перевыполнился) — мутация ПОСЛЕ interrupt.
        # tenant/user-guard ВПЛОТНУЮ к мутации (Codex MAJOR: не только до паузы).
        r2 = session.get(FamilyReminder, reminder_ref)
        if (r2 is None or r2.tenant_id != tenant_id or r2.user_id != user_id
                or r2.status != "pending"):
            return f"Напоминание «{title}» уже неактивно."
        r2.status = "cancelled"
        session.commit()
        return f"Готово, удалила «{title}»."

    @tool
    def ask_human(question: str) -> str:
        """Задать пользователю уточняющий вопрос (какое из нескольких) и дождаться ответа."""
        return str(interrupt(question))

    return [list_reminders, cancel_reminder, ask_human]


def _build_graph(llm_with_tools: Any, tools_by_name: dict):
    def chat(state: MessagesState):
        return {"messages": [llm_with_tools.invoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])]}

    def run_tools(state: MessagesState):
        out = []
        for tc in state["messages"][-1].tool_calls:
            res = tools_by_name[tc["name"]].invoke(tc["args"])
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
    assert _TOPOLOGY_VERSION == "react-v1:chat,tools"  # топология фиксирована
    return g.compile(checkpointer=_CHECKPOINTER)


def _scrub_ids(text: str) -> str:
    import re
    # ref=/id=/rem_<hex> не должны утечь пользователю в финальном тексте.
    return re.sub(r"\bref=\S+|\bid=\S+|\brem_[0-9a-f]+", "", text or "").strip()


def _interrupt_age_seconds(created_at: Any) -> float:
    """Возраст текущего снимка checkpoint (для TTL) — из САМОГО снимка (единый
    источник). FAIL-CLOSED (Codex R2 MAJOR): при отсутствии/невалидности
    created_at возвращаем inf → пауза считается протухшей (недатированную паузу
    НЕ возобновляем, а ротируем поколение)."""
    if not created_at:
        return float("inf")
    try:
        ts = (created_at if isinstance(created_at, datetime)
              else datetime.fromisoformat(str(created_at).replace("Z", "+00:00")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:  # noqa: BLE001
        return float("inf")


def _text_content(content: Any) -> str:
    """Нормализация контента ответа модели в строку (как extract_text_content у
    wassim249). Reasoning-модели (вкл. Mercury с reasoning_effort) могут вернуть
    СПИСОК блоков [{type:reasoning},{type:text}] — берём только текст; иначе
    .strip() на списке уронил бы ход в fallback."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
        return "".join(parts).strip()
    return str(content or "").strip()


def _pending_question(snap: Any) -> str:
    if snap.tasks and snap.tasks[0].interrupts:
        return str(snap.tasks[0].interrupts[0].value)
    return ""


async def handle_turn(
    *, session: Any, tenant_id: str, user_id: str, thread_id: str,
    llm: Any, user_text: str,
) -> str:
    """ВХОД нового цикла на одно входящее сообщение. Источник правды о паузе —
    сам checkpoint (snap.next + snap.created_at), как в wassim249. Поколение
    thread_id поднимаем только при протухшем pending или ошибке. НИКОГДА не
    поднимает исключений — при сбое отдаёт безопасный fallback."""
    base = thread_id
    gen = _THREAD_GEN.get(base, 0)

    def _cfg(g: int) -> dict:
        return {"configurable": {"thread_id": f"{base}#{g}"}}

    try:
        tools = build_reminder_tools(session, tenant_id, user_id)
        graph = _build_graph(llm.bind_tools(tools), {t.name: t for t in tools})

        snap = await graph.aget_state(_cfg(gen))
        live_pause = bool(snap.next) and _interrupt_age_seconds(snap.created_at) <= _PENDING_TTL_SECONDS

        if live_pause:  # живое уточнение → возобновляем тем же поколением
            result = await graph.ainvoke(Command(resume=user_text), _cfg(gen))
        else:
            # свежий ход. Если на этом поколении осталась ПРОТУХШАЯ пауза —
            # поднимаем поколение, чтобы свежий ход не приклеился к ней.
            if snap.next:
                gen += 1
                _THREAD_GEN[base] = gen
            result = await graph.ainvoke({"messages": [HumanMessage(user_text)]}, _cfg(gen))

        snap = await graph.aget_state(_cfg(gen))
        if snap.next:  # снова пауза → отдать вопрос пользователю
            return _scrub_ids(_pending_question(snap)) or "Уточни, пожалуйста."
        last = result["messages"][-1] if result.get("messages") else None
        text = _text_content(getattr(last, "content", "")) if isinstance(last, AIMessage) else ""
        return _scrub_ids(text) or "Готово."
    except Exception as exc:  # noqa: BLE001 — цикл не должен ронять ход
        # PII-safe (Codex R2 MAJOR): только тип ошибки + поколение, БЕЗ traceback
        # и str(exc) — они могут нести текст юзера / название / ref.
        logger.warning("react_loop: handle_turn failed type=%s gen=%s",
                       type(exc).__name__, gen)
        _THREAD_GEN[base] = gen + 1  # бросаем поколение → следующий ход на чистом треде
        return ("Ой, я потеряла контекст этого диалога. Повтори, пожалуйста, "
                "что нужно сделать.")
