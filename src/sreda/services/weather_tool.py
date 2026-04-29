"""Weather forecast tool — Open-Meteo (free, no API key, 16-day forecast).

2026-04-29 (incident user_tg_352612382: «погода завтра в Сходне»):
- `fetch_url(wttr.in)` отдавал ТОЛЬКО current weather, не forecast →
  LLM выкручивался плохо ("сервис показывает только текущую").
- `web_search` через DDG/Bing блокировался RU egress → ConnectError.

Open-Meteo решает обе проблемы:
* free, без регистрации (не нужен API key в .env);
* 16-дневный forecast с гранулярностью «daily» (max/min temp,
  precipitation, weathercode);
* отдельный geocoding endpoint (city name → lat/lon, RU поддерживается);
* доступен напрямую с VDS без SOCKS5-прокси (verified 2026-04-29).

Architecture:
* In-memory geocoding cache (`_GEO_CACHE`) — города повторяются часто
  (Сходня каждый день для одного юзера). Process-lifetime, без TTL —
  координаты городов не меняются.
* WMO weather code → emoji map (`_WEATHER_EMOJI`) — компактный визуал
  в ответе бота, юзер быстро парсит.
* F1 формат: одна строка на день («🌧️ ср 29 апр: +3°…+1° · 3.1мм»).

Tool signature:
    get_weather(location: str, day_offset: int = 0, days_count: int = 1)

LLM сам резолвит «завтра» → day_offset=1 + days_count=1, «на неделю»
→ days_count=7 и т.п.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

import httpx
from langchain_core.tools import tool as lc_tool

logger = logging.getLogger(__name__)


_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT_SECONDS = 8.0
_MAX_FORECAST_DAYS = 14  # Open-Meteo поддерживает до 16, но clip 14 для UX
_DEFAULT_TZ = "Europe/Moscow"


# Process-lifetime cache: name (lowercase, normalised) → (lat, lon, display_name, timezone)
_GEO_CACHE: dict[str, tuple[float, float, str, str]] = {}


# WMO weathercode → emoji (https://open-meteo.com/en/docs)
_WEATHER_EMOJI: dict[int, str] = {
    0: "☀️",      # Clear sky
    1: "🌤️",     # Mainly clear
    2: "⛅",      # Partly cloudy
    3: "☁️",      # Overcast
    45: "🌫️", 48: "🌫️",  # Fog
    51: "🌦️", 53: "🌦️", 55: "🌦️",  # Drizzle light/moderate/dense
    56: "🌨️", 57: "🌨️",  # Freezing drizzle
    61: "🌧️", 63: "🌧️", 65: "🌧️",  # Rain
    66: "🌨️", 67: "🌨️",  # Freezing rain
    71: "🌨️", 73: "🌨️", 75: "🌨️",  # Snow fall
    77: "❄️",     # Snow grains
    80: "🌦️", 81: "🌦️", 82: "🌦️",  # Rain showers
    85: "🌨️", 86: "🌨️",  # Snow showers
    95: "⛈️",     # Thunderstorm
    96: "⛈️", 99: "⛈️",  # Thunderstorm with hail
}


_RU_WEEKDAYS_SHORT: dict[int, str] = {
    0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс",
}

_RU_MONTHS_GEN: dict[int, str] = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


def _normalize_location(name: str) -> str:
    return " ".join((name or "").lower().split())


def _geocode(location: str) -> tuple[float, float, str, str] | None:
    """Resolve city name → (lat, lon, display_name, tz). Cache hit returns
    stored tuple. None on lookup failure."""
    key = _normalize_location(location)
    if not key:
        return None
    cached = _GEO_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        r = httpx.get(
            _GEOCODE_URL,
            params={"name": location, "language": "ru", "count": 1},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("weather geocode failed for %r: %s", location, exc)
        return None
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    try:
        lat = float(first["latitude"])
        lon = float(first["longitude"])
    except (KeyError, ValueError, TypeError):
        return None
    display = first.get("name") or location
    tz = first.get("timezone") or _DEFAULT_TZ
    tup = (lat, lon, display, tz)
    _GEO_CACHE[key] = tup
    return tup


def _format_day_line(
    day: _date, code: int, tmax: float, tmin: float, precip_mm: float,
) -> str:
    """F1 format: '🌧️ ср 29 апр: +3°…+1° · 3.1мм'"""
    emoji = _WEATHER_EMOJI.get(code, "·")
    weekday = _RU_WEEKDAYS_SHORT.get(day.weekday(), "")
    month = _RU_MONTHS_GEN.get(day.month, "?")
    date_part = f"{weekday} {day.day} {month}"
    # +3°…+1° (max…min, signed). Round half away from zero для деликатных
    # значений типа -0.4°/+0.4° (хочется явный знак).
    tmax_int = round(tmax)
    tmin_int = round(tmin)
    sign = lambda x: f"{x:+d}".replace("+0", "0") if x == 0 else f"{x:+d}"
    temp_part = f"{sign(tmax_int)}°…{sign(tmin_int)}°"
    # Осадки: «без осадков» если 0, иначе «N.Nмм»
    if precip_mm <= 0.05:
        precip_part = "без осадков"
    else:
        precip_part = f"{precip_mm:.1f}мм".rstrip("0").rstrip(".") + "мм" if False else f"{precip_mm:.1f}мм"
    return f"{emoji} {date_part}: {temp_part} · {precip_part}"


def _fetch_forecast(
    lat: float, lon: float, tz: str, forecast_days: int,
) -> dict | None:
    try:
        r = httpx.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,weathercode"
                ),
                "timezone": tz,
                "forecast_days": forecast_days,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("weather forecast failed for %.3f,%.3f: %s", lat, lon, exc)
        return None


def build_weather_tool() -> Callable:
    """Return LangChain tool ``get_weather(location, day_offset, days_count)``."""

    @lc_tool
    def get_weather(
        location: str,
        day_offset: int = 0,
        days_count: int = 1,
    ) -> str:
        """Прогноз погоды через Open-Meteo (free, без API key, 14 дней вперёд).

        Используй этот tool для ЛЮБЫХ запросов про погоду — текущую
        или прогноз. НЕ вызывай ``fetch_url`` на wttr.in или
        ``web_search`` для погоды — этот tool точнее (структурный
        forecast вместо HTML-парсинга) и работает с VDS без прокси.

        Args:
            location: Название города на русском или английском. Пример:
                «Сходня», «Москва», «Санкт-Петербург», «Berlin». Tool
                сам резолвит координаты через geocoding API.
            day_offset: 0=сегодня, 1=завтра, 2=послезавтра. Default 0.
            days_count: Сколько дней показать начиная с day_offset.
                1=один день, 7=неделя, 14=максимум. Default 1.

        Examples:
            «погода завтра в Сходне»  →  get_weather("Сходня", 1, 1)
            «погода на неделю»        →  get_weather("Москва", 0, 7)
            «погода в выходные»       →  get_weather("Питер", 5, 2)  # cб+вс если ср

        Returns:
            Multi-line текст:
                Сходня:
                🌧️ ср 29 апр: +3°…+1° · 3.1мм
                ⛅ чт 30 апр: +6°…-2° · без осадков
            Или короткий ``error: ...`` на сбое.
        """
        # Validate inputs
        if not (location or "").strip():
            return "error: empty location"
        try:
            day_offset = int(day_offset)
        except (TypeError, ValueError):
            day_offset = 0
        try:
            days_count = int(days_count)
        except (TypeError, ValueError):
            days_count = 1
        day_offset = max(0, min(day_offset, _MAX_FORECAST_DAYS - 1))
        days_count = max(1, min(days_count, _MAX_FORECAST_DAYS - day_offset))

        geo = _geocode(location)
        if geo is None:
            return f"error: не нашла город {location!r} — уточни"
        lat, lon, display, tz = geo

        forecast_total_days = day_offset + days_count
        data = _fetch_forecast(lat, lon, tz, forecast_total_days)
        if data is None:
            return "error: сервис погоды не отвечает, попробуй позже"

        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        codes = daily.get("weathercode") or []
        precip = daily.get("precipitation_sum") or []

        if not dates:
            return "error: пустой прогноз"

        # Slice [day_offset : day_offset + days_count]
        end = min(day_offset + days_count, len(dates))
        lines: list[str] = []
        for i in range(day_offset, end):
            try:
                d = _datetime.fromisoformat(dates[i]).date()
            except (ValueError, TypeError):
                continue
            try:
                line = _format_day_line(
                    d,
                    code=int(codes[i]) if i < len(codes) else -1,
                    tmax=float(tmax[i]) if i < len(tmax) else 0.0,
                    tmin=float(tmin[i]) if i < len(tmin) else 0.0,
                    precip_mm=float(precip[i]) if i < len(precip) else 0.0,
                )
            except (ValueError, TypeError, IndexError):
                continue
            lines.append(line)

        if not lines:
            return "error: не удалось распарсить прогноз"

        return f"{display}:\n" + "\n".join(lines)

    return get_weather
