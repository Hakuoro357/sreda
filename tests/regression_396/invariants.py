"""Переиспользуемые ИНВАРИАНТЫ состояния (#396) — красный при нарушении.

Судим НЕ «ответ похож», а по объективному состоянию (БД + текст реплики +
квитанции + потолки). Пять инвариантов из issue #396:

  1. ответ.успех ⇒ эффект.status ∈ {applied, already_applied}   (no_false_success)
  2. эффект.failed ⇒ ответ НЕ говорит «готово»                   (failed_not_done)
  3. тот же idempotency_key ⇒ ≤1 мутация                         (idempotency, контекстный)
  4. неоднозначная отмена ⇒ 0 мутаций                            (ambiguous_cancel, контекстный)
     4a. мутация состоялась ⇒ ответ НЕ говорит «отменила»        (mutated_not_cancelled, #362)
  5. публичный ответ ∩ технические поля = ∅                      (public_no_tech, #390/g-075)

УНИВЕРСАЛЬНЫЕ (1, 2, 4a, 5) гоняются на КАЖДОЙ фикстуре без конфигурации.
КОНТЕКСТНЫЕ (3, 4) требуют декларации фикстуры (какие ходы — «тот же ключ» /
«неоднозначная отмена»).

Функции ЧИСТЫЕ: принимают ``DialogOutcome`` (или его ходы), возвращают
``list[Violation]``. Тестируются на синтетике, ``handle_turn`` не поднимая.
"""
from __future__ import annotations

import re

from sreda.runtime.react_result_report import reply_has_tech_leak
from sreda.services.tool_schemas.families import TOOL_OP_CLASS

from .model import DialogOutcome, TurnOutcome, Violation

# ─────────────────────────── детекторы текста реплики ───────────────────────────

# Утверждение об УСПЕШНОЙ мутации — прошедшее время «помощницы» (жен. род, 1 л.).
# Инфинитив («добавить?») сюда НЕ попадает (нет -л-) → уточняющий вопрос не считается
# ложным успехом. Список замкнут на мутирующие глаголы семей Среды.
_MUTATION_CLAIM_RE = re.compile(
    r"\b(?:добави|записа|сохрани|постави|созда|состави|отмети|убра|удали|очисти|"
    r"внес|внёс|заплани|перенес|перенёс|обнови|отправи|запо?мни|занес)л[аио]?\b",
    re.IGNORECASE,
)
# Голое «готово/сделано» — утверждение об успехе ТОЛЬКО если ход пытался писать
# (был write-инструмент): иначе «Готово, вот список» на чтении ложно бы срабатывал.
_DONE_RE = re.compile(r"\b(готов[оа]|сделано)\b", re.IGNORECASE)

# Утверждение об ОТМЕНЕ / бездействии («Отменила, ничего не делаю» — репро #362).
_CANCEL_CLAIM_RE = re.compile(
    r"(отмени(?:ла|л)\b|отменено\b|ничего не делаю|не трогаю|не буду (?:это|ничего)|"
    r"не стала|оставила как есть)",
    re.IGNORECASE,
)

# ─────────────────────────── детектор техвыдачи (инв.5) ───────────────────────────

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?")
# snake_case-идентификатор инструмента/поля: add_task, result_type, list_id_or_title.
_SNAKE_RE = re.compile(r"\b[a-z]{2,}_[a-z0-9_]{2,}\b")
# английские тех-счётчики/паспорт результата — прямой источник #390.
_EN_TECH_RE = re.compile(
    r"\b(pending|done|total|overview|items|status|count|result_type|result_id|"
    r"checklist|reminder|task|shopping)\b",
    re.IGNORECASE,
)
# id-подобный токен в квадратных скобках: [checklist_ab12…], [rem_…].
_BRACKET_ID_RE = re.compile(r"\[[a-z]+_[0-9a-z]{4,}", re.IGNORECASE)


def tech_tokens(text: str) -> list[str]:
    """Все техвыдача-токены в тексте (для читаемого сообщения о нарушении).

    Латиница как таковая НЕ флагается (может быть контентом юзера — «iPhone»,
    «milk»): бьём только по тех-СИГНАТУРАМ (okv2/id=/hex, ISO-дата, snake_case-
    идентификатор, англ. тех-счётчик, bracket-id). Дедуп с сохранением порядка."""
    hits: list[str] = []
    t = text or ""
    if reply_has_tech_leak(t):
        hits.append("okv2/id=/hex")
    hits += _ISO_RE.findall(t)
    hits += [m.group(0) for m in _SNAKE_RE.finditer(t)]
    hits += [m.group(0) for m in _EN_TECH_RE.finditer(t)]
    hits += [m.group(0) for m in _BRACKET_ID_RE.finditer(t)]
    seen: dict[str, None] = {}
    for h in hits:
        seen.setdefault(h, None)
    return list(seen)


def _turn_attempted_write(turn: TurnOutcome) -> bool:
    return any(TOOL_OP_CLASS.get(n) == "write" for n in turn.tool_calls)


def _claims_success(turn: TurnOutcome) -> bool:
    """Реплика УТВЕРЖДАЕТ успешную мутацию? Мутирующий глагол в прош. времени, ИЛИ
    голое «готово/сделано» при попытке записи (write-инструмент на ходу)."""
    r = turn.reply or ""
    if _MUTATION_CLAIM_RE.search(r):
        return True
    return bool(_DONE_RE.search(r) and _turn_attempted_write(turn))


def _claims_cancel(turn: TurnOutcome) -> bool:
    return bool(_CANCEL_CLAIM_RE.search(turn.reply or ""))


# ─────────────────────────── универсальные инварианты ───────────────────────────


def inv_no_false_success(dialog: DialogOutcome) -> list[Violation]:
    """Инв.1: реплика утверждает успех ⇒ эффект РЕАЛЬНО состоялся (мутация БД ИЛИ
    доказанная applied-квитанция write). Иначе — ложный успех (класс #376)."""
    out: list[Violation] = []
    for i, t in enumerate(dialog.turns):
        if t.awaiting_confirm:
            continue  # пауза-подтверждение: мутация отложена до resume, реплика — вопрос
        if _claims_success(t) and not (t.mutated or t.has_applied_write):
            out.append(Violation(
                "no_false_success", "CRITICAL", i,
                f"реплика утверждает успех, но эффекта нет "
                f"(db_diff={t.db_diff}, applied_write={t.has_applied_write}): {t.reply!r}"))
    return out


def inv_failed_not_done(dialog: DialogOutcome) -> list[Violation]:
    """Инв.2: инструмент вернул ошибку И нет компенсирующего успеха ⇒ реплика НЕ
    говорит «готово». Модель не должна рапортовать успех поверх упавшего инструмента."""
    out: list[Violation] = []
    for i, t in enumerate(dialog.turns):
        if t.has_failed_tool and not t.has_applied_write and _claims_success(t):
            out.append(Violation(
                "failed_not_done", "CRITICAL", i,
                f"инструмент упал (receipts={[(r.name, r.result_kind) for r in t.receipts]}), "
                f"а реплика рапортует успех: {t.reply!r}"))
    return out


def inv_mutated_not_cancelled(dialog: DialogOutcome) -> list[Violation]:
    """Инв.4a (репро #362): мутация состоялась (или applied-квитанция) ⇒ реплика НЕ
    говорит «отменила / ничего не делаю». Иначе — ложная отмена (потеря данных)."""
    out: list[Violation] = []
    for i, t in enumerate(dialog.turns):
        if (t.mutated or t.has_applied_write) and _claims_cancel(t):
            out.append(Violation(
                "mutated_not_cancelled", "CRITICAL", i,
                f"эффект состоялся (db_diff={t.db_diff}, applied={t.has_applied_write}), "
                f"а реплика говорит об отмене: {t.reply!r}"))
    return out


def inv_public_no_tech(dialog: DialogOutcome) -> list[Violation]:
    """Инв.5 (#390/g-075): публичная реплика ∩ технические поля = ∅. Никаких
    англ. тех-счётчиков (pending/done/total), snake_case-идентификаторов, ISO-дат,
    okv2/id=/hex, bracket-id в тексте юзеру."""
    out: list[Violation] = []
    for i, t in enumerate(dialog.turns):
        toks = tech_tokens(t.reply)
        if toks:
            out.append(Violation(
                "public_no_tech", "CRITICAL", i,
                f"техвыдача в реплике: {toks} — {t.reply!r}"))
    return out


UNIVERSAL_INVARIANTS = (
    inv_no_false_success,
    inv_failed_not_done,
    inv_mutated_not_cancelled,
    inv_public_no_tech,
)


def run_universal(dialog: DialogOutcome) -> list[Violation]:
    """Все универсальные инварианты по диалогу. Пусто = чисто."""
    out: list[Violation] = []
    for inv in UNIVERSAL_INVARIANTS:
        out.extend(inv(dialog))
    return out


# ─────────────────────────── контекстные инварианты ───────────────────────────


def inv_idempotency(dialog: DialogOutcome, *, turns: list[int], table: str) -> list[Violation]:
    """Инв.3: набор ходов с ОДНИМ idempotency_key (повтор той же мутации) ⇒ суммарно
    ≤1 положительная мутация в целевой таблице. Второй идентичный add обязан
    задедупиться (0 новых строк), не создать дубль."""
    added = 0
    for i in turns:
        d = dialog.turns[i].db_diff.get(table, 0)
        if d > 0:
            added += d
    if added > 1:
        return [Violation(
            "idempotency", "MAJOR", -1,
            f"ходы {turns}: суммарно +{added} строк в «{table}» (ждали ≤1) — дедуп не сработал")]
    return []


def inv_ambiguous_cancel_zero_mutations(
        dialog: DialogOutcome, *, turns: list[int]) -> list[Violation]:
    """Инв.4: неоднозначная отмена ⇒ 0 мутаций. Помеченные ходы не должны менять БД
    (при неясной цели отмены безопаснее ничего не трогать)."""
    out: list[Violation] = []
    for i in turns:
        t = dialog.turns[i]
        if t.mutated:
            out.append(Violation(
                "ambiguous_cancel", "CRITICAL", i,
                f"неоднозначная отмена изменила БД (db_diff={t.db_diff}): {t.user_text!r}"))
    return out
