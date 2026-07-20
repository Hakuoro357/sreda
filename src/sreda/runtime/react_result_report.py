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

КОНТРАКТ КОРРЕКТНОСТИ (Codex terra R1):
  * НЕ ложный успех: заземляем ТОЛЬКО акты с ДОКАЗАННЫМ эффектом (add — okv2 `created` непусто;
    target-действия — по success-префиксу). confirm-ОТКАЗ («Хорошо, не трогаю.»), no-op/дубли
    (add all-dup, save_recipe duplicate) и create_checklist (возвращает СУЩЕСТВУЮЩИЙ список — не
    отличить от создания) НЕ заземляются. Явный allowlist — БЕЗ генерик-catch-all (иначе любой
    незнакомый write получал бы выдуманный глагол).
  * НЕ утечка тех-данных: имена цели/пунктов из args санитизируются (control-текст/id/длинное тире
    вычищаются, ``_sanitize``); неотображаемое имя → fail-closed (реплику не строим).
  * Детектор — по границам слов (токенный startswith-stem), НЕ подстрокой (иначе stem «дел» списка
    «Дела» ложно матчит «Сделала»).

Формат возврата инструментов НЕ трогаем — его строго парсят regex/okv2-контракты (#115).
Все функции ЧИСТЫЕ (без I/O) → тестируются изолированно и через ``handle_turn`` e2e.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, ToolMessage

from sreda.services.tool_schemas.families import TOOL_OP_CLASS
from sreda.services.tool_schemas.tool_ok_codec import is_okv2

# ─────────────────────────── санитизация имён (fail-closed) ───────────────────────────

_ID_RE = re.compile(r"^[a-zа-я]+_[0-9a-f]{12,}$", re.IGNORECASE)
_IDLIKE_TOKEN_RE = re.compile(r"\b[a-zа-я]+_[0-9a-f]{12,}\b", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EMDASH_RE = re.compile(r"[—–―]")  # длинное/среднее тире → обычный дефис (правило: без «—» юзеру)
_LATIN_RE = re.compile(r"[a-zA-Z]")  # латиница в детерминированном тексте = не «чистый русский»
_TECH_MARK_RE = re.compile(r"\b(?:id|ref|http|okv2|checklist|task|rem|clitem)\s*[=:]", re.IGNORECASE)
_MAX_NAME_LEN = 80


def _is_id(value: str) -> bool:
    return bool(_ID_RE.match((value or "").strip()))


def _sanitize(value: str) -> str:
    """Первичная чистка: control-символы, id-токены (checklist_<hex>…), длинное тире→дефис, схлоп
    пробелов, кап длины. Формы-чистоты (латиница/техмаркеры) проверяет `_clean_name`."""
    s = _CTRL_RE.sub(" ", str(value or ""))
    s = _EMDASH_RE.sub("-", s)
    s = _IDLIKE_TOKEN_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > _MAX_NAME_LEN:
        s = s[:_MAX_NAME_LEN].rstrip()
    return s


def _clean_name(value: str) -> str:
    """Безопасно ОТОБРАЖАЕМОЕ имя для детерминированного user-facing текста И инжекта в промпт
    (fail-closed, Codex sol+terra R1/R2): санитизация + отказ, если имя содержит ЛАТИНИЦУ (контракт
    «чистый русский без англицизмов»), техмаркеры (`id=`/`ref=`/`http`/…) или пусто/только-пунктуация.
    Пусто → небезопасно, вызывающий НЕ строит акт (голос как есть). Так ни латиница/техследы/
    инструкц-текст не утекут ни в fallback, ни в grounding_note."""
    s = _sanitize(value)
    if not s or _is_id(value):
        return ""
    if _LATIN_RE.search(s) or _TECH_MARK_RE.search(s):
        return ""
    if not re.search(r"[0-9а-яё]", s, re.IGNORECASE):  # ни буквы/цифры — только пунктуация
        return ""
    return s


# ─────────────────────────── аллоулист заземляемых действий ───────────────────────────

@dataclass(frozen=True)
class WriteAct:
    """Успешный мутирующий акт текущего хода с ДОКАЗАННЫМ эффектом, пригодный к называнию.

    ``kind`` — "add" (добавление пунктов; ``items`` непусто) или "target" (действие над одной
    именованной целью). ``target`` — чистое имя цели (может быть «» для add с неявной целью, напр.
    список покупок). ``items`` — санитизированные ДОБАВЛЕННЫЕ имена (авторитетно из okv2)."""

    tool: str
    kind: str
    target: str
    items: tuple[str, ...]


# add-действия: эффект = okv2 `created` непусто; поле имени цели (None = неявная цель).
_ADD_TOOLS: dict[str, str | None] = {
    "add_checklist_items": "list_id_or_title",
    "add_shopping_items": None,  # неявная цель — список покупок
}
# target-действия: (глагол, success-префикс результата, поле имени цели).
_TARGET_SPECS: dict[str, tuple[str, str, str]] = {
    "archive_checklist": ("убрала список", "ok:archived", "list_id_or_title"),
    "schedule_reminder": ("поставила напоминание", "ok:scheduled", "title"),
}


def _okv2_created(content: str) -> tuple[str, ...]:
    """okv2-payload несёт ФАКТИЧЕСКИ добавленные имена (`created`, dedup-aware #115). Пусто — если
    не okv2 / нет ключа / битый payload / all-dup (added_count=0). Санитизируется вызывающим."""
    if not is_okv2(content):
        return ()
    try:
        body = content.split(":", 2)[2]
        payload = json.loads(body)
    except (IndexError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    created = payload.get("created")
    if isinstance(created, list):
        return tuple(str(x) for x in created if isinstance(x, str) and x.strip())
    return ()


def _target_name(field: str | None, args: dict) -> str:
    if not field:
        return ""
    val = args.get(field)
    return _clean_name(val) if isinstance(val, str) else ""


def collect_successful_writes(messages) -> tuple[WriteAct, ...]:
    """Успешные мутирующие акты ТЕКУЩЕГО хода (после последнего HumanMessage) с ДОКАЗАННЫМ эффектом,
    пригодные к называнию. Аллоулист + проверка эффекта → no-op/дубли/отказ/create-existing НЕ
    заземляются (контракт «без ложного успеха»). Resume-путь (confirm «да») нового HumanMessage не
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
            if tm is None:
                continue
            art = getattr(tm, "artifact", None) or {}
            if not (isinstance(art, dict) and art.get("result_kind") == "ok"):
                continue
            content = str(getattr(tm, "content", "") or "")
            args = tc.get("args") or {}
            if name in _ADD_TOOLS:
                created = _okv2_created(content)
                if not created:  # all-dup / нераспарсено → нет эффекта, не заземляем
                    continue
                items = tuple(_clean_name(x) for x in created)
                if not all(items):  # ЛЮБОЙ пункт неотображаем (латиница/техследы) → fail-closed ВЕСЬ акт
                    continue
                field = _ADD_TOOLS[name]
                target = _target_name(field, args)
                if field and not target:  # checklist-add: имя списка ОБЯЗАНО быть отображаемым
                    continue  # (пустой target допустим только для add_shopping_items, field=None)
                acts.append(WriteAct(name, "add", target, items))
            elif name in _TARGET_SPECS:
                _verb, prefix, field = _TARGET_SPECS[name]
                if not content.startswith(prefix):  # нет эффекта (не тот исход)
                    continue
                target = _target_name(field, args)
                if not target:  # неотображаемое имя → fail-closed, не заземляем
                    continue
                acts.append(WriteAct(name, "target", target, ()))
            # прочие write-инструменты не заземляем (консервативно; голос как есть)
    return tuple(acts)


# ─────────────────────────── детектор заземлённости (объективный, по границам слов) ───────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", (text or "").lower().replace("ё", "е")).strip()


def _stem(word: str) -> str:
    """Устойчивость к морфологии (класс #180): длинное слово → префикс, короткое → минус окончание.
    «лопату»/«лопата» → «лопат»; «Дача»→«дач»."""
    if len(word) >= 6:
        return word[:5]
    if len(word) >= 4:
        return word[:-1]
    return word


def _name_mentioned(reply_tokens: list[str], name: str) -> bool:
    """Имя ПОЛНОСТЬЮ названо в реплике? Матч ПО ТОКЕНАМ (границы слов), не подстрокой — иначе stem
    «дел» списка «Дела» ложно матчит «Сделала» (Codex terra R1). Многословное имя → ВСЕ его значимые
    слова (≥4 симв.) названы (не «хотя бы одно» — иначе «молоко без лактозы»/«Дела на сегодня»
    засчитывались бы по одному общему слову, sol+terra R2). Каждое значимое слово → какой-то токен
    реплики начинается с его stem (морфология #180). Имя без значимых слов → точный токен целиком."""
    name_toks = [t for t in _norm(name).split() if len(t) >= 4]
    if not name_toks:
        whole = _norm(name)
        return bool(whole) and whole in reply_tokens
    return all(any(rt.startswith(_stem(nt)) for rt in reply_tokens) for nt in name_toks)


def reply_grounds_result(reply: str, acts) -> bool:
    """Реплика называет результат ВСЕХ актов? (объективно — по именам объекта действия, НЕ по списку
    фраз-отписок). add-акт заземлён, если назван КАЖДЫЙ добавленный пункт; target-акт — если названа
    цель. Подмена страховкой (в финализации) — когда заземлены НЕ ВСЕ акты: ловит и полный филлер, и
    ЧАСТИЧНЫЙ отчёт («Добавила грабли» при трёх добавленных / один акт из двух) — оба ревьюера R1
    (sol+terra) требовали полноты (цель+все пункты, все акты). Полный отчёт голоса (все имена) НЕ
    подменяется → живой голос (#121) сохранён; grounding_note (часть 1) подталкивает назвать всё."""
    if not acts:
        return True
    reply_tokens = _norm(reply).split()
    if not reply_tokens:
        return False
    for act in acts:
        if act.kind == "add":
            # add-акт заземлён ⟺ названы И имя списка (если есть; checklist-add его гарантирует
            # fail-closed), И КАЖДЫЙ добавленный пункт (sol+terra R2: приёмка «имя списка + пункты»).
            if act.target and not _name_mentioned(reply_tokens, act.target):
                return False
            if not (act.items and all(_name_mentioned(reply_tokens, it) for it in act.items)):
                return False
        elif not (act.target and _name_mentioned(reply_tokens, act.target)):
            return False
    return True


# ─────────────────────────── человеческие формулировки (тёплые, по-русски, санитизированные) ───────────────────────────

def _phrase(act: WriteAct) -> str:
    """Тёплая человеческая формулировка ОДНОГО акта: что сделала с чем (имена уже санитизированы
    в collect_*). Пусто — если называть нечего (fail-closed)."""
    if act.kind == "add":
        items = ", ".join(i for i in act.items if i)
        if not items:
            return ""
        if act.tool == "add_shopping_items":
            return f"добавила в список покупок: {items}"
        if act.target:
            return f"добавила в список «{act.target}»: {items}"
        return f"добавила пункты: {items}"
    spec = _TARGET_SPECS.get(act.tool)
    if spec and act.target:
        return f"{spec[0]} «{act.target}»"
    return ""


def _summary(acts) -> str:
    return "; ".join(p for p in (_phrase(a) for a in acts) if p)


def grounding_note(acts) -> str:
    """Часть 1: чистая сводка результата для ЖИВОГО голоса (озвучить тепло, назвав имена).
    Пусто, если называть нечего."""
    body = _summary(acts)
    if not body:
        return ""
    return (
        "Ты только что успешно выполнила: " + body + ". "
        "В ответе тепло и коротко подтверди это человеку, ОБЯЗАТЕЛЬНО назвав список и пункты "
        "своими словами; ничего сверх этого результата не добавляй и не выдумывай. "
        "Эту служебную заметку дословно не пересказывай."
    )


def fallback_reply(acts) -> str:
    """Часть 2: детерминированная заземлённая реплика-страховка (тёплый шаблон, называет результат).
    Пусто, если называть нечего (fail-closed — тогда голос оставляем как есть)."""
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
