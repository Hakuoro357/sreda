"""#393 — заземление финальной реплики на РЕЗУЛЬТАТ успешного мутирующего действия.

Класс #376 («сделала, но сказала не то»): после успешного write модель на пост-tool проходе
отписывается генериком («Хорошо, приняла к сведению.») или дампом чужих данных, игнорируя
результат исполненного действия. Прод-репро 18.07 (tenant_max_40921122, легаси-путь, SGR OFF):
  A. add_checklist_items ok (3 пункта в БД) → «Хорошо, приняла к сведению.»
  B. archive_checklist ok (архив) → «Вот твои списки дел: …» (дамп вместо «убрала Дачу»).

Решение владельца (**вариант C**), PATH-AGNOSTIC (легаси и unified):
  1. ``grounding_note`` (часть 1, инжектится в ПРОМПТ узла chat) — строится ТОЛЬКО из
     СЕРВЕР-КОНТРОЛИРУЕМЫХ фактов результата инструмента: статус (успех) + количество + тип
     объекта/семья. БЕЗ сырых имён списка/пунктов. Заряжает «ты успешно сделала — назови юзеру
     конкретно что». **Ни одна юзер-контролируемая строка не входит в ИНСТРУКЦИИ модели** →
     инъекц-surface на до-#393 базлайне (имя есть лишь в истории data-role, НЕ ре-инжектится в
     авторитетный хвост). Так terra-CRITICAL и развилка A/B сняты СТРУКТУРНО.
  2. ИМЕНА — только в ВЫВОДЕ юзеру: (а) живой голос называет их, читая из tool-результата в
     истории (data-role, не новый surface); (б) детерминированная страховка ``fallback_reply``
     (часть 2, финализация ``handle_turn``, прецедент ``_declined_reply`` #321) вставляет имена
     из РЕЗУЛЬТАТА инструмента (пункты — okv2 `created`, канонические). Вывод юзеру — НЕ
     инъекц-поверхность. Дисплей-гигиена имён (`_clean_name`/`_postformat`) — отдельно от инъекции.

КОНТРАКТ КОРРЕКТНОСТИ (Codex sol+terra R1-R4):
  * НЕ ложный успех: заземляем ТОЛЬКО акты с ДОКАЗАННЫМ эффектом (add — okv2 `created` непусто;
    target — success-префикс). confirm-ОТКАЗ / no-op / дубли / create-existing НЕ заземляются.
  * Явный allowlist заземляемых действий — БЕЗ генерик-catch-all.
  * Дисплей-гигиена имён: латиница/тех-конструкции/id/control → не отображаем; ИМЯ неотображаемо →
    страховка ГРАЦИОЗНО деградирует к серверным фактам (кол-во+тип), а НЕ молчит (owner R4-c).
  * Детектор — по границам слов (токены; короткие слова — точный матч, длинные — stem), полнота
    (все пункты + имя списка), пропускает акты без отображаемых имён.

Формат возврата инструментов НЕ трогаем — его строго парсят regex/okv2-контракты (#115).
Все функции ЧИСТЫЕ (без I/O).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, ToolMessage

from sreda.services.tool_schemas.families import TOOL_OP_CLASS
from sreda.services.tool_schemas.tool_ok_codec import is_okv2

# ─────────────────────────── дисплей-гигиена имён (для ВЫВОДА, не инъекции) ───────────────────────────

_ID_RE = re.compile(r"^[a-zа-я]+_[0-9a-f]{12,}$", re.IGNORECASE)
_IDLIKE_TOKEN_RE = re.compile(r"\b[a-zа-я]+_[0-9a-f]{12,}\b", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EMDASH_RE = re.compile(r"[—–―]")  # длинное/среднее тире → обычный дефис (правило «без «—» юзеру»)
_LATIN_RE = re.compile(r"[a-zA-Z]")  # латиница в детерминированном тексте = не «чистый русский»
# WHITELIST отображаемого имени: кириллица/цифры/пробел/дефис/обычная пунктуация/кавычки/скобки.
# Тех-конструкции (=/;/:/`/{}/<>/|/…) вне whitelist → имя неотображаемо (дисплей-гигиена, owner «оставить»).
_ALLOWED_NAME_RE = re.compile(r"^[0-9а-яё \-.,!?«»()]+$", re.IGNORECASE)
_MAX_DISPLAY_NAME = 64  # длиннее → graceful ТРУНКЕЙТ для показа (не drop, не сырой филлер — owner R4-c)
# машинная утечка в ЛЮБОЙ финальной реплике (вкл. заземлённый ответ модели): okv2-конверт, key=hex-id,
# id=/ref=. Длинное тире НЕ здесь (Opus R4): `_postformat._strip_tech_leak` и так БЕЗУСЛОВНО «—»→дефис
# ПОСЛЕ выбора текста → клауза была избыточна и ложно подменяла заземлённый голос с тире. Латиницу тоже
# не включаем — это может быть КОНТЕНТ юзера (его пункт), а не техслед.
_TECH_LEAK_RE = re.compile(r"okv2:|\b(?:id|ref)\s*=|_[0-9a-f]{12,}", re.IGNORECASE)
# служебные слова (детектор полноты их НЕ требует; короткие СУЩЕСТВИТЕЛЬНЫЕ сюда НЕ входят — «сыр»/«дом»/
# «чай»/«суп» обязательны, sol+terra R3). Только предлоги/союзы/частицы/местоимения.
_STOPWORDS: frozenset[str] = frozenset(
    "и в во на с со по для из к ко о об от у за до не а но же ли бы то или "
    "без при над под про через между перед у я мы ты вы он она оно они это эта этот эти "
    "мой моя мои наш наша наши да нет".split()
)


def reply_has_tech_leak(text: str) -> bool:
    """Финальная реплика несёт машинную утечку (okv2-конверт / id=/ref= / сырой hex-id)? Тогда даже
    ЗАЗЕМЛЁННЫЙ ответ модели заменяем чистой страховкой. Длинное тире СЮДА НЕ входит (Opus R4):
    `_postformat` его и так безусловно чистит; латиница — контент юзера, не утечка."""
    return bool(_TECH_LEAK_RE.search(text or ""))


def _is_id(value: str) -> bool:
    return bool(_ID_RE.match((value or "").strip()))


def _sanitize(value: str) -> str:
    """Первичная чистка: control-символы, id-токены (checklist_<hex>…), длинное тире→дефис, схлоп пробелов."""
    s = _CTRL_RE.sub(" ", str(value or ""))
    s = _EMDASH_RE.sub("-", s)
    s = _IDLIKE_TOKEN_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _clean_name(value: str) -> str:
    """Безопасно ОТОБРАЖАЕМОЕ имя для ВЫВОДА юзеру (дисплей-гигиена; вариант C — в ПРОМПТ имена НЕ
    попадают, потому это не про инъекцию). «» (неотображаемо), если имя: пустое/id; латиница; символ
    вне whitelist (тех-конструкция =/;/…); только пунктуация. Длинное легит-имя → ГРАЦИОЗНЫЙ трункейт
    (owner R4-c: не drop, не молчаливый филлер). Неотображаемо → страховка деградирует к фактам."""
    s = _sanitize(value)
    if not s or _is_id(value):
        return ""
    if _LATIN_RE.search(s) or not _ALLOWED_NAME_RE.match(s):
        return ""
    if not re.search(r"[0-9а-яё]", s, re.IGNORECASE):  # хотя бы слово/цифра, не только пунктуация
        return ""
    if len(s) > _MAX_DISPLAY_NAME:  # graceful трункейт по границе слова
        s = s[:_MAX_DISPLAY_NAME].rsplit(" ", 1)[0].strip() or s[:_MAX_DISPLAY_NAME].strip()
    return s


# ─────────────────────────── аллоулист заземляемых действий ───────────────────────────

@dataclass(frozen=True)
class WriteAct:
    """Успешный мутирующий акт текущего хода с ДОКАЗАННЫМ эффектом.

    ``kind`` — "add" (добавление пунктов), "target" (действие над одной целью) или "bulk"
    (#409: массовое действие БЕЗ имени цели — «очисти весь список покупок»). ``target`` —
    ОТОБРАЖАЕМОЕ имя цели («» если неотображаемо/неявно — тогда деградация к типу; у bulk всегда «»).
    ``items`` — ОТОБРАЖАЕМЫЕ добавленные имена (подмножество; из okv2 `created`). ``count`` —
    серверное КОЛИЧЕСТВО затронутого (для add — сколько реально добавлено; для target — 1; для
    bulk — сколько строк реально затронуто); из результата, не из args."""

    tool: str
    kind: str
    target: str
    items: tuple[str, ...]
    count: int


# add-действия: эффект = okv2 `created` непусто; поле имени цели (None = неявная цель — список покупок).
_ADD_TOOLS: dict[str, str | None] = {
    "add_checklist_items": "list_id_or_title",
    "add_shopping_items": None,
}
# add-тип объекта (для серверных фактов grounding_note + деградации fallback).
_ADD_WHERE: dict[str, str] = {
    "add_checklist_items": "чек-лист",
    "add_shopping_items": "список покупок",
}
# target-действия: (глагол, success-префикс результата, поле имени цели, тип-фраза без имени).
_TARGET_SPECS: dict[str, tuple[str, str, str, str]] = {
    "archive_checklist": ("убрала список", "ok:archived", "list_id_or_title", "заархивирован чек-лист"),
    "schedule_reminder": ("поставила напоминание", "ok:scheduled", "title", "поставлено напоминание"),
}
# #409 bulk-действия: массовая мутация БЕЗ имени цели (у «очисти весь список покупок» имени нет by
# design — инструмент без аргументов). Формат: (глагол для вывода, success-префикс с КОЛИЧЕСТВОМ
# после него, тип-фраза для промпта, единицы). Отдельный kind, а не _TARGET_SPECS, потому что:
#   * count у target захардкожен в 1 — здесь количество и есть суть результата («убрала N позиций»);
#   * заземлённость у target проверяется по ИМЕНИ цели, а у bulk имени нет — проверяем по числу
#     (иначе `not act.target` всегда давал бы «не заземлено» → живой голос подменялся бы ВСЕГДА).
# #409 R2 (sol+terra, независимо оба): для bulk-деструктива СВОБОДНЫЙ ТЕКСТ МОДЕЛИ НЕ СУДИМ
# ВООБЩЕ. История: R1 — критерий «названо число» пропускал «напомню через 5 минут» (ложный
# положительный); R2 — критерий «число + доменный якорь» пропускал «в списке покупок 5 позиций»
# (доменный якорь ≠ признак ВЫПОЛНЕННОГО действия) и заодно резал корректное «убрала 5 позиций
# из списка» (ложный отрицательный). Наращивать русские паттерны запрещено правилом проекта
# («недетерминизм чинить промптом, не языковыми паттернами»). Потому: результат bulk-действия
# всегда отдаём ДЕТЕРМИНИРОВАННОЙ квитанцией из СТРУКТУРНОГО исхода инструмента. Это и проще
# (эвристика удалена, а не усложнена), и закрывает оба класса ошибок сразу.
#
# Поля: (глагол успеха, success-префикс с КОЛИЧЕСТВОМ после него, тип-фраза для промпта,
#        единицы, текст «нечего было делать», текст «не получилось»).
_BULK_SPECS: dict[str, tuple[str, str, str, tuple[str, str, str], str, str]] = {
    "clear_shopping_list": (
        "убрала весь список покупок", "ok:cleared:", "очищен список покупок",
        ("позиция", "позиции", "позиций"),
        "список покупок и так был пуст",
        "не получилось очистить список покупок",
    ),
}


@dataclass(frozen=True)
class BulkOutcome:
    """#409 R2: ТЕРМИНАЛЬНЫЙ исход bulk-инструмента текущего хода — включая те, что НЕ являются
    успехом с эффектом. ``kind``: "cleared" (N>0) | "empty" (N=0, чистить было нечего) |
    "error" (инструмент не отработал). Нужен именно отдельный сбор: ``collect_successful_writes``
    по контракту #393 отдаёт ТОЛЬКО доказанные эффекты, и потому после ok:cleared:0 / error
    страховка не включалась вовсе — модель могла безнаказанно отрапортовать «убрала всё»
    (R1/R2 MAJOR M2, оба ревьюера отклонили довод «это вне scope»)."""

    tool: str
    kind: str
    count: int


def _okv2_created(content: str) -> tuple[str, ...]:
    """okv2-payload несёт ФАКТИЧЕСКИ добавленные имена (`created`, dedup-aware #115). Пусто — если не
    okv2 / нет ключа / битый payload / all-dup (added_count=0)."""
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
    """Успешные мутирующие акты ТЕКУЩЕГО хода (после последнего HumanMessage) с ДОКАЗАННЫМ эффектом.
    Аллоулист + проверка эффекта → no-op/дубли/отказ/create-existing НЕ заземляются («без ложного
    успеха»). Неотображаемое имя акт НЕ роняет (вариант C) — несёт `count`/тип для грациозной
    деградации. Resume-путь (confirm «да») нового HumanMessage не добавляет → окно ловит пост-confirm."""
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
                items = tuple(c for c in (_clean_name(x) for x in created) if c)  # отображаемое подмножество
                target = _target_name(_ADD_TOOLS[name], args)  # «» если неотображаемо
                acts.append(WriteAct(name, "add", target, items, len(created)))
            elif name in _TARGET_SPECS:
                _verb, prefix, field, _type = _TARGET_SPECS[name]
                if not content.startswith(prefix):  # нет эффекта (не тот исход)
                    continue
                acts.append(WriteAct(name, "target", _target_name(field, args), (), 1))
            elif name in _BULK_SPECS:
                # #409: эффект ДОКАЗАН количеством из результата. N=0 («список уже был пуст») —
                # no-op: НЕ заземляем, иначе страховка отрапортовала бы ложный успех («убрала
                # весь список покупок») там, где ничего не убрано. Битый хвост → тоже не заземляем.
                _verb, prefix, _type, _units_, _empty, _err = _BULK_SPECS[name]
                if not content.startswith(prefix):
                    continue
                try:
                    n = int(content[len(prefix):].strip())
                except ValueError:
                    continue
                if n <= 0:
                    continue
                acts.append(WriteAct(name, "bulk", "", (), n))
            # прочие write-инструменты не заземляем (консервативно; голос как есть)
    return tuple(acts)


# ─────────────────────────── детектор заземлённости (объективный, по границам слов) ───────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", (text or "").lower().replace("ё", "е")).strip()


def _stem(word: str) -> str:
    if len(word) >= 6:
        return word[:5]
    return word[:-1]  # 5 символов → минус окончание (короче обрабатывает _name_mentioned точным матчем)


def _name_mentioned(reply_tokens: list[str], name: str) -> bool:
    """Имя ПОЛНОСТЬЮ названо в реплике? Матч ПО ТОКЕНАМ (границы слов), не подстрокой. Многословное имя
    → ВСЕ значимые (non-stopword) слова названы (не одно общее — «молоко без лактозы»/«сыр и хлеб»/«Дом
    сегодня» по одному не проходят). Короткое слово (<5) → ТОЧНЫЙ токен (нет ложного «чай»→«чайник»,
    owner R4-d); длинное → токен реплики начинается со stem (морфология #180)."""
    name_toks = [t for t in _norm(name).split() if len(t) >= 2 and t not in _STOPWORDS]
    if not name_toks:
        whole = _norm(name)
        return bool(whole) and whole in reply_tokens

    def _hit(nt: str) -> bool:
        if len(nt) < 4:  # 2-3 симв. («чай»/«сыр»/«дом») → ТОЧНЫЙ токен (нет «чай»→«чайник», owner R4-d)
            return nt in reply_tokens
        return any(rt.startswith(_stem(nt)) for rt in reply_tokens)  # len≥4 → stem («Дача»→«дач»→«дачу»)

    return all(_hit(nt) for nt in name_toks)


def reply_grounds_result(reply: str, acts) -> bool:
    """Реплика называет результат ВСЕХ актов (по ОТОБРАЖАЕМЫМ именам объекта действия)? add-акт —
    названы имя списка (если отображаемо) И КАЖДЫЙ отображаемый пункт; target-акт — названа цель.
    Акт БЕЗ отображаемых имён (латиница/id) в проверку НЕ вносит требования (пропускаем — по именам
    не верифицировать; grounding_note-факт всё равно заряжал модель). Подмена (в финализации) — когда
    заземлены не все проверяемые акты: ловит полный филлер и ЧАСТИЧНЫЙ отчёт. Полный отчёт голоса (все
    имена) НЕ подменяется → живой голос (#121) сохранён."""
    if not acts:
        return True
    reply_tokens = _norm(reply).split()
    if not reply_tokens:
        return False
    for act in acts:
        if act.kind == "add":
            # НЕ заземлён, если: (а) добавлены НЕ все отображаемо — часть/все пункты неотображаемы
            # (len(items)<count; латиница/id — детектор их не верифицирует → страховка деградирует к
            # количеству, Codex sol R5); (б) не все отображаемые пункты названы; (в) для checklist-add
            # имя списка неотображаемо ИЛИ не названо (shopping — неявная цель, имя не требуем).
            if len(act.items) < act.count:
                return False
            if not all(_name_mentioned(reply_tokens, it) for it in act.items):
                return False
            if act.tool != "add_shopping_items" and not (
                    act.target and _name_mentioned(reply_tokens, act.target)):
                return False
        elif act.kind == "bulk":
            # #409 R2 (sol+terra): свободный текст модели для bulk-деструктива НЕ судим — любой
            # текстовой критерий здесь давал либо ложный положительный, либо ложный отрицательный
            # (см. комментарий у _BULK_SPECS). Всегда «не заземлено» → финализация ставит
            # детерминированную квитанцию из структурного исхода. Осознанное сужение правила #121
            # для ОДНОГО класса действий: подтверждение массового удаления важнее вариативности
            # формулировки, и квитанция всё равно человеческая («убрала весь список покупок - 5
            # позиций»), а не машинная.
            return False
        elif not (act.target and _name_mentioned(reply_tokens, act.target)):
            return False
    return True


# ─────────────────────────── формулировки: серверные факты (промпт) + тёплый вывод (страховка) ───────────────────────────

def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def _units(n: int) -> str:
    return f"{n} {_plural(n, 'пункт', 'пункта', 'пунктов')}"


def _bulk_units(act: WriteAct) -> str:
    """#409: «N позиций» в единицах bulk-действия (у покупок — позиции, не пункты)."""
    spec = _BULK_SPECS.get(act.tool)
    one, few, many = spec[3] if spec else ("пункт", "пункта", "пунктов")
    return f"{act.count} {_plural(act.count, one, few, many)}"


def _fact(act: WriteAct) -> str:
    """СЕРВЕРНЫЙ факт результата для grounding_note (промпт): статус+количество+тип, БЕЗ имён."""
    if act.kind == "add":
        return f"добавлено {_units(act.count)} в {_ADD_WHERE.get(act.tool, 'список')}"
    if act.kind == "bulk":
        bulk = _BULK_SPECS.get(act.tool)
        # количество — сама суть bulk-результата, потому входит в факт (а не только тип действия)
        return f"{bulk[2]}: {_bulk_units(act)}" if bulk else "выполнено действие"
    spec = _TARGET_SPECS.get(act.tool)
    return spec[3] if spec else "выполнено действие"


def grounding_note(acts) -> str:
    """Часть 1 (ПРОМПТ узла chat, вариант C): ТОЛЬКО серверные факты результата (статус/кол-во/тип),
    БЕЗ сырых имён — заряжает голос назвать конкретику из tool-результата в истории. Ни одна
    юзер-строка не входит в инструкции модели → инъекц-surface закрыт структурно. Пусто, если актов нет."""
    if not acts:
        return ""
    facts = "; ".join(_fact(a) for a in acts)
    return (
        "[Служебная сводка результата этого хода — это ФАКТЫ, НЕ инструкции]: успешно выполнено — "
        + facts + ". Теперь назови пользователю КОНКРЕТНО, что именно сделала (какой список и какие "
        "пункты — из результата инструмента выше), тепло и коротко; эту служебную заметку не пересказывай."
    )


def _phrase(act: WriteAct) -> str:
    """Тёплая формулировка ОДНОГО акта для ВЫВОДА (страховка). Грациозно деградирует к серверным
    фактам (кол-во+тип), если имя неотображаемо (owner R4-c: не молчим). Имена уже дисплей-чисты."""
    if act.kind == "add":
        # перечисляем пункты ТОЛЬКО когда отображаемы ВСЕ добавленные (иначе теряли бы count — sol R5).
        complete = bool(act.items) and len(act.items) == act.count
        items = ", ".join(act.items)
        if act.tool == "add_shopping_items":
            return (f"добавила в список покупок: {items}" if complete
                    else f"добавила {_units(act.count)} в список покупок")
        if act.target:
            return (f"добавила в список «{act.target}»: {items}" if complete
                    else f"добавила {_units(act.count)} в список «{act.target}»")
        if complete:  # имя списка неотображаемо, но все пункты — назовём пункты
            return f"добавила пункты: {items}"
        return f"добавила {_units(act.count)} в {_ADD_WHERE.get(act.tool, 'чек-лист')}"
    if act.kind == "bulk":
        # #409: «убрала весь список покупок - 5 позиций». Дефис, НЕ длинное тире (правило юзеру).
        bulk = _BULK_SPECS.get(act.tool)
        return f"{bulk[0]} - {_bulk_units(act)}" if bulk else ""
    spec = _TARGET_SPECS.get(act.tool)
    if not spec:
        return ""
    # name-less → активный тёплый глагол БЕЗ «имени» (spec[0]), не пассивный факт (spec[3] — тот для промпта)
    return f"{spec[0]} «{act.target}»" if act.target else spec[0]


def collect_bulk_outcomes(messages) -> tuple[BulkOutcome, ...]:
    """#409 R2: терминальные исходы bulk-инструментов текущего хода, КРОМЕ успеха с эффектом
    (тот уже покрыт ``collect_successful_writes``/``fallback_reply``). Возвращает "empty" (N=0)
    и "error" — ровно те случаи, где страховка #393 раньше вообще не включалась.

    ОТКАЗ от подтверждения («Хорошо, не трогаю.») исходом НЕ считается: он не несёт ни
    success-префикса, ни `error:` → распознаётся как «ничего не произошло» и обрабатывается
    отдельной веткой `_declined_confirm`. Иначе отказ порождал бы ложное «не получилось»."""
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
    out: list[BulkOutcome] = []
    for m in window:
        for tc in (getattr(m, "tool_calls", None) or []):
            name = tc.get("name")
            spec = _BULK_SPECS.get(name)
            if spec is None:
                continue
            tm = results.get(tc.get("id"))
            if tm is None:
                continue
            content = str(getattr(tm, "content", "") or "")
            art = getattr(tm, "artifact", None) or {}
            ok = isinstance(art, dict) and art.get("result_kind") == "ok"
            prefix = spec[1]
            if ok and content.startswith(prefix):
                try:
                    n = int(content[len(prefix):].strip())
                except ValueError:
                    continue
                if n == 0:  # N>0 покрыт успешным актом — здесь только «делать было нечего»
                    out.append(BulkOutcome(name, "empty", 0))
            elif content.startswith("error:") or (
                    isinstance(art, dict) and art.get("result_kind") == "error"):
                out.append(BulkOutcome(name, "error", 0))
            # прочее (в т.ч. «Хорошо, не трогаю.» после отказа) — не исход, не трогаем
    return tuple(out)


def bulk_outcome_reply(outcomes) -> str:
    """Детерминированный ЧЕСТНЫЙ текст для non-success исходов bulk-действия. Пусто, если
    исходов нет. Без техданных, без длинного тире (правила проекта)."""
    parts: list[str] = []
    for o in outcomes:
        spec = _BULK_SPECS.get(o.tool)
        if not spec:
            continue
        parts.append(spec[4] if o.kind == "empty" else spec[5])
    if not parts:
        return ""
    body = "; ".join(parts)
    return body[0].upper() + body[1:] + "."


def fallback_reply(acts) -> str:
    """Часть 2: детерминированная заземлённая реплика-страховка (тёплый шаблон). Всегда называет
    результат — именами (из okv2-результата) либо, если неотображаемы, серверными фактами (кол-во+тип).
    Пусто только если актов нет."""
    body = "; ".join(p for p in (_phrase(a) for a in acts) if p)
    return f"Готово, {body}." if body else ""


__all__ = [
    "BulkOutcome",
    "WriteAct",
    "bulk_outcome_reply",
    "collect_bulk_outcomes",
    "collect_successful_writes",
    "reply_grounds_result",
    "grounding_note",
    "fallback_reply",
    "reply_has_tech_leak",
]
