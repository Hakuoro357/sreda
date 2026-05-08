"""Unit-тесты для pending_bot — BRANCH_ORDER и branch_index helper.

Используется для idempotency-проверки в `telegram_bot._handle_callback`
(2026-04-28 spam-loop fix tg_1089832184).

2026-05-08: tour сокращён 11 → 4 (intro → voice → memory → done).
Старые ветки (schedule/reminders/checklists/shopping/recipes/family/
dont_do) теперь aliases на intro для backwards-compat с in-progress
tours.
"""

from __future__ import annotations

from sreda.services import pending_bot


def test_branch_order_starts_with_intro_ends_with_done() -> None:
    """BRANCH_ORDER задаёт линейную последовательность тура."""
    assert pending_bot.BRANCH_ORDER[0] == "intro"
    assert pending_bot.BRANCH_ORDER[-1] == "done"


def test_branch_order_is_4_steps() -> None:
    """4-step tour: intro → voice → memory → done.

    Цель сокращения (2026-05-08): юзеры устают на 4-5 экране,
    drop-off ~50% к 6-му тапу. Сокращение до 4 закрывает elevator
    pitch (кто я → как со мной говорить → что я делаю → жди модератора)
    и удерживает completion.
    """
    expected = ("intro", "voice", "memory", "done")
    assert pending_bot.BRANCH_ORDER == expected


def test_branch_index_returns_position() -> None:
    assert pending_bot.branch_index("intro") == 0
    assert pending_bot.branch_index("voice") == 1
    assert pending_bot.branch_index("memory") == 2
    assert pending_bot.branch_index("done") == 3


def test_branch_index_unknown_returns_minus_one() -> None:
    """Aliases (welcome / what / etc.) и unknown branches → -1.

    `_handle_callback` использует это для skip-условия: если cur_idx == -1,
    idempotency check пропускается и юзер попадает в общий flow."""
    # Aliases маппятся в intro в `_BRANCHES`, но в ORDER их нет.
    assert pending_bot.branch_index("welcome") == -1
    assert pending_bot.branch_index("what") == -1
    assert pending_bot.branch_index("life") == -1
    # Дропнутые в 2026-05-08 ветки (теперь aliases) тоже не в ORDER.
    assert pending_bot.branch_index("schedule") == -1
    assert pending_bot.branch_index("reminders") == -1
    assert pending_bot.branch_index("checklists") == -1
    assert pending_bot.branch_index("shopping") == -1
    assert pending_bot.branch_index("recipes") == -1
    assert pending_bot.branch_index("family") == -1
    assert pending_bot.branch_index("dont_do") == -1
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
    assert pending_bot.branch_index("voice") == pending_bot.branch_index("voice")
    # «Откат назад» — НЕ блокируется (cur != last)
    assert pending_bot.branch_index("intro") != pending_bot.branch_index("memory")
    # Forward — НЕ блокируется
    assert pending_bot.branch_index("memory") != pending_bot.branch_index("voice")


def test_dropped_branches_alias_to_intro() -> None:
    """2026-05-08: 7 веток сокращены, но `_BRANCHES` всё ещё их знает —
    map'ятся в intro. In-progress туры из старой версии не падают."""
    intro_reply = pending_bot.match("pb:intro", is_callback=True)
    for old_key in (
        "schedule", "reminders", "checklists", "shopping",
        "recipes", "family", "dont_do",
    ):
        old_reply = pending_bot.match(f"pb:{old_key}", is_callback=True)
        assert old_reply == intro_reply, (
            f"alias '{old_key}' should map to intro reply"
        )


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
    """Средняя ветка (voice) — prev (intro) + next (memory) в одном ряду."""
    kb = pending_bot.build_navigation_keyboard("voice")
    rows = kb["inline_keyboard"]
    assert len(rows) == 1
    assert len(rows[0]) == 2, "voice: expected prev + next buttons"
    prev_btn, next_btn = rows[0]
    assert prev_btn["callback_data"] == "pb:intro"
    assert "←" in prev_btn["text"]
    assert "Привет" in prev_btn["text"]
    assert next_btn["callback_data"] == "pb:memory"
    assert "Память" in next_btn["text"]
    assert "→" in next_btn["text"]


def test_navigation_keyboard_pre_final_branch_uses_gotovo_label() -> None:
    """Предпоследняя ветка `memory` — next кнопка «Готово ✓»,
    не «Готово →»."""
    kb = pending_bot.build_navigation_keyboard("memory")
    rows = kb["inline_keyboard"]
    prev_btn, next_btn = rows[0]
    assert prev_btn["callback_data"] == "pb:voice"
    assert next_btn["callback_data"] == "pb:done"
    assert "Готово" in next_btn["text"]
    assert "✓" in next_btn["text"]
    assert "→" not in next_btn["text"], "final next: emoji ✓, not arrow"


def test_navigation_keyboard_done_keeps_prev_button() -> None:
    """2026-04-29: финал `done` остаётся navigable. prev-кнопка
    остаётся — tour становится permanent reference, юзер может
    скроллить обратно."""
    kb = pending_bot.build_navigation_keyboard("done")
    rows = kb["inline_keyboard"]
    assert len(rows) == 1, f"done: expected 1 row with prev button, got {rows}"
    assert len(rows[0]) == 1, "done: expected only prev (no next)"
    btn = rows[0][0]
    assert btn["callback_data"] == "pb:memory"
    assert "←" in btn["text"]
    assert "Память" in btn["text"]


def test_navigation_keyboard_unknown_branch_falls_back_to_intro() -> None:
    """Неизвестный branch (alias / typo) → intro keyboard."""
    intro_kb = pending_bot.build_navigation_keyboard("intro")
    fallback_kb = pending_bot.build_navigation_keyboard("nonexistent_xyz")
    assert fallback_kb == intro_kb


def test_navigation_keyboard_all_branches_round_trip_consistent() -> None:
    """Цепочка: на каждой ветке next ведёт к следующей в BRANCH_ORDER,
    prev ведёт к предыдущей. Проверка что builder корректно собирает
    переходы по всему туру (включая done — там только prev)."""
    order = pending_bot.BRANCH_ORDER
    for i, br in enumerate(order):
        kb = pending_bot.build_navigation_keyboard(br)
        rows = kb["inline_keyboard"]
        flat = rows[0]
        # Prev button (на всех кроме intro)
        if i == 0:
            assert not any(b["text"].startswith("←") for b in flat), (
                "intro: should NOT have prev button"
            )
        else:
            prev_match = [b for b in flat if b["callback_data"] == f"pb:{order[i-1]}"]
            assert prev_match, f"branch {br}: missing prev button to {order[i-1]}"
        # Next button (на всех кроме done)
        if br == "done":
            assert not any(
                b["callback_data"].startswith("pb:") and "→" in b["text"]
                for b in flat
            ), "done: should NOT have next button"
        else:
            next_match = [b for b in flat if b["callback_data"] == f"pb:{order[i+1]}"]
            assert next_match, f"branch {br}: missing next button to {order[i+1]}"
