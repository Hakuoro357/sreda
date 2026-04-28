"""Unit-тесты для pending_bot — BRANCH_ORDER и branch_index helper.

Используется для idempotency-проверки в `telegram_bot._handle_callback`
(2026-04-28 spam-loop fix tg_1089832184).
"""

from __future__ import annotations

from sreda.services import pending_bot


def test_branch_order_starts_with_intro_ends_with_done() -> None:
    """BRANCH_ORDER задаёт линейную последовательность тура."""
    assert pending_bot.BRANCH_ORDER[0] == "intro"
    assert pending_bot.BRANCH_ORDER[-1] == "done"


def test_branch_order_contains_all_known_branches() -> None:
    """Все 11 ветвей из docs/copy/welcome.md есть в BRANCH_ORDER."""
    expected = {
        "intro", "voice", "schedule", "reminders", "checklists",
        "shopping", "recipes", "family", "memory", "dont_do", "done",
    }
    assert set(pending_bot.BRANCH_ORDER) == expected
    # И длина равна — нет дубликатов.
    assert len(pending_bot.BRANCH_ORDER) == 11


def test_branch_index_returns_position() -> None:
    assert pending_bot.branch_index("intro") == 0
    assert pending_bot.branch_index("voice") == 1
    assert pending_bot.branch_index("schedule") == 2
    assert pending_bot.branch_index("done") == 10


def test_branch_index_unknown_returns_minus_one() -> None:
    """Aliases (welcome / what / etc.) и unknown branches → -1.

    `_handle_callback` использует это для skip-условия: если cur_idx == -1,
    idempotency check пропускается и юзер попадает в общий flow."""
    # Aliases маппятся в intro в `_BRANCHES`, но в ORDER их нет.
    assert pending_bot.branch_index("welcome") == -1
    assert pending_bot.branch_index("what") == -1
    assert pending_bot.branch_index("life") == -1
    assert pending_bot.branch_index("nonexistent") == -1
    assert pending_bot.branch_index("") == -1


def test_branch_index_supports_idempotency_compare() -> None:
    """Контракт: cur_idx <= last_idx означает повтор/откат → no-op.

    Сценарий tg_1089832184 19:25: юзер тапнул `pb:schedule` (idx=2),
    welcome_v2_progress.last_branch == 'schedule'. Повторный tap должен
    дать cur_idx <= last_idx, т.е. skip."""
    cur = pending_bot.branch_index("schedule")
    last = pending_bot.branch_index("schedule")
    assert cur >= 0 and last >= 0
    assert cur <= last  # повтор → drop

    # Откат назад: юзер на reminders (3), тапнул intro (0)
    cur = pending_bot.branch_index("intro")
    last = pending_bot.branch_index("reminders")
    assert cur < last  # cur ≤ last → drop

    # Forward progress: юзер на voice (1), тапнул schedule (2)
    cur = pending_bot.branch_index("schedule")
    last = pending_bot.branch_index("voice")
    assert cur > last  # cur > last → proceed
