"""ToolSpec instances for the WEB family (Sub-A4 phase 10) —
FINAL family closing 55/55.

3 tools migrated: weather_tool, web_search_tool, fetch_url_tool.

Sources of truth:
- Tool signatures: ``services/weather_tool.py:498``,
  ``services/web_search_tool.py:244,338``
- Output schemas + parsers: ``services/tool_schemas/housewife.py``
  — WeatherToolOk, WebSearchToolOk/Empty, FetchUrlToolOk
- Output is raw_text MVP boundary (same as ListMenuOk pre-promotion)
  — runtime emits free-form forecast/results/page content
- Optional per-tariff (build_*_tool factories aren't always wired
  depending on subscription level)
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.housewife import (
    FetchUrlToolOutput,
    WeatherToolOutput,
    WebSearchToolOutput,
)


# ---------------------------------------------------------------------------
# Web-specific aliases
# ---------------------------------------------------------------------------


WeatherLocation = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""City name or place description («Москва», «Питер центр», «Бали»).
Open-Meteo geocoder handles fuzzy match; cap matches runtime."""


SearchQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Search query for web_search_tool. Free-form; Tavily/DDG handles
tokenization. 500 char cap covers complex multi-keyword queries."""


FetchUrl = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,  # min `http://x.y` ≈ 10 chars
        max_length=2000,  # browsers typically support 2k char URLs
        pattern=r"^https?://[^\s]+$",
    ),
]
"""HTTP(S) URL for fetch_url_tool. Strict regex catches malformed
URLs at planner time before runtime hits httpx."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class WeatherToolInput(BaseModel):
    """Get weather forecast via Open-Meteo (free, no API key, 14d).
    Replaces wttr.in which only gave current weather. Returns text
    summary suitable for direct quotation to the user."""

    model_config = ConfigDict(extra="forbid")
    location: WeatherLocation


class WebSearchToolInput(BaseModel):
    """Web search via Tavily (30/user/мес + 950 global quota) with
    DDG fallback. Quota tracking is automatic via session/tenant/user.
    Returns title + URL + snippet for top hits, or «no results»
    when search returned 0 hits."""

    model_config = ConfigDict(extra="forbid")
    query: SearchQuery


class FetchUrlToolInput(BaseModel):
    """Fetch + extract readable content from one HTTP(S) URL via
    readability-lxml. Use ONLY for URLs the user provided or that
    web_search_tool returned — don't crawl arbitrary URLs. Hard
    timeout 30s, fails closed with typed error variants."""

    model_config = ConfigDict(extra="forbid")
    url: FetchUrl


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


WEATHER_TOOL_SPEC = ToolSpec(
    name="weather_tool",
    description=(
        "Получить прогноз погоды для города/места через Open-Meteo "
        "(бесплатный, без API key, до 14 дней). Возвращает текстовую "
        "сводку готовую к цитированию юзеру: emoji + температура + "
        "осадки + ветер по дням. ИСПОЛЬЗУЙ когда юзер прямо просит "
        "погоду («какая погода завтра», «прогноз на неделю»). НЕ "
        "вызывай для общих вопросов о климате (это знание из модели). "
        "Если geocoder не нашёл город — runtime вернёт error: не "
        "нашла город, переспроси юзера уточнить."
    ),
    family="web",
    effect="read",
    read_domains=["web"],
    write_domains=[],
    input_model=WeatherToolInput,
    output_model=WeatherToolOutput,
    trigger_examples=[
        "какая погода завтра в Москве",
        "прогноз на неделю в Питере",
        "что с погодой на выходных",
        "будет ли дождь в субботу",
    ],
    mutex_notes=[
        "Только для запросов о ПОГОДЕ конкретного места. Для общих климатических вопросов — знание из модели.",
        "Если location не определён — переспроси юзера, не подставляй default.",
    ],
    timeout_seconds=20,
    side_effect_class="read_only",
)


WEB_SEARCH_TOOL_SPEC = ToolSpec(
    name="web_search_tool",
    description=(
        "Найти информацию в интернете через Tavily search (с DDG "
        "fallback при исчерпании квоты). Возвращает топ-3 hit'а с "
        "title + URL + snippet, либо статус empty («no results»). "
        "ИСПОЛЬЗУЙ когда нужна свежая информация которой нет в "
        "модели: новости / актуальные цены / расписания / "
        "specific factual lookup. НЕ вызывай для общих вопросов "
        "которые модель уже знает (история, наука, культура). "
        "Quota: 30/user/мес + 950 global — не злоупотребляй."
    ),
    family="web",
    effect="read",
    read_domains=["web"],
    write_domains=[],
    input_model=WebSearchToolInput,
    output_model=WebSearchToolOutput,
    trigger_examples=[
        "какие сейчас новости про X",
        "найди рецепт борща",
        "что сегодня показывают в кино",
        "сколько стоит билет в Питер",
    ],
    mutex_notes=[
        "Только для свежей информации. Для общих знаний (история, наука) — отвечай из модели.",
        "Для извлечения контента с конкретной страницы → fetch_url_tool (после получения URL из этого search).",
        "Для прогноза погоды — отдельный weather_tool (не web search).",
    ],
    timeout_seconds=30,
    side_effect_class="read_only",
)


FETCH_URL_TOOL_SPEC = ToolSpec(
    name="fetch_url_tool",
    description=(
        "Скачать и извлечь читабельный контент с одной HTTP(S) "
        "страницы (через readability-lxml — удаляет nav/ads/sidebar). "
        "ИСПОЛЬЗУЙ ТОЛЬКО для URL'ов которые ЮЗЕР дал ИЛИ которые "
        "вернул web_search_tool — НЕ ходи по произвольным URL'ам. "
        "Hard timeout 30 сек, fail-closed с typed error variants "
        "(empty url / timeout / http NNN). Возвращает чистый текст "
        "статьи готовый к цитированию или summary."
    ),
    family="web",
    effect="read",
    read_domains=["web"],
    write_domains=[],
    input_model=FetchUrlToolInput,
    output_model=FetchUrlToolOutput,
    trigger_examples=[
        "посмотри по этой ссылке https://...",
        "что на этой странице",
        "извлеки рецепт со страницы которую я скинул",
        "почитай эту статью",
    ],
    mutex_notes=[
        "ТОЛЬКО для user-provided ИЛИ search-returned URL'ов. НЕ для arbitrary crawling.",
        "Для поиска информации (без конкретного URL) → web_search_tool.",
    ],
    timeout_seconds=45,
    side_effect_class="read_only",
)


WEB_SPECS: list[ToolSpec] = [
    WEATHER_TOOL_SPEC,
    WEB_SEARCH_TOOL_SPEC,
    FETCH_URL_TOOL_SPEC,
]


__all__ = [
    "FETCH_URL_TOOL_SPEC",
    "FetchUrl",
    "FetchUrlToolInput",
    "SearchQuery",
    "WEATHER_TOOL_SPEC",
    "WEB_SEARCH_TOOL_SPEC",
    "WEB_SPECS",
    "WeatherLocation",
    "WeatherToolInput",
    "WebSearchToolInput",
]
