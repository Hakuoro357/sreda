"""#394: копия удаления списка — на языке пользователя, без внутреннего «архив*».

Живой прогон 18/20.07 (tenant_max_40921122): на «Удали список Дача» система отвечала
внутренним «архивирую» в ДВУХ местах — (1) confirm-пауза «Я сейчас архивирую чек-лист
«Дача». Нужно твоё подтверждение.» и (2) финальный ответ (#393-заземление) «заархивировала
список Дача». Юзер сказал «удали» — ответ должен быть на его языке. Действие остаётся
soft-delete (archive/status=archived), обратимость НЕ трогаем — правка только КОПИИ.

Место 1 — react_loop: `_CONFIRM_PHRASE["archive_checklist"]` + динамик `_ph_arch`.
Место 2 — react_result_report: `_TARGET_SPECS["archive_checklist"]` — spec[0] (тёплый
глагол страховки) и spec[3] (серверный факт, заряжает промпт узла chat).

Реверсивность (механика archive не тронута): `tests/unit/test_miniapp_checklist_archive.py`
(archive_list → status='archived', не hard-delete; остаётся зелёным).
"""

from __future__ import annotations

import re

from sreda.runtime.react_loop import _CONFIRM_PHRASE
from sreda.runtime.react_result_report import (
    WriteAct,
    _fact,
    _phrase,
    fallback_reply,
    grounding_note,
    reply_has_archive_leak,
)

_ARCHIVE_RE = re.compile(r"архив", re.IGNORECASE)
_DELETE_RE = re.compile(r"удал|убр", re.IGNORECASE)


def _archive_act(target: str = "Дача") -> WriteAct:
    return WriteAct("archive_checklist", "target", target, (), 1)


# ─────────────────────────── место 1: confirm-копия ───────────────────────────

def test_confirm_phrase_archive_no_archive_root_394():
    """Статичная confirm-фраза удаления списка НЕ содержит корня «архив» и говорит «удал»."""
    phrase = _CONFIRM_PHRASE["archive_checklist"]
    assert not _ARCHIVE_RE.search(phrase), phrase
    assert _DELETE_RE.search(phrase), phrase


def test_confirm_full_sentence_archive_394():
    """Полная реплика паузы («Я сейчас {phrase}. Нужно твоё подтверждение.») — на языке
    юзера, без «архив*» (обёртка _confirm_wrap)."""
    sentence = f"Я сейчас {_CONFIRM_PHRASE['archive_checklist']}. Нужно твоё подтверждение."
    assert not _ARCHIVE_RE.search(sentence), sentence
    assert _DELETE_RE.search(sentence)


# ─────────────────────────── место 2: финальный ответ (#393-заземление) ───────────────────────────

def test_grounding_fact_archive_no_archive_root_394():
    """Серверный факт (spec[3]) для grounding_note заряжает голос на языке юзера, без «архив*».
    Именно он раньше подсказывал модели сказать «заархивировала»."""
    fact = _fact(_archive_act())
    assert not _ARCHIVE_RE.search(fact), fact
    assert _DELETE_RE.search(fact), fact


def test_grounding_note_archive_no_archive_root_394():
    note = grounding_note([_archive_act()])
    assert note and not _ARCHIVE_RE.search(note), note
    assert _DELETE_RE.search(note)


def test_fallback_reply_archive_no_archive_root_394():
    """Детерминированная страховка (spec[0]) называет результат на языке юзера, с именем списка."""
    fb = fallback_reply([_archive_act("Дача")])
    assert fb and "Дача" in fb
    assert not _ARCHIVE_RE.search(fb), fb
    assert _DELETE_RE.search(fb)


def test_phrase_archive_named_and_nameless_394():
    """Тёплая формулировка одного акта: с именем и без (граздеградация) — обе без «архив*»."""
    named = _phrase(_archive_act("Дача"))
    assert "Дача" in named and not _ARCHIVE_RE.search(named) and _DELETE_RE.search(named)
    nameless = _phrase(_archive_act(""))
    assert not _ARCHIVE_RE.search(nameless) and _DELETE_RE.search(nameless)


# ─────────────────────────── S-4 (R2 sol): архив-бэкстоп живого ответа ───────────────────────────

def test_archive_leak_backstop_394():
    """R2 sol MINOR: живой ответ модели «Заархивировала список Дача» ЗАЗЕМЛЁН (называет «Дача»), но
    несёт корень «архив» → детектор заземления это не ловит; `reply_has_archive_leak` ловит →
    финализация подменит детерминированной страховкой («удалила список»). Механизм-гейт, не промпт."""
    acts = [_archive_act("Дача")]
    assert reply_has_archive_leak("Заархивировала список «Дача».", acts) is True
    assert reply_has_archive_leak("Удалила список «Дача».", acts) is False
    # без archive-акта — не наше дело (add-акт с «архивным» ИМЕНЕМ списка не триггерит)
    add = [WriteAct("add_checklist_items", "add", "Архив", ("молоко",), 1)]
    assert reply_has_archive_leak("Добавила молоко в список «Архив».", add) is False
