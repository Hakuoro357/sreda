"""#393 — заземление финальной реплики на РЕЗУЛЬТАТ успешного мутирующего действия.

Класс #376 («сделала, но сказала не то»): после успешного write модель на пост-tool проходе
отписывается генериком («Хорошо, приняла к сведению.») или дампом чужих данных, игнорируя
результат исполненного действия. Прод-репро 18.07 (tenant_max_40921122, легаси-путь, SGR OFF):
  A. add_checklist_items ok (3 пункта в БД) → «Хорошо, приняла к сведению.»
  B. archive_checklist ok (архив) → «Вот твои списки дел: …» (дамп вместо «убрала Дачу»).

Вариант владельца **D** (заземление голоса + страховка), PATH-AGNOSTIC (легаси и unified):
  1. ``grounding_note`` — чистая человеческая сводка результата, инжектится в контекст ПЕРЕД
     финальным проходом, чтобы живой голос (#121) озвучил её тепло, назвав имена.
  2. ``fallback_reply`` — детерминированная заземлённая реплика-СТРАХОВКА: если голос всё равно
     не назвал результат (детектор ``reply_grounds_result``), подменяем ею (точка финализации
     ``handle_turn``, прецедент ``_declined_reply`` #321). Тёплый шаблон, язык юзера, без
     англицизмов/тех-данных/длинного тире.

Имена берём из ``tool_call.args`` (имя списка/цели) + okv2-payload/args (сами пункты). Формат
возврата инструментов НЕ трогаем — его строго парсят regex-контракты в
``tool_schemas/housewife.py`` и structured-выходы (#115); менять его = ломать их.

Все функции ЧИСТЫЕ (без I/O) → тестируются изолированно и через ``handle_turn`` e2e.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, ToolMessage

from sreda.services.tool_schemas.families import TOOL_OP_CLASS
from sreda.services.tool_schemas.tool_ok_codec import is_okv2

# id-формы аргументов (checklist_<hex>/rem_<hex>/task_<hex>/…) — НЕ человеческое имя.
_ID_RE = re.compile(r"^[a-zа-я]+_[0-9a-f]{12,}$", re.IGNORECASE)
# Поля args, несущие человеческое имя цели (в порядке приоритета).
_NAME_FIELDS: tuple[str, ...] = ("list_id_or_title", "title", "name")


@dataclass(frozen=True)
class WriteAct:
    """Успешный мутирующий акт текущего хода, пригодный к называнию.

    ``target`` — человеческое имя цели (список/задача/напоминание), «» если неизвестно
    (id-аргумент/неподдержанное поле). ``items`` — добавленные пункты (для add-действий;
    авторитетно из okv2-результата, иначе из args)."""

    tool: str
    target: str
    items: tuple[str, ...]


def _is_id(value: str) -> bool:
    return bool(_ID_RE.match((value or "").strip()))


def _target(name: str, args: dict) -> str:
    for field in _NAME_FIELDS:
        val = args.get(field)
        if isinstance(val, str) and val.strip() and not _is_id(val):
            return val.strip()
    return ""


def _okv2_created(content: str) -> tuple[str, ...] | None:
    """okv2-payload несёт ФАКТИЧЕСКИ добавленные имена (dedup-aware, #115). None — если не
    okv2 / нет ключа ``created`` / битый payload. Пустой кортеж — все пункты были дублями."""
    if not is_okv2(content):
        return None
    try:
        body = content.split(":", 2)[2]
        payload = json.loads(body)
    except (IndexError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    created = payload.get("created")
    if isinstance(created, list):
        return tuple(str(x).strip() for x in created if isinstance(x, str) and x.strip())
    return None


def _items(name: str, args: dict, content: str) -> tuple[str, ...]:
    created = _okv2_created(content)
    if created is not None:
        return created  # авторитетно (может быть пусто = все дубли)
    raw = args.get("items")
    if isinstance(raw, list):
        return tuple(str(x).strip() for x in raw if isinstance(x, str) and x.strip())
    return ()


def _is_success(tm: ToolMessage) -> bool:
    """Успех = result_kind==ok И протокол-префикс ok:/okv2:. КРИТИЧНО: confirm-ОТКАЗ обёртки
    возвращает «Хорошо, не трогаю.» с result_kind==ok — БЕЗ префикса → не успех (иначе
    отменённый archive принялся бы за успех = ЛОЖНЫЙ УСПЕХ, хуже филлера). no-op/error тоже."""
    art = getattr(tm, "artifact", None) or {}
    if not (isinstance(art, dict) and art.get("result_kind") == "ok"):
        return False
    content = str(getattr(tm, "content", "") or "")
    return is_okv2(content) or content.startswith("ok:")


def collect_successful_writes(messages) -> tuple[WriteAct, ...]:
    """Успешные мутирующие акты ТЕКУЩЕГО хода (после последнего HumanMessage), пригодные к
    называнию (есть имя цели ИЛИ пункты). Неназемляемые (id-only/неподдержанные) пропускаем —
    там голос остаётся как есть (safe). Resume-путь (confirm «да») нового HumanMessage не
    добавляет → окно захватывает пост-confirm write."""
    msgs = list(messages or [])
    start = 0
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            start = i + 1
            break
    window = msgs[start:]
    results: dict = {}
    for m in window:
        if isinstance(m, ToolMessage):
            results[getattr(m, "tool_call_id", None)] = m
    acts: list[WriteAct] = []
    for m in window:
        for tc in (getattr(m, "tool_calls", None) or []):
            name = tc.get("name")
            if TOOL_OP_CLASS.get(name) != "write":
                continue
            tm = results.get(tc.get("id"))
            if tm is None or not _is_success(tm):
                continue
            args = tc.get("args") or {}
            content = str(getattr(tm, "content", "") or "")
            target = _target(name, args)
            items = _items(name, args, content)
            if not target and not items:
                continue  # нечего называть → голос как есть
            acts.append(WriteAct(tool=name, target=target, items=items))
    return tuple(acts)


# ─────────────────────────── детектор заземлённости (объективный) ───────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", (text or "").lower().replace("ё", "е")).strip()


def _stem(word: str) -> str:
    """Устойчивость к морфологии (класс #180: точный матч по живому языку хрупок): длинное
    слово → префикс, короткое → минус окончание. «лопату»/«лопата» → «лопат»; «Дача»→«дач»."""
    if len(word) >= 6:
        return word[:5]
    if len(word) >= 4:
        return word[:-1]
    return word


def _name_mentioned(reply_norm: str, name: str) -> bool:
    toks = [t for t in _norm(name).split() if len(t) >= 4]
    if not toks:
        whole = _norm(name)
        return bool(whole) and whole in reply_norm
    return any(_stem(t) in reply_norm for t in toks)


def reply_grounds_result(reply: str, acts) -> bool:
    """Реплика НАЗЫВАЕТ результат хотя бы одного акта? (объективно — по именам объекта действия,
    НЕ по списку фраз-отписок, который хрупок). add-акт заземлён, если назван хотя бы один пункт;
    иначе (архив/создание/цель без пунктов) — если названа цель. Подмена страховкой — когда НЕ
    заземлён НИ ОДИН акт (формулировка владельца «реплика не называет результат»); это оберегает
    живой голос от лишних подмен (#121)."""
    reply_norm = _norm(reply)
    if not reply_norm:
        return False
    for act in acts:
        if act.items:
            if any(_name_mentioned(reply_norm, it) for it in act.items):
                return True
        elif act.target and _name_mentioned(reply_norm, act.target):
            return True
    return False


# ─────────────────────────── человеческие формулировки (тёплые, по-русски) ───────────────────────────

def _generic_verb(name: str) -> str:
    for prefix, verb in (
        ("add_", "добавила"), ("create_", "завела"), ("save_", "сохранила"),
        ("plan_", "составила"), ("generate_", "собрала"), ("schedule_", "поставила"),
        ("archive_", "убрала"), ("delete_", "удалила"), ("remove_", "убрала"),
        ("cancel_", "отменила"), ("clear_", "очистила"), ("complete_", "отметила выполненным"),
        ("uncomplete_", "вернула в работу"), ("mark_", "отметила"), ("update_", "обновила"),
        ("move_", "перенесла"), ("link_", "связала"), ("attach_", "прикрепила"),
        ("detach_", "открепила"), ("unlink_", "отвязала"),
    ):
        if name.startswith(prefix):
            return verb
    return "сделала"


def _phrase(act: WriteAct) -> str:
    """Тёплая человеческая формулировка ОДНОГО акта: что сделала с чем (с именами)."""
    tool, tgt = act.tool, (f"«{act.target}»" if act.target else "")
    items = ", ".join(act.items)
    if tool == "add_checklist_items":
        base = f"добавила в список {tgt}".rstrip()
        return f"{base}: {items}" if items else f"обновила список {tgt}".rstrip()
    if tool == "add_shopping_items":
        return f"добавила в список покупок: {items}" if items else "обновила список покупок"
    if tool == "create_checklist":
        return f"завела список {tgt}".rstrip()
    if tool == "archive_checklist":
        return f"убрала список {tgt}".rstrip()
    if tool == "add_task":
        return f"добавила задачу {tgt}".rstrip()
    if tool == "schedule_reminder":
        return f"поставила напоминание {tgt}".rstrip()
    verb = _generic_verb(tool)
    if items:
        return f"{verb}: {items}"
    if tgt:
        return f"{verb} {tgt}".rstrip()
    return verb


def _summary(acts) -> str:
    return "; ".join(_phrase(a) for a in acts if _phrase(a))


def grounding_note(acts) -> str:
    """Часть 1: чистая сводка результата для ЖИВОГО голоса (озвучить тепло, назвав имена).
    Пусто, если называть нечего."""
    if not acts:
        return ""
    return (
        "Ты только что успешно выполнила: " + _summary(acts) + ". "
        "В ответе тепло и коротко подтверди это человеку, ОБЯЗАТЕЛЬНО назвав список и пункты "
        "своими словами; ничего сверх этого результата не добавляй и не выдумывай. "
        "Эту служебную заметку дословно не пересказывай."
    )


def fallback_reply(acts) -> str:
    """Часть 2: детерминированная заземлённая реплика-страховка (тёплый шаблон, называет
    результат). Пусто, если называть нечего (тогда голос оставляем как есть)."""
    body = _summary(acts)
    if not body:
        return ""
    return f"Готово, {body}."


__all__ = [
    "WriteAct",
    "collect_successful_writes",
    "reply_grounds_result",
    "grounding_note",
    "fallback_reply",
]
