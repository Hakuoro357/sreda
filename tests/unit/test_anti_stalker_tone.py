"""Anti-stalker tone guard (Часть B/C плана v2).

Гарантирует что в HARDCODED bot-текстах (которые бот отправляет юзеру
без LLM) нет фраз-маркеров «слежки за пользователем». План v2 — Фильтр 5
(«нет спонтанных чек-инов») + Фильтр 4 («read-back без счёта»).

Сканируемые файлы — только те, где живут тексты, идущие ПРЯМО юзеру:
- pending_bot.py (scripted welcome + 6 веток)
- onboarding_aha_worker.py (Aha-2 текст)
- housewife_reminder_worker.py (reminder template)
- housewife_onboarding_worker.py (kickoff intro)
- onboarding.py (build_welcome_message после approval)

НЕ сканируем ``handlers.py`` — там prompt-инструкции, которые СОДЕРЖАТ
negative examples («плохо: "Как прошёл день?"») намеренно, чтобы LLM
знала что избегать.

Blacklist — конкретные фразы в любом регистре:
- "Как прошёл день" — классический creepy check-in
- "Давно тебя не было" — проверка «пропал?»
- "Проверяю, ты занята" — «мониторю активность»
- "Я заметила что ты" / "Вижу что ты" — сбор наблюдений
- "ты N раз ..." / "N раз упомянула" — подсчёт вслух
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Файлы, текст которых идёт юзеру напрямую (без LLM).
SCANNED_FILES: tuple[Path, ...] = (
    REPO_ROOT / "src" / "sreda" / "services" / "pending_bot.py",
    REPO_ROOT / "src" / "sreda" / "services" / "onboarding.py",
    REPO_ROOT / "src" / "sreda" / "workers" / "onboarding_aha_worker.py",
    REPO_ROOT / "src" / "sreda" / "workers" / "housewife_reminder_worker.py",
    REPO_ROOT / "src" / "sreda" / "workers" / "housewife_onboarding_worker.py",
)

# Явные creepy-фразы. Case-insensitive.
_BANNED_LITERALS: tuple[str, ...] = (
    "как прошёл день",
    "как прошел день",
    "давно тебя не было",
    "давно не писала",
    "проверяю, ты занята",
    "я заметила что ты",
    "я заметила, что ты",
    "вижу что ты",
    "вижу, что ты",
)

# Регексные шаблоны счёта упоминаний.
_BANNED_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "ты N раз упомянула" / "N раз упоминала"
    re.compile(r"\bты\s+\d+\s+раз", re.IGNORECASE),
    re.compile(r"\bдваж?ды\s+упоминала\b", re.IGNORECASE),
    re.compile(r"\bтрижды\s+упоминала\b", re.IGNORECASE),
    # "за последние N дней ты …" — cadence-tracking
    re.compile(
        r"за\s+последние\s+\d+\s+дн(ей|я)\s+ты\b", re.IGNORECASE,
    ),
    # Gender assumption about the USER (prod rule 2026-04-25): пока
    # пол юзера не известен — прошедшее время ж.р. про него запрещено.
    # Про бота («Среда») ж.р. допустимо, но «ты сказала/попросила/сама»
    # — нет. Ловим второе лицо + глагол в ж.р. past tense.
    re.compile(
        r"\bты\s+(сама|сказала|говорила|попросила|зафиксировала|"
        r"упомянула|добавила|создала|хотела|успела|написала|"
        r"отметила|сохранила|подключила|нажала)\b",
        re.IGNORECASE,
    ),
)


def _strip_comments_and_docstrings(source: str) -> str:
    """Убираем комментарии и docstring, чтобы negative examples в
    пояснительных текстах модуля не валили тест. Достаточно примитивный
    strip: line-comments + triple-quoted blocks."""
    # Triple-quoted strings: нематчим через компактный regex, удаляем.
    stripped = re.sub(r'"""[\s\S]*?"""', "", source)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    # Single-line comments.
    stripped = re.sub(r"(^|\s)#[^\n]*", lambda m: m.group(1), stripped)
    return stripped


@pytest.mark.parametrize("path", SCANNED_FILES, ids=lambda p: p.name)
def test_no_stalker_literals(path: Path) -> None:
    assert path.exists(), f"scanned file missing: {path}"
    src = path.read_text(encoding="utf-8")
    code = _strip_comments_and_docstrings(src)
    lower = code.lower()
    for bad in _BANNED_LITERALS:
        assert bad not in lower, (
            f"{path.name}: creepy phrase detected: «{bad}». "
            "Перефразируй или убери (см. Фильтр 5 анти-сталкер)."
        )


@pytest.mark.parametrize("path", SCANNED_FILES, ids=lambda p: p.name)
def test_no_stalker_counting_patterns(path: Path) -> None:
    assert path.exists(), f"scanned file missing: {path}"
    src = path.read_text(encoding="utf-8")
    code = _strip_comments_and_docstrings(src)
    for pat in _BANNED_PATTERNS:
        match = pat.search(code)
        assert match is None, (
            f"{path.name}: read-back-with-count detected: {match.group(0)!r}. "
            "Используй мягкий read-back, например «похоже, часто …» "
            "(см. Фильтр 4 анти-сталкер)."
        )


def test_scanner_self_check() -> None:
    """Мини-тест самого сканера — что он реально ловит запрещённые
    фразы, если они вдруг появятся. Защищает от случайного no-op."""
    bad_sample = 'text = "Как прошёл день? Ты дважды упоминала молоко."'
    code = _strip_comments_and_docstrings(bad_sample)
    assert "как прошёл день" in code.lower()
    matched = any(p.search(code) for p in _BANNED_PATTERNS)
    assert matched, "sanity check broken: pattern matcher doesn't catch 'дважды упоминала'"


def test_gender_assumption_blocked() -> None:
    """Self-check: ж.р. про ЮЗЕРА в past tense должно триггерить."""
    gendered = 'text = "Ты сама попросила поставить напоминание."'
    code = _strip_comments_and_docstrings(gendered)
    matched = any(p.search(code) for p in _BANNED_PATTERNS)
    assert matched, "gender-assumption pattern must catch 'ты сама'"

    gendered2 = 'text = "Помнишь, ты говорила про стоматолога."'
    code2 = _strip_comments_and_docstrings(gendered2)
    matched2 = any(p.search(code2) for p in _BANNED_PATTERNS)
    assert matched2, "gender-assumption pattern must catch 'ты говорила'"


# ---------------------------------------------------------------------------
# Brand voice + Yandex Maps prompt rules (2026-04-28, п.7.1 + 7.2)
# ---------------------------------------------------------------------------
#
# Эти тесты проверяют что в _HOUSEWIFE_FOOD_PROMPT прописаны правила:
# - Среда всегда отвечает в ж.р. (бренд)
# - Адреса сопровождаются ссылкой на yandex.ru/maps
# Не сканируем динамический output LLM (это может сделать только
# integration-тест с реальным LLM-вызовом), но проверяем что инструкции
# в prompt'е есть.


def test_housewife_prompt_has_sreda_feminine_rule() -> None:
    from sreda.runtime.handlers import _HOUSEWIFE_FOOD_PROMPT

    text = _HOUSEWIFE_FOOD_PROMPT.lower()
    assert "род среды" in text or "среда — она" in text, (
        "В _HOUSEWIFE_FOOD_PROMPT должен быть раздел про обязательный "
        "женский род самонарратива Среды (бренд)."
    )
    # Явные примеры правильного и запрещённого
    assert "запомнила" in text and "помог" in text, (
        "Раздел про ж.р. должен содержать примеры правильного "
        "(«запомнила») и запрещённого («помог»)."
    )


def test_housewife_prompt_has_yandex_maps_rule() -> None:
    from sreda.runtime.handlers import _HOUSEWIFE_FOOD_PROMPT

    text = _HOUSEWIFE_FOOD_PROMPT.lower()
    assert "yandex.ru/maps" in text, (
        "В _HOUSEWIFE_FOOD_PROMPT должна быть инструкция про "
        "сопровождение адресов ссылкой на yandex.ru/maps."
    )
    assert "адрес" in text and "ссылк" in text, (
        "Раздел про адреса должен явно упоминать про ссылки."
    )
