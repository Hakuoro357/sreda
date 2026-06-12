"""#124 срез 1 — блок ПОСЛЕДНИЕ_РЕПЛИКИ_ЮЗЕРА в суффиксе промпта.

Корень (Codex R1 CRITICAL): закрытые ходы рендерятся только как summary,
поэтому надиктованные пункты («Обувь», «Носки»…) физически не доходят до
планировщика. Срез 1 даёт детерминированный источник — отдельный блок
последних user-реплик закрытых ходов.
"""
from __future__ import annotations

from sreda.runtime.planner.prompt_builder import (
    NowMoment,
    ProfileSnapshot,
    TurnMessage,
    TurnSnapshot,
    build_recent_utterances_block,
    build_variable_suffix,
)


def _closed(turn_id: str, user_text: str) -> TurnSnapshot:
    return TurnSnapshot(
        turn_id=turn_id, started_at="2026-06-12T10:00:00", summary=None,
        messages=[
            TurnMessage(role="юзер", text=user_text, ts="2026-06-12T10:00:00"),
            TurnMessage(role="среда", text="ок", ts="2026-06-12T10:00:01"),
        ],
    )


def test_block_holds_ten_dictated_items() -> None:
    """Чеклист п.3: 10 надиктованных пунктов ПРИСУТСТВУЮТ в блоке."""
    items = ["Обувь", "Носки", "Шапка", "Рукавицы", "Шарф",
             "Куртка", "Штаны", "Сапоги", "Перчатки", "Свитер"]
    closed = [_closed(f"t{i}", it) for i, it in enumerate(items)]
    block = build_recent_utterances_block(closed_turns=closed)
    for it in items:
        assert it in block, f"пункт «{it}» потерян из блока"


def test_block_excludes_current_turn() -> None:
    """Чеклист: текущий ход (последний user) НЕ попадает в блок."""
    closed = [_closed("t1", "Молоко"), _closed("t2", "Хлеб")]
    block = build_recent_utterances_block(
        closed_turns=closed, current_user_message="собери в список")
    assert "собери в список" not in block
    assert "Молоко" in block and "Хлеб" in block


def test_block_caps_to_last_ten() -> None:
    """Берём последние ≤10 реплик; более старые опускаются."""
    closed = [_closed(f"t{i}", f"пункт{i}") for i in range(15)]
    block = build_recent_utterances_block(closed_turns=closed)
    assert "пункт14" in block and "пункт5" in block   # последние 10: 5..14
    assert "пункт0" not in block                       # старое опущено


def test_suffix_contains_recent_utterances_block() -> None:
    """Сквозь build_variable_suffix: блок реально попадает в суффикс."""
    closed = [_closed("t1", "Обувь"), _closed("t2", "Носки")]
    suffix = build_variable_suffix(
        profile=ProfileSnapshot(address="ты"),
        memories=[], active_turn=None, closed_turns=closed,
        now=NowMoment(__import__("datetime").datetime(2026, 6, 12, 10, 0)),
        user_message="собери в список",
    )
    assert "ПОСЛЕДНИЕ_РЕПЛИКИ_ЮЗЕРА" in suffix
    assert "Обувь" in suffix and "Носки" in suffix
