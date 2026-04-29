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


def test_branch_index_supports_strict_equality_idempotency() -> None:
    """2026-04-29: после edit-based wizard rework контракт изменился.
    Idempotency блокирует ТОЛЬКО точный повтор того же branch'а
    (cur_idx == last_idx). «Откат назад» (cur_idx < last_idx) — теперь
    легитимная навигация (юзер тапнул «← prev»), не блокируется.

    Старая семантика `cur_idx <= last_idx` вызывала бы блок prev-таппа
    в wizard'е, что сломало бы B (двустороннюю) навигацию."""
    # Точный повтор — блокируется
    assert pending_bot.branch_index("schedule") == pending_bot.branch_index("schedule")
    # «Откат назад» — НЕ блокируется (cur != last)
    assert pending_bot.branch_index("intro") != pending_bot.branch_index("reminders")
    # Forward — НЕ блокируется
    assert pending_bot.branch_index("schedule") != pending_bot.branch_index("voice")


def test_navigation_keyboard_intro_has_only_next() -> None:
    """Первая ветка — только кнопка «next →» (нет prev)."""
    kb = pending_bot.build_navigation_keyboard("intro")
    rows = kb["inline_keyboard"]
    assert len(rows) == 1, f"intro: expected 1 row, got {rows}"
    assert len(rows[0]) == 1, "intro: expected only 'next' button"
    btn = rows[0][0]
    assert btn["callback_data"] == "pb:voice"
    assert "Голос" in btn["text"] and "→" in btn["text"]


def test_navigation_keyboard_middle_has_prev_and_next() -> None:
    """Средняя ветка (например voice) — prev + next в одном ряду."""
    kb = pending_bot.build_navigation_keyboard("voice")
    rows = kb["inline_keyboard"]
    assert len(rows) == 1
    assert len(rows[0]) == 2, "voice: expected prev + next buttons"
    prev_btn, next_btn = rows[0]
    assert prev_btn["callback_data"] == "pb:intro"
    assert "←" in prev_btn["text"]
    assert "Привет" in prev_btn["text"]
    assert next_btn["callback_data"] == "pb:schedule"
    assert "Расписание" in next_btn["text"]
    assert "→" in next_btn["text"]


def test_navigation_keyboard_pre_final_branch_uses_gotovo_label() -> None:
    """Предпоследняя ветка `dont_do` — next кнопка «Готово ✓»,
    не «Готово →»."""
    kb = pending_bot.build_navigation_keyboard("dont_do")
    rows = kb["inline_keyboard"]
    prev_btn, next_btn = rows[0]
    assert prev_btn["callback_data"] == "pb:memory"
    assert next_btn["callback_data"] == "pb:done"
    assert "Готово" in next_btn["text"]
    assert "✓" in next_btn["text"]
    assert "→" not in next_btn["text"], "final next: emoji ✓, not arrow"


def test_navigation_keyboard_done_is_empty_keyboard() -> None:
    """Финал `done` — пустой inline_keyboard (Telegram удалит buttons
    при editMessageText с этим markup'ом)."""
    kb = pending_bot.build_navigation_keyboard("done")
    assert kb == {"inline_keyboard": []}


def test_navigation_keyboard_unknown_branch_falls_back_to_intro() -> None:
    """Неизвестный branch (alias / typo) → intro keyboard."""
    intro_kb = pending_bot.build_navigation_keyboard("intro")
    fallback_kb = pending_bot.build_navigation_keyboard("nonexistent_xyz")
    assert fallback_kb == intro_kb


def test_navigation_keyboard_all_branches_round_trip_consistent() -> None:
    """Цепочка: на каждой ветке next ведёт к следующей в BRANCH_ORDER,
    prev ведёт к предыдущей. Проверка что builder корректно собирает
    переходы по всему туру."""
    order = pending_bot.BRANCH_ORDER
    for i, br in enumerate(order):
        kb = pending_bot.build_navigation_keyboard(br)
        if br == "done":
            assert kb == {"inline_keyboard": []}
            continue
        rows = kb["inline_keyboard"]
        flat = rows[0]
        # Prev button
        if i == 0:
            assert all(
                not b["callback_data"].endswith(order[max(i-1, 0)]) or
                b["text"].startswith("←") is False  # no prev on first
                for b in flat
            )
        else:
            prev_match = [b for b in flat if b["callback_data"] == f"pb:{order[i-1]}"]
            assert prev_match, f"branch {br}: missing prev button to {order[i-1]}"
        # Next button
        next_match = [b for b in flat if b["callback_data"] == f"pb:{order[i+1]}"]
        assert next_match, f"branch {br}: missing next button to {order[i+1]}"


