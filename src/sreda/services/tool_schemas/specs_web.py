"""ToolSpec instances for the WEB family (Sub-A4 phase 10) —
FINAL family closing 55/55.

3 tools migrated: get_weather, web_search, fetch_url.

Sources of truth:
- Tool signatures: ``services/weather_tool.py:498-507``
  (get_weather location + day_offset + days_count + granularity),
  ``services/web_search_tool.py:244, 338`` (web_search query,
  fetch_url url)
- Codex R1 CRITICAL (both branches): names MUST match the
  ``@lc_tool``-decorated runtime function names exactly, not
  the build_*_tool factory names. specs/manifest/parsers ALL
  use the runtime callable name.
- Codex R1 MAJOR (HIGH): get_weather has 3 extra args planner
  must be able to use (tomorrow / week / hourly / part_of_day).
- Codex R1 MEDIUM (HIGH catch): fetch_url returns JSON envelope
  ``{url, final_url, status, extractor, truncated, length, text}``
  (web_search_tool.py:419) — NOT raw text. Schema must reflect.
- Codex R1 MAJOR (medium): fetch_url runtime rejects localhost /
  private / link-local hosts. Post-#244 the authoritative
  early-reject policy lives in ``services/ssrf_guard.py``
  (``validate_fetch_url``) — the schema validator below delegates
  to it directly (audit 2026-07-18 MINOR: no separate mirror logic
  to drift).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from sreda.services.ssrf_guard import validate_fetch_url
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
"""City name. Open-Meteo geocoder handles fuzzy match."""


SearchQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


def _validate_safe_url(value: str) -> str:
    """SSRF early-reject at planner-input time.

    Audit 2026-07-18 MINOR: previously a hand-rolled mirror of the
    pre-#244 runtime guard (``web_search_tool.py:73``) — it only caught
    canonical IP literals and silently passed obfuscated numeric hosts
    (``http://127.1/``, ``http://2130706433/``, ``http://0x7f.0.0.1/``).
    Now delegates to ``services/ssrf_guard.validate_fetch_url`` — the
    SAME policy function the runtime fetch path uses (#244): scheme,
    userinfo, port (80/443 only), localhost family, numeric/obfuscated
    hosts, denylisted IP literals (incl. NAT64/6to4/Teredo). The
    authoritative SSRF boundary stays the runtime guard + egress
    nftables; this layer just lets the planner see the constraint
    early instead of waiting for the runtime to fail."""
    ok, reason = validate_fetch_url(value)
    if not ok:
        raise ValueError(f"URL rejected (SSRF guard): {reason}")
    return value


FetchUrl = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,
        max_length=2000,
        pattern=r"^https?://[^\s]+$",
    ),
    AfterValidator(_validate_safe_url),
]


WeatherGranularity = Literal["daily", "part_of_day", "hourly"]
"""Per get_weather signature at weather_tool.py:506. Matches three
runtime-supported detail levels."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class WeatherToolInput(BaseModel):
    """Codex R1 MAJOR: schema mirrors full runtime signature
    (weather_tool.py:502) — location + day_offset (0-13, 0=today)
    + days_count (1-14, how many days to show) + granularity
    (daily / part_of_day / hourly).

    Cross-field: day_offset + days_count must fit within 14-day
    forecast window per Open-Meteo cap."""

    model_config = ConfigDict(extra="forbid")
    location: WeatherLocation
    day_offset: int = Field(default=0, ge=0, le=13)
    """0=today, 1=tomorrow, 2=day after tomorrow. Cap 13 because
    max forecast horizon is 14 days (day_offset 13 + days_count 1)."""
    days_count: int = Field(default=1, ge=1, le=14)
    granularity: WeatherGranularity = "daily"

    @model_validator(mode="after")
    def _validate_horizon(self) -> "WeatherToolInput":
        if self.day_offset + self.days_count > 14:
            raise ValueError(
                f"day_offset={self.day_offset} + days_count="
                f"{self.days_count} exceeds 14-day Open-Meteo forecast "
                f"window. Pick start+count that fit in 14 days."
            )
        return self


class WebSearchToolInput(BaseModel):
    """Web search via Tavily (30/user/мес + 950 global quota) with
    DDG fallback."""

    model_config = ConfigDict(extra="forbid")
    query: SearchQuery


class FetchUrlToolInput(BaseModel):
    """Fetch + extract content from one HTTP(S) URL. SSRF-safe URL
    validator mirrors runtime guard (web_search_tool.py:73)."""

    model_config = ConfigDict(extra="forbid")
    url: FetchUrl


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


GET_WEATHER_SPEC = ToolSpec(
    name="get_weather",
    # #144: ужато — перечисление примеров свёрнуто (аргументы самодокументны)
    description=(
        "Получить прогноз погоды через Open-Meteo (free, до 14 дней). "
        "Аргументы: location (город), day_offset (0=сегодня…13; default "
        "0), days_count (1..14; default 1), granularity (daily | "
        "part_of_day | hourly; default daily). «завтра»→day_offset=1; "
        "«на неделю»→days_count=7; «почасовой»→hourly; «как "
        "одеться»→part_of_day. Cap: day_offset+days_count ≤14."
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
        "почасовой прогноз на завтра",
        "как одеться сегодня",
    ],
    mutex_notes=[
        "Только для запросов о ПОГОДЕ. НЕ вызывай fetch_url на wttr.in и НЕ web_search для погоды — этот точнее (Open-Meteo).",
        "Если location не определён — переспроси юзера, не подставляй default.",
    ],
    timeout_seconds=20,
    side_effect_class="read_only",
)


WEB_SEARCH_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Найти информацию в интернете через Tavily search (DDG fallback "
        "при исчерпании квоты). Возвращает топ hits (title + URL + "
        "snippet) либо status=empty («no results»). Для свежего: "
        "новости/цены/расписания. НЕ для общих знаний (модель знает). "
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
        "Только для свежей информации. Для общих знаний — отвечай из модели.",
        "Для извлечения контента с конкретной страницы → fetch_url. Для погоды → get_weather.",
    ],
    timeout_seconds=30,
    side_effect_class="read_only",
)


FETCH_URL_SPEC = ToolSpec(
    name="fetch_url",
    description=(
        "Скачать и извлечь читабельный контент с одной HTTP(S) "
        "страницы. SSRF-safe: localhost/private/link-local rejected. "
        "ТОЛЬКО для URL'ов, которые дал ЮЗЕР ИЛИ вернул web_search — НЕ "
        "для arbitrary crawling. Runtime возвращает JSON envelope (поля "
        "url/final_url/status/extractor/truncated/length/text) — planner "
        "работает со структурированным FetchUrlToolOk."
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
        "ТОЛЬКО для user-provided ИЛИ search-returned URL'ов. НЕ для crawling.",
        "Для поиска (без URL) → web_search. Для погоды → get_weather.",
    ],
    timeout_seconds=45,
    side_effect_class="read_only",
)


WEB_SPECS: list[ToolSpec] = [
    GET_WEATHER_SPEC,
    WEB_SEARCH_SPEC,
    FETCH_URL_SPEC,
]


__all__ = [
    "FETCH_URL_SPEC",
    "FetchUrl",
    "FetchUrlToolInput",
    "GET_WEATHER_SPEC",
    "SearchQuery",
    "WEB_SEARCH_SPEC",
    "WEB_SPECS",
    "WeatherGranularity",
    "WeatherLocation",
    "WeatherToolInput",
    "WebSearchToolInput",
]
