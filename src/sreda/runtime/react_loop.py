"""Новый разговорный цикл (ReAct + interrupt/HITL) — #162 срез: напоминания+задачи.

Назначение: для тенанта Бориса ReAct = ЕДИНСТВЕННЫЙ путь (без plan-execute fallback).
Срез инструментов: напоминания {list, schedule, update, cancel} + задачи {list, add,
update(без расписания), complete, uncomplete, cancel, delete}. Остальные семьи — мягкая
деградация («пока умею напоминания и задачи»). Состояние — InMemorySaver (RAM, ПД на
покое НЕТ). Остальные тенанты на гейт НЕ попадают (нулевой регресс).

Идемпотентность (within-turn, #162 Фаза 0):
- create (schedule_reminder/add_task) — ctx-ветка в сервисах (operation_id с ВРЕМЕНЕМ в
  ключе + ON CONFLICT + SELECT стабильного id). ctx биндится в run_tools per tool_call;
  turn_key минтится РАЗ на ход и живёт в state графа (переживает resume) → operation_id
  стабилен и при перевыполнении узла после interrupt (g-032).
- destructive (cancel/delete) — confirm через interrupt() + снимок + state-guard,
  мутация ТОЛЬКО после «да» (детерминированный guardrail, не доверяем намерению ЛЛМ).
- mutate (complete/uncomplete/update) — no-op guard на повторе (см. сервисы).
Полный прод-субстрат идемпотентности (durable history, межходовой дедуп, atomic audit) —
ОТЛОЖЕН в #163. emit_event в этом срезе НЕ зовём.

Граф пересобирается на запрос с инструментами под session ЭТОГО запроса; checkpointer —
общий singleton на процесс (поллер/uvicorn однопроцессны — проверено).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt

from sreda.runtime.planner.tool_runtime import (
    ToolRuntimeContext,
    allocate_operation_id,
    bind_tool_runtime,
)

logger = logging.getLogger("sreda.react_loop")

# Топология фиксирована: ОДИН checkpointer на множестве свежекомпилированных графов.
# v2 — добавлен канал turn_key в state (#162). InMemory сбрасывается на рестарте, миграции
# чекпойнтов не нужны.
_TOPOLOGY_VERSION = "react-v2:chat,tools,turn_key"
_CHECKPOINTER = InMemorySaver()  # singleton на процесс
_THREAD_GEN: dict[str, int] = {}
_PENDING_TTL_SECONDS = 300  # 5 минут (решение владельца)

_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_YES = {"да", "ага", "угу", "удали", "удаляй", "подтверждаю", "yes", "верно", "точно"}
# Решение строится на `not _is_yes(...)` (всё не-«да» = отказ — fail-closed для удаления).

# #162 полный перенос: семьи, которые добираем из общего реестра (напоминания+задачи
# отдаём бес­поке-инструментами выше — с именованным confirm). onboarding/ui/utility — НЕ
# в разговорном цикле.
_EXTRA_FAMILIES = {"shopping", "recipes", "menu", "household", "checklists", "web"}
# Разрушающие инструменты добранных семей → требуют подтверждения (переиспользуем
# канонический набор из handlers, минус не-разрушающие). reminders/tasks-разрушающее
# уже под бес­поке-confirm выше.
_CONFIRM_PHRASE = {
    "delete_recipe": "удалить рецепт",
    "remove_shopping_items": "удалить позиции из списка покупок",
    "clear_bought_shopping": "очистить купленное в списке покупок",
    "clear_menu": "очистить меню",
    "remove_family_member": "удалить члена семьи",
    "delete_checklist_item": "удалить пункт чек-листа",
    "archive_checklist": "архивировать чек-лист",
    # move_task_to_checklist шаг 1 ОТМЕНЯЕТ исходную задачу (+напоминание) — destructive,
    # обходил бы confirm иначе (все 3 ревьюера, MAJOR).
    "move_task_to_checklist": "перенести задачу в дела (исходная задача отменится)",
}


def _confirm_wrap(inner: Any, phrase: str) -> Any:
    """Обернуть разрушающий инструмент подтверждением через interrupt(): мутация
    ТОЛЬКО после «да» (детерминированный guardrail, как у cancel_reminder). Сохраняет
    имя/описание/схему inner — ЛЛМ зовёт прозрачно; ctx остаётся забинженным (вызов
    inner.invoke идёт внутри bind_tool_runtime из run_tools)."""
    from langchain_core.tools import StructuredTool

    def _wrapped(**kwargs: Any) -> str:
        decision = interrupt(f"Точно {phrase}? Это действие необратимо.")
        if not _is_yes(str(decision)):
            return "Хорошо, не трогаю."
        return str(inner.invoke(kwargs))

    return StructuredTool.from_function(
        func=_wrapped, name=inner.name, description=inner.description,
        args_schema=inner.args_schema,
    )


class ReactState(MessagesState):
    """MessagesState + turn_key. turn_key минтится РАЗ на ход (handle_turn) и
    хранится в checkpoint → переживает resume; run_tools берёт его отсюда для
    стабильного operation_id (within-turn идемпотентность, g-032)."""

    turn_key: str


def _system_prompt(today_str: str) -> str:
    return (
        "<persona>\nТы — Среда, ассистент в мессенджере. Ведёшь напоминания и задачи "
        "пользователя.\n</persona>\n\n"
        "<style>\nОтвечай по-русски, тепло и живо — как помощник, не как сухая справка. "
        "Без markdown-звёздочек, заголовков и таблиц. СПИСКИ (напоминания, задачи, покупки, "
        "меню и т.п.) выводи КАЖДЫЙ ПУНКТ С НОВОЙ СТРОКИ через «— », НЕ в одну строку через "
        "тире. Дату и время пиши по-человечески («19 июня, 09:00»). Один вопрос за раз. "
        "Не начинай ответ с «Отлично!», «Конечно!».\n</style>\n\n"
        f"<context>\nСегодня {today_str}. Относительные даты («сегодня», «завтра», «в пятницу») "
        "САМ переводи в абсолютные перед вызовом инструментов: дату — YYYY-MM-DD, время — HH:MM, "
        "момент напоминания — полный ISO-8601 datetime.\n</context>\n\n"
        "<tools>\nНапоминания:\n"
        "- list_reminders(title_match): активные напоминания (ref, название, время).\n"
        "- schedule_reminder(title, trigger_iso): создать напоминание на абсолютный момент.\n"
        "- update_reminder(reminder_ref, title?, trigger_iso?): изменить напоминание.\n"
        "- cancel_reminder(reminder_ref): удалить. Инструмент САМ спросит подтверждение.\n"
        "Задачи:\n"
        "- list_tasks(scheduled_date?): задачи (ref, название, дата/время).\n"
        "- add_task(title, scheduled_date?, time_start?, notes?): создать задачу.\n"
        "- update_task(task_ref, title?, notes?): изменить ТЕКСТ задачи (перенос по времени "
        "пока не поддержан).\n"
        "- complete_task(task_ref) / uncomplete_task(task_ref): отметить выполненной/вернуть.\n"
        "- cancel_task(task_ref) / delete_task(task_ref): отменить/удалить. САМИ спросят "
        "подтверждение.\n"
        "- ask_human(question): уточнить у пользователя (какое из нескольких).\n"
        "Другое (своими инструментами): списки покупок, недельное меню, рецепты, чек-листы, "
        "члены семьи, заметки-память, погода и веб-поиск.\n</tools>\n\n"
        "<scope>\nТы ведёшь: напоминания, задачи, списки покупок, меню, рецепты, чек-листы, "
        "членов семьи, заметки-память; знаешь погоду и веб-поиск. Если просят совсем вне этого "
        "(оплатить счёт, позвонить за меня) — коротко скажи, что так не умеешь; инструменты "
        "не выдумывай.\n</scope>\n\n"
        "<examples>\nПравильно:\nПользователь: «удали напоминание про зал»\n"
        "→ list_reminders(title_match=\"зал\"); если ровно одно — cancel_reminder(ref).\n"
        "Пользователь: «вечернее» (ответ на выбор)\n"
        "→ НЕ вызывать list_reminders снова; cancel_reminder(ref вечернего).\n"
        "Пользователь: «добавь задачу полить цветы завтра»\n"
        "→ add_task(title=\"полить цветы\", scheduled_date=<завтра YYYY-MM-DD>).\n"
        "Пользователь: «покажи список покупок» (формат списка — СТРОГО так, с переносами):\n"
        "Вот твой список покупок:\n— молоко\n— хлеб\n— яйца\n\n"
        "Неправильно (так НЕ делай):\n"
        "- перечислять списком в ОДНУ строку: «список: — молоко — хлеб» (НАДО каждый с новой строки);\n"
        "- вызывать list повторно, когда список уже есть;\n"
        "- спрашивать «точно удалить?» через ask_human — это делают сами cancel/delete;\n"
        "- спрашивать несколько вещей сразу.\n</examples>\n\n"
        "<rules>\n1. Если по запросу подходит НЕ ровно одно — ask_human, какое именно "
        "(перечисли варианты с временем/датой).\n"
        "2. Определился ровно один — вызови нужный инструмент по его ref. Подтверждение "
        "разрушающие берут сами; не дублируй.\n3. Минимум вызовов: список уже получен — не "
        "запрашивай снова.\n4. ref бери из результата list_*, не выдумывай.\n"
        "5. Один вопрос за раз.\n"
        "6. ЛЮБОЙ список (напоминания, задачи, покупки, меню, рецепты) — ВСЕГДА построчно: "
        "вводная фраза с двоеточием, затем КАЖДЫЙ пункт на ОТДЕЛЬНОЙ строке с «— ». "
        "НИКОГДА не перечисляй в одну строку.\n</rules>"
    )


def _fmt(when: datetime) -> str:
    return f"{when.day} {_MONTHS[when.month]} в {when:%H:%M}"


def _fmt_task_when(t: Any) -> str:
    d = getattr(t, "scheduled_date", None)
    ts = getattr(t, "time_start", None)
    if d is None:
        return "без даты"
    s = f"{d.day} {_MONTHS[d.month]}"
    if ts is not None:
        s += f" в {ts:%H:%M}"
    return s


def _is_yes(text: str) -> bool:
    t = (text or "").strip().lower().rstrip("!.")
    return any(t == y or t.startswith(y + " ") for y in _YES)


def _parse_dt(s: str) -> datetime:
    """ISO-8601 → aware UTC. Naive трактуем как UTC (как schedule_reminder сегодня)."""
    dt = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_slice_tools(session: Any, tenant_id: str, user_id: str) -> list:
    """Тонкие инструменты среза (напоминания+задачи) над сервисами, замкнутые на
    session/tenant/user ЭТОГО запроса. Идемпотентность создания — в сервисах
    (ctx-ветка); ctx биндится в run_tools. Разрушающие сами спрашивают подтверждение."""
    from sreda.db.models.housewife import FamilyReminder
    from sreda.services.housewife_reminders import HousewifeReminderService
    from sreda.services.tasks import TaskService

    reminders = HousewifeReminderService(session)
    tasks = TaskService(session)

    # ---- напоминания ----------------------------------------------------
    def _active_reminders() -> list:
        session.expire_all()
        return (session.query(FamilyReminder)
                .filter(FamilyReminder.tenant_id == tenant_id,
                        FamilyReminder.user_id == user_id,
                        FamilyReminder.status == "pending")
                .order_by(FamilyReminder.trigger_at).all())

    @tool
    def list_reminders(title_match: str = "") -> str:
        """Показать активные напоминания (опц. фильтр по подстроке названия)."""
        rows = [r for r in _active_reminders()
                if title_match.lower() in (r.title or "").lower()]
        if not rows:
            return "Активных напоминаний по этому запросу нет."
        return "\n".join(f"- ref={r.id} | {r.title} | {_fmt(r.trigger_at)}" for r in rows)

    @tool
    def schedule_reminder(title: str, trigger_iso: str) -> str:
        """Создать напоминание. trigger_iso — АБСОЛЮТНЫЙ ISO-8601 datetime (относительные
        даты резолвь сам по сегодняшней дате из <context>)."""
        try:
            when = _parse_dt(trigger_iso)
        except Exception:  # noqa: BLE001
            return f"Не разобрала время: {trigger_iso!r}. Дай абсолютный момент."
        r = reminders.schedule(tenant_id=tenant_id, user_id=user_id,
                               title=title, trigger_at=when)
        return f"ok:scheduled:{r.id} | {r.title} | {_fmt(r.trigger_at)}"

    @tool
    def update_reminder(reminder_ref: str, title: str = "", trigger_iso: str = "") -> str:
        """Изменить напоминание по ref: название и/или момент (АБСОЛЮТНЫЙ ISO)."""
        # user-guard (симметрично cancel_reminder): сервис update гардит только tenant.
        r0 = session.get(FamilyReminder, reminder_ref)
        if r0 is None or r0.tenant_id != tenant_id or r0.user_id != user_id:
            return "Такого напоминания у тебя нет."
        new_trigger = None
        if trigger_iso:
            try:
                new_trigger = _parse_dt(trigger_iso)
            except Exception:  # noqa: BLE001
                return f"Не разобрала время: {trigger_iso!r}."
        # no-op guard (#162 п.5): те же значения → успех без записи.
        new_title = title or None
        if ((new_title is None or new_title == r0.title)
                and (new_trigger is None or new_trigger == r0.trigger_at)):
            return f"ok:updated:{r0.id} | {r0.title} | {_fmt(r0.trigger_at)}"
        r = reminders.update(
            tenant_id=tenant_id, reminder_id=reminder_ref,
            title=title or None, trigger_at=new_trigger,
        )
        if r is None:
            return "Такого напоминания у тебя нет."
        return f"ok:updated:{r.id} | {r.title} | {_fmt(r.trigger_at)}"

    @tool
    def cancel_reminder(reminder_ref: str) -> str:
        """Удалить напоминание по ref. САМ спрашивает подтверждение и удаляет ТОЛЬКО при «да»."""
        r = session.get(FamilyReminder, reminder_ref)
        if r is None or r.tenant_id != tenant_id or r.user_id != user_id:
            return "Такого напоминания у тебя нет."
        if r.status != "pending":
            return f"Напоминание «{r.title}» уже неактивно."
        title, when = r.title, r.trigger_at  # снимок ДО interrupt (g-032)
        decision = interrupt(f"Точно удалить «{title} — {_fmt(when)}»?")
        if not _is_yes(str(decision)):
            return f"Хорошо, не удаляю «{title}». Скажи, какое тогда."
        r2 = session.get(FamilyReminder, reminder_ref)
        if (r2 is None or r2.tenant_id != tenant_id or r2.user_id != user_id
                or r2.status != "pending"):
            return f"Напоминание «{title}» уже неактивно."  # идемпотентно
        r2.status = "cancelled"
        session.commit()
        return f"Готово, удалила «{title}»."

    # ---- задачи ---------------------------------------------------------
    @tool
    def list_tasks(scheduled_date: str = "") -> str:
        """Показать задачи пользователя (опц. фильтр по дате YYYY-MM-DD)."""
        d = None
        if scheduled_date:
            try:
                d = date.fromisoformat(scheduled_date.strip())
            except Exception:  # noqa: BLE001
                return f"Не разобрала дату: {scheduled_date!r}."
        # d=None → ВСЕ pending (без фильтра даты); d задан → задачи этой даты.
        # NB: include_no_date=True при d=None отфильтровал бы ТОЛЬКО задачи без даты
        # (tasks.py:728) — не использовать здесь.
        rows = tasks.list(tenant_id=tenant_id, user_id=user_id, scheduled_date=d)
        if not rows:
            return "Задач по этому запросу нет."
        return "\n".join(f"- ref={t.id} | {t.title} | {_fmt_task_when(t)}" for t in rows)

    @tool
    def add_task(title: str, scheduled_date: str = "", time_start: str = "",
                 notes: str = "") -> str:
        """Создать задачу. scheduled_date — YYYY-MM-DD (абсолютная), time_start — HH:MM.
        Без чек-листов и без напоминания-при-создании (пока не поддержано)."""
        d = None
        ts = None
        if scheduled_date:
            try:
                d = date.fromisoformat(scheduled_date.strip())
            except Exception:  # noqa: BLE001
                return f"Не разобрала дату: {scheduled_date!r}."
        if time_start:
            try:
                ts = time.fromisoformat(time_start.strip())
            except Exception:  # noqa: BLE001
                return f"Не разобрала время: {time_start!r}."
        t = tasks.add(tenant_id=tenant_id, user_id=user_id, title=title,
                      scheduled_date=d, time_start=ts, notes=notes or None)
        return f"ok:created:{t.id} | {t.title} | {_fmt_task_when(t)}"

    @tool
    def update_task(task_ref: str, title: str = "", notes: str = "") -> str:
        """Изменить ТЕКСТ задачи (название/заметки). Перенос по времени пока не поддержан."""
        t0 = tasks._get(tenant_id, user_id, task_ref)  # noqa: SLF001
        if t0 is None:
            return "Такой задачи у тебя нет."
        # no-op guard (#162 п.5): те же значения → успех без записи (replay не двигает updated_at).
        new_title = (title or "").strip()[:500] or None
        new_notes = (notes or "").strip() or None
        if ((new_title is None or new_title == (t0.title or None))
                and (new_notes is None or new_notes == t0.notes)):
            return f"ok:updated:{t0.id} | {t0.title}"
        t = tasks.update(tenant_id=tenant_id, user_id=user_id, task_id=task_ref,
                         title=title or None, notes=notes or None)
        return f"ok:updated:{t.id} | {t.title}" if t else "Такой задачи у тебя нет."

    @tool
    def complete_task(task_ref: str) -> str:
        """Отметить задачу выполненной."""
        t = tasks.complete(tenant_id=tenant_id, user_id=user_id, task_id=task_ref)
        return "Готово, отметила выполненной." if t else "Такой задачи у тебя нет."

    @tool
    def uncomplete_task(task_ref: str) -> str:
        """Вернуть задачу в работу (снять отметку «выполнено»)."""
        t = tasks.uncomplete(tenant_id=tenant_id, user_id=user_id, task_id=task_ref)
        return "Готово, вернула в работу." if t else "Такой задачи у тебя нет."

    def _confirm_destructive_task(task_ref: str, verb: str, apply) -> str:
        """Общий confirm-wrapper для cancel/delete задачи: снимок→interrupt→act."""
        t = tasks._get(tenant_id, user_id, task_ref)  # noqa: SLF001 — внутр. lookup сервиса
        if t is None:
            return "Такой задачи у тебя нет."
        title = t.title
        decision = interrupt(f"Точно {verb} задачу «{title}»?")
        if not _is_yes(str(decision)):
            return f"Хорошо, не трогаю «{title}»."
        ok = apply(task_ref)
        # ok=False → задача уже отсутствует/отменена (idempotent replay) → успех.
        return f"Готово, {verb}: «{title}»."

    @tool
    def cancel_task(task_ref: str) -> str:
        """Отменить задачу по ref. САМ спрашивает подтверждение."""
        # idempotent pre-check (Codex MAJOR): уже отменена → не переспрашивать.
        t0 = tasks._get(tenant_id, user_id, task_ref)  # noqa: SLF001
        if t0 is not None and t0.status == "cancelled":
            return f"Задача «{t0.title}» уже отменена."

        def _apply(ref: str) -> bool:
            return tasks.cancel(tenant_id=tenant_id, user_id=user_id, task_id=ref) is not None
        return _confirm_destructive_task(task_ref, "отменяю", _apply)

    @tool
    def delete_task(task_ref: str) -> str:
        """Удалить задачу по ref. САМ спрашивает подтверждение."""
        def _apply(ref: str) -> bool:
            return tasks.delete(tenant_id=tenant_id, user_id=user_id, task_id=ref)
        return _confirm_destructive_task(task_ref, "удаляю", _apply)

    @tool
    def ask_human(question: str) -> str:
        """Задать пользователю уточняющий вопрос (какое из нескольких) и дождаться ответа."""
        return str(interrupt(question))

    bespoke = [
        list_reminders, schedule_reminder, update_reminder, cancel_reminder,
        list_tasks, add_task, update_task, complete_task, uncomplete_task,
        cancel_task, delete_task, ask_human,
    ]

    # #162 полный перенос — добираем остальные семьи из общего реестра
    # (покупки/меню/рецепты/чек-листы/семья/веб) + память. Напоминания/задачи
    # отданы бес­поке выше (именованный confirm + within-turn идемпотентность).
    from sreda.runtime.tools import build_memory_tools
    from sreda.services.embeddings import get_embeddings_client
    from sreda.services.housewife_chat_tools import build_housewife_tools
    from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST

    _emb = get_embeddings_client()
    _bespoke_names = {t.name for t in bespoke}
    extra: list = []
    for t in build_housewife_tools(
        session=session, tenant_id=tenant_id, user_id=user_id,
        pending_buttons_state=None, menu_display_state=None,
        embedding_client=_emb,
    ):
        if t.name in _bespoke_names:
            continue  # напоминания/задачи уже у бес­поке
        if TOOL_FAMILY_MANIFEST.get(t.name) not in _EXTRA_FAMILIES:
            continue  # onboarding/ui/utility/tasks-cross — вне цикла
        extra.append(
            _confirm_wrap(t, _CONFIRM_PHRASE[t.name])
            if t.name in _CONFIRM_PHRASE else t
        )
    # память + веб; фильтруем по семье и дедупим — иначе утекает
    # log_unsupported_request (utility), которого в цикле быть не должно (Codex MINOR).
    _seen = {t.name for t in bespoke} | {t.name for t in extra}
    for t in build_memory_tools(
        session=session, tenant_id=tenant_id, user_id=user_id, embedding_client=_emb,
    ):
        if t.name in _seen:
            continue
        if TOOL_FAMILY_MANIFEST.get(t.name) not in {"memory", "web"}:
            continue
        _seen.add(t.name)
        extra.append(t)
    return bespoke + extra


def _build_graph(llm_with_tools: Any, tools_by_name: dict, *,
                 tenant_id: str, user_id: str, today_str: str):
    system_prompt = _system_prompt(today_str)

    def chat(state: ReactState):
        return {"messages": [llm_with_tools.invoke(
            [SystemMessage(system_prompt), *state["messages"]])]}

    def run_tools(state: ReactState):
        turn_key = state.get("turn_key") or ""
        exec_id = (hashlib.sha1(turn_key.encode("utf-8")).hexdigest()
                   if turn_key else "")
        out = []
        for tc in state["messages"][-1].tool_calls:
            # ctx per tool_call: turn_key (из state, переживает resume) + step_id=tc id
            # (из checkpointed AIMessage) → operation_id стабилен при перевыполнении узла.
            if turn_key:
                # ctx.operation_id — per-STEP id (для будущего emit_event/#163); в срезе
                # НЕ дедуп-ключ create: сервисы пересчитывают row-ключ через
                # compute_operation_id_create(plan_id=execution_id, step_id, logical_key).
                op_id = allocate_operation_id(
                    turn_key=turn_key, step_id=tc["id"], tool_name=tc["name"])
                ctx = ToolRuntimeContext(
                    operation_id=op_id, execution_id=exec_id, step_id=tc["id"],
                    tool_name=tc["name"], tenant_id=tenant_id, user_id=user_id,
                    turn_key=turn_key)
                with bind_tool_runtime(ctx):
                    res = tools_by_name[tc["name"]].invoke(tc["args"])
            else:
                res = tools_by_name[tc["name"]].invoke(tc["args"])
            out.append(ToolMessage(content=str(res), name=tc["name"],
                                   tool_call_id=tc["id"]))
        return {"messages": out}

    def route(state: ReactState):
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    g = StateGraph(ReactState)
    g.add_node("chat", chat)
    g.add_node("tools", run_tools)
    g.add_edge(START, "chat")
    g.add_conditional_edges("chat", route, {"tools": "tools", END: END})
    g.add_edge("tools", "chat")
    assert _TOPOLOGY_VERSION == "react-v2:chat,tools,turn_key"  # топология фиксирована
    return g.compile(checkpointer=_CHECKPOINTER)


def _scrub_ids(text: str) -> str:
    import re
    # ref=/id=/rem_/task_/checklist_<hex> + скобочные «(ref …)» не должны утечь пользователю.
    t = text or ""
    t = re.sub(r"\(\s*(?:ref|id)\b[^)]*\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bref\s*[:=]\s*\S+|\bid\s*[:=]\s*\S+", "", t, flags=re.IGNORECASE)
    # {12,} hex: id-формы (rem_/task_/… = 24 hex) снимаем, но «task_face» и короткие
    # слова не трогаем (Codex MINOR — граница длины).
    t = re.sub(r"\b(?:rem|task|checklist|sh)_[0-9a-f]{12,}", "", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([)\].,!?])", r"\1", t)
    return t.strip()


def _interrupt_age_seconds(created_at: Any) -> float:
    """Возраст текущего снимка checkpoint (TTL). FAIL-CLOSED: при отсутствии/невалидности
    created_at → inf (недатированную паузу НЕ возобновляем, ротируем поколение)."""
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
    """Нормализация контента ответа модели в строку (reasoning-блоки → текст)."""
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
    llm: Any, user_text: str, inbound_message_id: str = "", channel: str = "react",
) -> str:
    """ВХОД нового цикла на одно входящее сообщение. Источник правды о паузе — сам
    checkpoint (snap.next + snap.created_at). turn_key минтится РАЗ на свежий ход из
    durable inbound_message_id и живёт в state (переживает resume). НИКОГДА не поднимает
    исключений — при сбое отдаёт безопасный fallback."""
    base = thread_id
    gen = _THREAD_GEN.get(base, 0)

    def _cfg(g: int) -> dict:
        return {"configurable": {"thread_id": f"{base}#{g}"}}

    try:
        # дата-якорь для резолва относительных дат моделью (МСК = UTC+3)
        from datetime import timedelta
        today_str = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d (%A)")

        tools = build_slice_tools(session, tenant_id, user_id)
        graph = _build_graph(
            llm.bind_tools(tools), {t.name: t for t in tools},
            tenant_id=tenant_id, user_id=user_id, today_str=today_str)

        snap = await graph.aget_state(_cfg(gen))
        live_pause = (bool(snap.next)
                      and _interrupt_age_seconds(snap.created_at) <= _PENDING_TTL_SECONDS)

        if live_pause:  # живое уточнение → возобновляем (turn_key уже в state)
            result = await graph.ainvoke(Command(resume=user_text), _cfg(gen))
        else:
            if snap.next:  # протухшая пауза на этом поколении → свежий ход на чистом треде
                gen += 1
                _THREAD_GEN[base] = gen
            # turn_key минтится РАЗ на свежий ход; durable inbound id (не in-memory счётчик).
            turn_key = f"react:{channel}:{tenant_id}:{inbound_message_id or thread_id}"
            result = await graph.ainvoke(
                {"messages": [HumanMessage(user_text)], "turn_key": turn_key}, _cfg(gen))

        snap = await graph.aget_state(_cfg(gen))
        if snap.next:  # снова пауза → отдать вопрос пользователю
            return _scrub_ids(_pending_question(snap)) or "Уточни, пожалуйста."
        last = result["messages"][-1] if result.get("messages") else None
        text = _text_content(getattr(last, "content", "")) if isinstance(last, AIMessage) else ""
        return _scrub_ids(text) or "Готово."
    except Exception as exc:  # noqa: BLE001 — цикл не должен ронять ход
        # PII-safe: только тип ошибки + поколение, БЕЗ traceback и str(exc).
        logger.warning("react_loop: handle_turn failed type=%s gen=%s",
                       type(exc).__name__, gen)
        _THREAD_GEN[base] = gen + 1
        return ("Ой, я потеряла контекст этого диалога. Повтори, пожалуйста, "
                "что нужно сделать.")
