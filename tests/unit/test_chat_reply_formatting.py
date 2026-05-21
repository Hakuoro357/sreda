from sreda.runtime.handlers import _format_week_menu_reply


def test_format_week_menu_reply_separates_weekday_blocks():
    raw = (
        "Меню на неделю с 25 мая: Понедельник\n"
        "— Завтрак: Овсянка\n"
        "— Обед: Суп\n"
        "— Ужин: Рис с овощами Вторник\n"
        "— Завтрак: Яичница\n"
        "— Обед: Щи\n"
        "— Ужин: Паста Среда\n"
        "— Завтрак: Творог\n"
        "— Обед: Гороховый суп\n"
        "— Ужин: Рыба Хочешь собрать список покупок?"
    )

    formatted = _format_week_menu_reply(raw)

    assert "25 мая:\n\nПонедельник:" in formatted
    assert "Рис с овощами\n\nВторник:" in formatted
    assert "Паста\n\nСреда:" in formatted
    assert "Рыба\n\nХочешь собрать список покупок?" in formatted


def test_format_week_menu_reply_handles_colon_weekday_headings():
    raw = (
        "Меню на неделю с 25 мая: Понедельник:\n"
        "— Завтрак: Овсянка\n"
        "— Обед: Суп\n"
        "— Ужин: Рис с овощами Вторник:\n"
        "— Завтрак: Яичница\n"
        "— Обед: Щи\n"
        "— Ужин: Паста Собрать ингредиенты в список покупок?"
    )

    formatted = _format_week_menu_reply(raw)

    assert "25 мая:\n\nПонедельник:" in formatted
    assert "Рис с овощами\n\nВторник:" in formatted
    assert "Паста\n\nСобрать ингредиенты в список покупок?" in formatted


def test_format_week_menu_reply_leaves_non_menu_text_unchanged():
    text = "Во вторник можно приготовить пасту, если захочешь."

    assert _format_week_menu_reply(text) == text
