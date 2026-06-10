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
    assert "Овощи_фрукты:" in lines
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
