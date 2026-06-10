"""#123 — вёрстка ответов: структурное сырьё + правила формата у рта.

Скриншоты владельца 2026-06-10: один и тот же плановый путь дал в MAX кашу
одним абзацем, в Telegram — столбик с прилипшими краями. Корни: сырьё без
категорий + промпт рта без правил вёрстки.
"""

from __future__ import annotations

from sreda.services.composer.registry import REGISTRY


def test_shopping_show_groups_by_category():
    items = [
        {"display_line": "молоко", "category": "молочные"},
        {"display_line": "сливочное масло (200 г)", "category": "молочные"},
        {"display_line": "картофель (1 кг)", "category": "овощи_фрукты"},
        {"display_line": "хлеб", "category": "хлеб"},
    ]
    out = REGISTRY.render("shopping_list_show", {"items": items})
    lines = out.split("\n")
    # заголовки групп — своей строкой, пункты — под ними
    assert "Молочные:" in lines
    assert "Овощи и фрукты:" in lines  # #123-добив: ярлык вместо кода
    i_cat = lines.index("Молочные:")
    assert lines[i_cat + 1] == "• молоко"
    assert lines[i_cat + 2] == "• сливочное масло (200 г)"
    # категория не повторяется на каждом пункте
    assert out.count("Молочные:") == 1


def test_shopping_show_without_category_flat():
    # элементы без категории (или строковые, #118 R3) — плоский столбик
    out = REGISTRY.render("shopping_list_show", {"items": [
        {"display_line": "молоко"}, "хлеб",
    ]})
    assert "• молоко" in out and "• хлеб" in out
    assert ":" in out.splitlines()[0]  # вступительная строка на месте


def test_shopping_show_mixed_categories_and_strings():
    out = REGISTRY.render("shopping_list_show", {"items": [
        {"display_line": "молоко", "category": "молочные"},
        "что-то строкой",
    ]})
    assert "Молочные:" in out and "• что-то строкой" in out


def test_humanize_prompt_has_layout_rules():
    """Пин: правила вёрстки доезжают до системного промпта рта."""
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    spec = LLM_PROMPT_REGISTRY.get("humanize_result")
    sp = spec.system_prompt
    assert "ВЁРСТКА" in sp
    assert "ПО ОДНОМУ пункту на строку" in sp
    assert "ОТДЕЛЬНОЙ строкой ДО" in sp
    assert "ПОСЛЕ перечня" in sp
    assert "заголовки групп" in sp
    assert "НЕ у каждого пункта" in sp


def test_categoryless_after_group_not_glued():
    """Codex #123 R1 MINOR (оба): бескатегорийный элемент после группы —
    отделён пустой строкой, а не «под чужим заголовком»."""
    out = REGISTRY.render("shopping_list_show", {"items": [
        {"display_line": "молоко", "category": "молочные"},
        {"display_line": "батарейки"},
        "что-то строкой",
    ]})
    lines = out.split("\n")
    i = lines.index("• молоко")
    assert lines[i + 1] == ""  # разделитель после группы
    assert "• батарейки" in lines and "• что-то строкой" in lines


def test_unsorted_literal_duplicates_headers_tolerated():
    """Codex #123 R1 MINOR (medium): несортированный ЛИТЕРАЛ даёт повторный
    заголовок — осознанно терпимо (сервис сортирует; в шаблоне не сортируем,
    чтобы не переупорядочивать видимые пользователю данные). Пин поведения."""
    out = REGISTRY.render("shopping_list_show", {"items": [
        {"display_line": "молоко", "category": "молочные"},
        {"display_line": "хлеб", "category": "хлеб"},
        {"display_line": "сыр", "category": "молочные"},
    ]})
    assert out.count("Молочные:") == 2  # документированная деградация


def test_category_capitalization_preserves_tail():
    """Агент-ревьюер #123 (уникальная находка): первая буква — вверх, хвост
    НЕ трогаем — категории бывают пользовательскими («СВЧ Товары»)."""
    out = REGISTRY.render("shopping_list_show", {"items": [
        {"display_line": "тарелки", "category": "СВЧ Товары"},
    ]})
    assert "СВЧ Товары:" in out and "Свч товары:" not in out


def test_category_labels_ru():
    """#123-добив: внутренние коды таксономии → человеческие ярлыки;
    неизвестные пользовательские — подчёркивания в пробелы, хвост не калечим."""
    from sreda.services.composer.templates_housewife import category_label_ru

    assert category_label_ru("овощи_фрукты") == "Овощи и фрукты"
    assert category_label_ru("мясо_рыба") == "Мясо и рыба"
    assert category_label_ru("бытовая_химия") == "Бытовая химия"
    assert category_label_ru("СВЧ Товары") == "СВЧ Товары"
    assert category_label_ru("моя_полка") == "Моя полка"
    out = REGISTRY.render("shopping_list_show", {"items": [
        {"display_line": "морковь", "category": "овощи_фрукты"},
    ]})
    assert "Овощи и фрукты:" in out and "Овощи_фрукты" not in out


def test_humanize_prompt_has_anti_glue_examples():
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    sp = LLM_PROMPT_REGISTRY.get("humanize_result").system_prompt
    assert "НЕПРАВИЛЬНО: «— хлеб Бакалея:»" in sp
    assert "НЕПРАВИЛЬНО: «— яйца Если нужно" in sp


def test_gemini_flash_lite_provider_registered():
    """(б) #107: кандидат в рот подключён в реестр провайдеров."""
    from sreda.services.llm import CHAT_PROVIDERS, _OPENROUTER_MODEL_BY_PROVIDER

    assert "openrouter-gemini-2.5-flash-lite" in CHAT_PROVIDERS
    assert _OPENROUTER_MODEL_BY_PROVIDER["openrouter-gemini-2.5-flash-lite"] == (
        "google/gemini-2.5-flash-lite"
    )


def test_category_labels_pinned_to_service_taxonomy():
    """Codex #123b R1 MINOR (оба): карта ярлыков покрывает ВСЮ фиксированную
    таксономию сервиса и не содержит чужих ключей (анти-дрейф)."""
    from sreda.db.models.housewife_food import SHOPPING_CATEGORIES
    from sreda.services.composer.templates_housewife import CATEGORY_LABELS_RU

    assert set(CATEGORY_LABELS_RU) == set(SHOPPING_CATEGORIES), (
        "карта ярлыков разъехалась с таксономией сервиса"
    )
