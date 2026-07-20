"""Weather forecast tool — Open-Meteo (free, no API key, 16-day forecast).

2026-04-29 (incident user_tg_352612382: «погода завтра в Сходне»):
- `fetch_url(wttr.in)` отдавал ТОЛЬКО current weather, не forecast →
  LLM выкручивался плохо ("сервис показывает только текущую").
- `web_search` через DDG/Bing блокировался RU egress → ConnectError.

2026-05-08 (incident trace_2e248c2277d94e47, 78510ms):
- На запрос «почасовой прогноз» Среда игнорировала get_weather (daily-only)
  и уходила в 6× sequential fetch_url к wttr.in за ~78 секунд.
- Расширен с поддержкой granularity={daily, part_of_day, hourly}.
  Open-Meteo бесплатно даёт hourly endpoint — теперь используем его.

Open-Meteo решает проблемы:
* free, без регистрации (не нужен API key в .env);
* до 16 дней forecast в daily / hourly режимах;
* отдельный geocoding endpoint (city name → lat/lon, RU поддерживается);
* доступен напрямую с VDS без SOCKS5-прокси (verified 2026-04-29).

Architecture:
* In-memory geocoding cache (`_GEO_CACHE`) — города повторяются часто.
  Process-lifetime, без TTL — координаты не меняются. Lock-protected
  для thread-safety после Phase B parallel dispatch.
* `_compute_hourly_window()` — single source of bounds для API
  start_hour/end_hour И post-filter (consistency).
* WMO weather code → emoji map + severity ranking (для bucket'инга).
* F1 формат: одна строка на день («🌧️ ср 29 апр: +3°…+1° · 3.1мм»).

Tool signature:
    get_weather(location, day_offset=0, days_count=1, granularity="daily")

LLM сам резолвит intent → granularity:
- «во сколько дождь» → granularity="hourly"
- «как одеться сегодня» → granularity="part_of_day"
- «погода на неделю» → granularity="daily" (default)
"""

from __future__ import annotations

import datetime as _datetime_module
import logging
import threading
import time as _time_module
from collections.abc import Callable
from datetime import (
    date as _date,
    datetime as _datetime,
    time as _time,
    timedelta as _timedelta,
    tzinfo as _tzinfo,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from langchain_core.tools import tool as lc_tool

logger = logging.getLogger(__name__)


_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT_SECONDS = 8.0
_MAX_FORECAST_DAYS = 14  # Open-Meteo поддерживает до 16, но clip 14 для UX
_DEFAULT_TZ = "Europe/Moscow"
_HOURLY_CAP_HOURS = 48  # max часов в hourly output (даже если days_count=7)


# Process-lifetime cache: name (lowercase, normalised) → (lat, lon, display_name, timezone)
_GEO_CACHE: dict[str, tuple[float, float, str, str]] = {}
# Lock-protected чтобы Phase B parallel dispatch не race'ил на dict write.
_GEO_CACHE_LOCK = threading.Lock()

# Аудит 2026-07-18 (svc-features #11): негативный кэш геокодинга. Без него
# неудачный город или лежащий Open-Meteo (timeout=8с) повторно бился в сеть на
# каждый retry LLM внутри хода — серийные 8-секундные таймауты на hot-path.
# key → monotonic-expiry; TTL короткий (60с): ход длится секунды, а восстановление
# провайдера не маскируется надолго. Success-кэш без TTL (координаты не меняются),
# негатив — транзиентен, поэтому с TTL.
_GEO_NEG_CACHE: dict[str, float] = {}
_GEO_NEG_TTL_SECONDS = 60.0
_GEO_NEG_CACHE_MAX = 1024  # bound против мусорных уникальных городов (П2-гигиена)

# Дефолт per-turn storm-cap для get_weather (svc-features #11; как
# react_web_search_per_turn_cap=4 в #211). Переопределяется параметром
# build_weather_tool(per_turn_cap=...).
_WEATHER_DEFAULT_PER_TURN_CAP = 4


def _geo_neg_remember(key: str) -> None:
    """Записать негативный результат с TTL; при переполнении — purge протухших."""
    with _GEO_CACHE_LOCK:
        if len(_GEO_NEG_CACHE) >= _GEO_NEG_CACHE_MAX:
            now = _time_module.monotonic()
            for k in [k for k, exp in _GEO_NEG_CACHE.items() if exp <= now]:
                del _GEO_NEG_CACHE[k]
        _GEO_NEG_CACHE[key] = _time_module.monotonic() + _GEO_NEG_TTL_SECONDS


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


# WMO weathercode → severity ranking (для dominant code в dry buckets).
# Используется когда precip всюду 0 — выбираем «характерное» состояние,
# не первый час. Higher rank = более выраженное явление.
_WEATHER_SEVERITY: dict[int, int] = {
    0: 0,    # clear
    1: 1, 2: 2, 3: 3,                 # mostly clear → overcast
    45: 4, 48: 4,                     # fog
    51: 5, 53: 5, 55: 5,              # drizzle
    56: 6, 57: 6,                     # freezing drizzle
    61: 7, 63: 7, 65: 7,              # rain
    66: 8, 67: 8,                     # freezing rain
    71: 7, 73: 7, 75: 7,              # snow
    77: 6,                            # snow grains
    80: 7, 81: 7, 82: 7,              # rain showers
    85: 7, 86: 7,                     # snow showers
    95: 9, 96: 9, 99: 9,              # thunderstorm
}


_RU_WEEKDAYS_SHORT: dict[int, str] = {
    0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс",
}

_RU_MONTHS_GEN: dict[int, str] = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


# Granularity normalization — fail-loud (не silent fallback).
_GRANULARITY_ALIASES: dict[str, str] = {
    "daily": "daily", "day": "daily", "по дням": "daily",
    "part_of_day": "part_of_day", "parts": "part_of_day",
    "part-of-day": "part_of_day", "parts_of_day": "part_of_day",
    "по частям дня": "part_of_day", "утром-днём-вечером": "part_of_day",
    "hourly": "hourly", "hour": "hourly", "hours": "hourly",
    "by_hour": "hourly", "почасовой": "hourly",
}


# Part-of-day buckets: (hour_start_inclusive, hour_end_exclusive, label).
_PART_OF_DAY_BUCKETS: list[tuple[int, int, str]] = [
    (0, 6, "ночь"),
    (6, 12, "утро"),
    (12, 18, "день"),
    (18, 24, "вечер"),
]


def _normalize_location(name: str) -> str:
    return " ".join((name or "").lower().split())


def _normalize_granularity(value: str | None) -> str | None:
    """Returns canonical granularity name or None if unknown.

    None → caller возвращает explicit error so LLM может retry с правильным
    значением. Silent fallback на daily маскирует drift («hour» вместо «hourly»).
    """
    return _GRANULARITY_ALIASES.get((value or "").strip().lower())


def _resolve_timezone(tz_name: str | None) -> tuple[_tzinfo, str]:
    """Returns (tzinfo, canonical_request_name).

    Оба нужны: tzinfo для local datetime arithmetic, canonical name для
    params["timezone"] чтобы API получил valid string, не Junk/Bogus.
    Fallback chain: tz_name → Europe/Moscow → UTC. Final hard fallback —
    datetime.timezone.utc (всегда работает, ZoneInfo("UTC") может не быть
    на Windows без tzdata).
    """
    candidates = [tz_name, _DEFAULT_TZ, "UTC"]
    for tz in candidates:
        if not tz:
            continue
        try:
            return ZoneInfo(tz), tz
        except ZoneInfoNotFoundError:
            logger.warning("invalid timezone %r, falling back", tz)
    return _datetime_module.timezone.utc, "UTC"


def _dominant_weather_code(
    codes: list[int], precips: list[float],
) -> int:
    """Pick the «characteristic» weather code for a bucket.

    Если есть precip > 0.1мм в bucket — код часа с max precip (самый
    «осадочный» час). Если precip всюду 0 — severity-ranked: max
    severity среди codes. Это решает case: dry bucket с переменной
    облачностью → выбирает overcast (3) > partly cloudy (2) > clear (0),
    не первый час.
    """
    if not codes:
        return 0
    # Phase 1: max-precip if any wet hour
    if precips and any(p > 0.1 for p in precips):
        max_idx = max(range(len(precips)), key=lambda i: precips[i])
        if max_idx < len(codes):
            return int(codes[max_idx])
    # Phase 2: severity-ranked
    return max(codes, key=lambda c: _WEATHER_SEVERITY.get(int(c), 0))


def _bucket_hours_by_part_of_day(
    times_iso: list[str],
    temps: list[float],
    codes: list[int],
    precips: list[float],
    winds: list[float],
) -> list[tuple[_date, str, int, float, float, float]]:
    """Group hourly arrays into per-day part-of-day buckets.

    Returns list of (day, bucket_label, dominant_code, tavg, precip_sum, wind_avg).
    Поддерживает несколько дней — buckets группируются по (day, label).
    """
    by_key: dict[tuple[_date, str], dict] = {}

    for i, t_iso in enumerate(times_iso):
        try:
            dt = _datetime.fromisoformat(t_iso)
        except (ValueError, TypeError):
            continue
        hour = dt.hour
        # Find bucket
        label = None
        for h_start, h_end, name in _PART_OF_DAY_BUCKETS:
            if h_start <= hour < h_end:
                label = name
                break
        if label is None:
            continue

        key = (dt.date(), label)
        bucket = by_key.setdefault(key, {
            "codes": [], "temps": [], "precips": [], "winds": [],
        })
        if i < len(codes):
            try:
                bucket["codes"].append(int(codes[i]))
            except (ValueError, TypeError):
                pass
        if i < len(temps):
            try:
                bucket["temps"].append(float(temps[i]))
            except (ValueError, TypeError):
                pass
        if i < len(precips):
            try:
                bucket["precips"].append(float(precips[i]))
            except (ValueError, TypeError):
                pass
        if i < len(winds):
            try:
                bucket["winds"].append(float(winds[i]))
            except (ValueError, TypeError):
                pass

    # Build sorted output: by date, then by canonical bucket order.
    bucket_order = {name: idx for idx, (_, _, name) in enumerate(_PART_OF_DAY_BUCKETS)}
    sorted_keys = sorted(by_key.keys(), key=lambda k: (k[0], bucket_order[k[1]]))

    out: list[tuple[_date, str, int, float, float, float]] = []
    for key in sorted_keys:
        bucket = by_key[key]
        if not bucket["temps"]:
            continue
        code = _dominant_weather_code(bucket["codes"], bucket["precips"])
        tavg = sum(bucket["temps"]) / len(bucket["temps"])
        precip_sum = sum(bucket["precips"]) if bucket["precips"] else 0.0
        wind_avg = (
            sum(bucket["winds"]) / len(bucket["winds"])
            if bucket["winds"] else 0.0
        )
        out.append((key[0], key[1], code, tavg, precip_sum, wind_avg))
    return out


def _compute_hourly_window(
    now_local: _datetime,
    location_tz: _tzinfo,
    granularity: str,
    day_offset: int,
    days_count: int,
) -> tuple[_datetime, _datetime, str, str, bool, _datetime]:
    """Compute hourly window in location-local timezone.

    Returns:
      (start_local, effective_end_local, request_start, request_end, truncated, raw_end_local)

    `effective_end_local` — уже учитывает 48h cap. Используется И для
    API params, И для post-filter (single source of truth, no drift).
    `raw_end_local` — uncapped, для truncation marker text.

    Семантика:
    - hourly + day_offset=0  →  start = current hour (округлённо вниз),
                                end = последний часовой отметка дня day_offset+days_count-1
    - hourly + day_offset>0  →  start = 00:00 целевого дня
    - part_of_day             →  start = 00:00 (полный день для bucket'инга)
    """
    base_date = now_local.date() + _timedelta(days=day_offset)

    # ⚠ Codex r3 MAJOR: для hourly+day_offset=0 ВСЕГДА стартуем с now (даже
    # если days_count>1) — иначе past hours съедают часть 48h cap.
    if granularity == "hourly" and day_offset == 0:
        start_local = now_local.replace(minute=0, second=0, microsecond=0)
    else:
        start_local = _datetime.combine(base_date, _time(0, 0), tzinfo=location_tz)

    end_date = base_date + _timedelta(days=days_count - 1)
    end_local = _datetime.combine(end_date, _time(23, 0), tzinfo=location_tz)
    raw_end_local = end_local

    # Soft cap: 48h независимо от user-requested days_count.
    delta_hours = int((end_local - start_local).total_seconds() // 3600) + 1
    if granularity == "hourly" and delta_hours > _HOURLY_CAP_HOURS:
        # 47 hours offset → 48 inclusive entries (start..start+47h).
        effective_end_local = start_local + _timedelta(hours=_HOURLY_CAP_HOURS - 1)
        truncated = True
    else:
        effective_end_local = end_local
        truncated = False

    request_start = start_local.strftime("%Y-%m-%dT%H:%M")
    request_end = effective_end_local.strftime("%Y-%m-%dT%H:%M")
    return start_local, effective_end_local, request_start, request_end, truncated, raw_end_local


def _format_signed_temp(t: float) -> str:
    """+3° / 0° / -1°"""
    val = round(t)
    if val == 0:
        return "0°"
    return f"{val:+d}°"


def _format_precip(precip_mm: float) -> str:
    if precip_mm <= 0.05:
        return "без осадков"
    return f"{precip_mm:.1f}мм"


def _format_wind(wind_ms: float) -> str:
    return f"{round(wind_ms)}м/с"


def _safe_float(v: object) -> float | None:
    """#176: optional-поле из API → float или None (битое НЕ роняет строку, остаётся additive)."""
    try:
        return float(v) if v is not None else None  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _hhmm(iso: object) -> str | None:
    """#176: ISO «2026-06-19T03:44» → «03:44». None если формат не строгий HH:MM (Codex MINOR:
    не пускаем мусор после «T» в «Светло …»)."""
    if not isinstance(iso, str) or "T" not in iso:
        return None
    part = iso.split("T", 1)[1][:5]
    if len(part) == 5 and part[:2].isdigit() and part[2] == ":" and part[3:].isdigit():
        return part
    return None


_HPA_TO_MMHG = 0.750062  # #176: гПа → мм рт.ст. (конвенция для RU)


def _format_day_line(
    day: _date, code: int, tmax: float, tmin: float, precip_mm: float,
    *, feels_max: float | None = None, feels_min: float | None = None,
    prob: float | None = None, gust: float | None = None, uv: float | None = None,
) -> str:
    """F1 format: '🌧️ ср 29 апр: +3°…+1° · 3.1мм'. #176: опц. ощущается/вероятность/порывы/UV —
    дописываются ТОЛЬКО когда поле пришло из API (обратная совместимость)."""
    emoji = _WEATHER_EMOJI.get(code, "·")
    weekday = _RU_WEEKDAYS_SHORT.get(day.weekday(), "")
    month = _RU_MONTHS_GEN.get(day.month, "?")
    date_part = f"{weekday} {day.day} {month}"
    temp_part = f"{_format_signed_temp(tmax)}…{_format_signed_temp(tmin)}"
    # #176 «ощущается» — только если заметно (≥2°) отличается от фактической (иначе шум)
    if (feels_max is not None and feels_min is not None
            and (abs(feels_max - tmax) >= 2 or abs(feels_min - tmin) >= 2)):
        temp_part += (f" (ощущ. {_format_signed_temp(feels_max)}…"
                      f"{_format_signed_temp(feels_min)})")
    # #176 вероятность осадков объединяем с суммой (без «осадки 30%, без осадков»)
    if prob is not None and prob > 0:
        if precip_mm > 0.05:
            precip_part = f"осадки {int(round(prob))}% ({precip_mm:.1f}мм)"
        else:
            precip_part = f"осадки {int(round(prob))}%"
    else:
        precip_part = _format_precip(precip_mm)
    gust_part = f" · порывы {round(gust)} м/с" if gust is not None and gust >= 12 else ""
    # #176 хвост: UV — только заметный (≥3, когда уже стоит мазаться кремом)
    uv_part = f" · УФ {round(uv)}" if uv is not None and uv >= 3 else ""
    return f"{emoji} {date_part}: {temp_part} · {precip_part}{gust_part}{uv_part}"


def _format_hour_line(
    dt: _datetime, code: int, temp: float, precip_mm: float, wind_ms: float,
    *, prob: float | None = None, gust: float | None = None,
) -> str:
    """Format: '☀️ 09:00 +4° · 0мм · 3м/с'. #176: + вероятность осадков и порывы (когда есть)."""
    emoji = _WEATHER_EMOJI.get(code, "·")
    hh = f"{dt.hour:02d}:00"
    temp_part = _format_signed_temp(temp)
    if precip_mm <= 0.05:
        precip_part = "0мм"
    else:
        precip_part = f"{precip_mm:.1f}мм"
    if prob is not None and prob > 0:  # #176 вероятность осадков рядом с суммой
        precip_part += f" ({int(round(prob))}%)"
    wind_part = _format_wind(wind_ms)
    if gust is not None and gust >= 12:  # #176 порывы — только заметные
        wind_part += f" (порывы {round(gust)} м/с)"
    return f"  {emoji} {hh} {temp_part} · {precip_part} · {wind_part}"


def _format_part_of_day_line(
    bucket_label: str, code: int, tavg: float, precip_mm: float, wind_avg: float,
) -> str:
    """Format: '🌧️ день 12–18: +6° · 2.1мм · 5м/с'"""
    emoji = _WEATHER_EMOJI.get(code, "·")
    # Look up bucket time range
    h_range = next(
        (f"{s:02d}–{e:02d}" for s, e, n in _PART_OF_DAY_BUCKETS if n == bucket_label),
        "",
    )
    label_part = f"{bucket_label} {h_range}".rstrip()
    temp_part = _format_signed_temp(tavg)
    precip_part = _format_precip(precip_mm)
    wind_part = _format_wind(wind_avg)
    return f"{emoji} {label_part}: {temp_part} · {precip_part} · {wind_part}"


def _format_day_header(day: _date) -> str:
    """'ср 8 май' (для hourly multi-day и part_of_day single-day header)."""
    weekday = _RU_WEEKDAYS_SHORT.get(day.weekday(), "")
    month = _RU_MONTHS_GEN.get(day.month, "?")
    return f"{weekday} {day.day} {month}"


def _geocode(location: str) -> tuple[float, float, str, str] | None:
    """Resolve city name → (lat, lon, display_name, tz).

    Cache hit returns stored tuple. None on lookup failure.
    Lock-protected (read под lock, network call ВНЕ lock, write
    под lock через setdefault).
    """
    key = _normalize_location(location)
    if not key:
        return None

    # Read под lock (Codex Phase B finding — concurrent dispatch может race).
    with _GEO_CACHE_LOCK:
        cached = _GEO_CACHE.get(key)
        neg_expiry = _GEO_NEG_CACHE.get(key)
    if cached is not None:
        return cached
    # Аудит 2026-07-18 (svc-features #11): негативный кэш — повторный сетевой
    # вызов в пределах TTL не делаем (лежачий провайдер/неудачный город не
    # штурмуем на каждый retry LLM внутри хода).
    if neg_expiry is not None and neg_expiry > _time_module.monotonic():
        return None

    # Network call ВНЕ lock — long, не держим pool.
    try:
        r = httpx.get(
            _GEOCODE_URL,
            params={"name": location, "language": "ru", "count": 1},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Аудит 2026-07-18 (svc-features #8 / cross-security П4): город юзера —
        # PII (фактическая геолокация), в лог открыто не пишем — только длина.
        # 2026-07-18 (R1 C8): str(httpx.HTTPStatusError) содержит полный URL
        # с ``name=…`` в query — логируем только класс ошибки + HTTP status.
        logger.warning(
            "weather geocode failed (location len=%d): %s (HTTP %s)",
            len(location or ""),
            type(exc).__name__,
            getattr(getattr(exc, "response", None), "status_code", None),
        )
        _geo_neg_remember(key)
        return None

    results = data.get("results") or []
    if not results:
        _geo_neg_remember(key)  # город не найден — в пределах хода не переспрашиваем
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

    # Write под lock c setdefault (избегаем lost-update если два thread'а
    # одновременно cache miss'нули один и тот же key).
    with _GEO_CACHE_LOCK:
        return _GEO_CACHE.setdefault(key, tup)


def _fetch_forecast(
    lat: float,
    lon: float,
    request_tz_name: str,
    granularity: str,
    forecast_days: int,
    request_start_hour: str | None = None,
    request_end_hour: str | None = None,
) -> dict | None:
    """Fetch Open-Meteo forecast.

    Always requests `daily` summary. Adds `hourly` + `start_hour`/`end_hour`
    when granularity ∈ {hourly, part_of_day}.

    ⚠ Phase 0 finding (2026-05-08): forecast_days клипает window когда
    одновременно set'нут start_hour. Поэтому для не-daily granularities —
    НЕ передаём forecast_days, только hour-window определяет range.
    """
    params: dict = {
        "latitude": lat,
        "longitude": lon,
        "timezone": request_tz_name,
        "daily": (
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,weathercode,"
            "apparent_temperature_max,apparent_temperature_min,"   # #176 ощущается
            "precipitation_probability_max,wind_gusts_10m_max,"     # #176 вероятность/порывы
            "uv_index_max,sunrise,sunset"                           # #176 хвост: UV/рассвет/закат
        ),
        "current": (  # #176 текущие условия (для «сейчас/сегодня»)
            "temperature_2m,apparent_temperature,precipitation,weather_code,"
            "wind_speed_10m,wind_gusts_10m,relative_humidity_2m,pressure_msl"  # #176 хвост: давление
        ),
        "wind_speed_unit": "ms",
    }

    if granularity in ("hourly", "part_of_day"):
        # Hourly mode: omit forecast_days (Phase 0 — clips start_hour window).
        params["hourly"] = (
            "temperature_2m,precipitation,weathercode,windspeed_10m,"
            "precipitation_probability,wind_gusts_10m"  # #176
        )
        if request_start_hour:
            params["start_hour"] = request_start_hour
        if request_end_hour:
            params["end_hour"] = request_end_hour
    else:
        # Daily-only: keep forecast_days (legacy behaviour).
        params["forecast_days"] = forecast_days

    try:
        r = httpx.get(
            _FORECAST_URL,
            params=params,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Аудит 2026-07-18 (svc-features #8 / cross-security П4): lat/lon с
        # точностью 3 знака — фактическая геолокация юзера (PII), в лог не пишем.
        # 2026-07-18 (R1 C8): str(httpx.HTTPStatusError) содержит полный URL
        # с ``latitude``/``longitude`` в query — логируем только класс ошибки
        # + HTTP status.
        logger.warning(
            "weather forecast failed granularity=%s: %s (HTTP %s)",
            granularity,
            type(exc).__name__,
            getattr(getattr(exc, "response", None), "status_code", None),
        )
        return None


def build_weather_tool(*, per_turn_cap: int | None = None) -> Callable:
    """Return LangChain tool ``get_weather(location, day_offset, days_count, granularity)``.

    Аудит 2026-07-18 (svc-features #11): per-turn storm-cap по образцу #211
    (web_search/fetch_url). build_weather_tool пересобирается КАЖДЫЙ ход
    (build_slice_tools per-turn) → счётчик в замыкании сам сбрасывается.
    Дефолт 4 (как react_web_search_per_turn_cap); <=0 = выкл. Настройки в
    config нет — параметр для будущего knob'а и тестов.
    """
    if per_turn_cap is None:
        per_turn_cap = _WEATHER_DEFAULT_PER_TURN_CAP
    try:
        per_turn_cap = max(0, int(per_turn_cap or 0))  # клампим (<=0 = выкл)
    except (ValueError, TypeError):
        per_turn_cap = 0
    _turn_weather_calls = [0]
    # Lock — корректный инкремент при конкурентных read-шагах plan-execute
    # (asyncio.gather + thread-pool), как в web_search (#211, Codex R1 MAJOR).
    _turn_weather_lock = threading.Lock()

    @lc_tool
    def get_weather(
        location: str,
        day_offset: int = 0,
        days_count: int = 1,
        granularity: str = "daily",
    ) -> str:
        """Прогноз погоды через Open-Meteo (free, без API key, до 14 дней).

        Используй этот tool для ЛЮБЫХ запросов про погоду — текущую,
        прогноз дневной, по частям дня, или почасовой. НЕ вызывай
        ``fetch_url`` на wttr.in или ``web_search`` для погоды — этот
        tool точнее (структурный forecast, hourly endpoint) и работает
        с VDS без прокси.

        Args:
            location: Название города на русском или английском. Пример:
                «Сходня», «Москва», «Санкт-Петербург», «Berlin». Tool
                сам резолвит координаты через geocoding API.
            day_offset: 0=сегодня, 1=завтра, 2=послезавтра. Default 0.
            days_count: Сколько дней показать начиная с day_offset.
                1=один день, 7=неделя, 14=максимум. Default 1.
            granularity: Уровень детализации прогноза:
                - "daily" (default) — общая сводка день за днём («погода
                  на неделю», «прогноз завтра»).
                - "part_of_day" — разбивка на ночь/утро/день/вечер
                  («как одеться сегодня», «будет ли дождь днём»,
                  «утром-днём-вечером»).
                - "hourly" — почасовая разбивка («во сколько начнётся
                  дождь», «когда стихнет ветер», «почасовой прогноз»).

        Examples:
            «погода завтра в Сходне»          → get_weather("Сходня", 1, 1)
            «погода на неделю»                → get_weather("Москва", 0, 7)
            «как одеться сегодня в Питере»   → get_weather("Питер", 0, 1, "part_of_day")
            «во сколько дождь сегодня?»       → get_weather("Москва", 0, 1, "hourly")
            «почасовой прогноз на завтра»     → get_weather("Москва", 1, 1, "hourly")

        Returns:
            Multi-line текст с погодой согласно granularity. Или короткий
            ``error: ...`` на сбое (city not found, API timeout, etc.).
        """
        # Validate inputs
        if not (location or "").strip():
            return "error: empty location"

        # Аудит 2026-07-18 (svc-features #11): жёсткая отсечка шторма — не более
        # per_turn_cap погодных запросов за ОДИН ход, ДО geocode (экономим и
        # 8-секундные таймауты лежачего Open-Meteo, и время hot-path). #211-паттерн.
        if per_turn_cap > 0:
            with _turn_weather_lock:
                _turn_weather_calls[0] += 1
                over_cap = _turn_weather_calls[0] > per_turn_cap
            if over_cap:
                return (
                    "error:weather_turn_limit: достигнут предел погодных запросов "
                    "за один ответ — ответь тем, что уже получено; новые запросы "
                    "погоды в этом ходе не выполняются"
                )

        # Granularity normalization (fail-loud — explicit error для unknown).
        canonical_granularity = _normalize_granularity(granularity)
        if canonical_granularity is None:
            return (
                f"error: unsupported granularity: {granularity!r}. "
                "Use daily / part_of_day / hourly."
            )

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

        # Resolve location-local timezone (with fallback chain).
        location_tz, request_tz_name = _resolve_timezone(tz)
        now_local = _datetime.now(tz=location_tz)

        # Compute hourly window (used for params + post-filter).
        request_start_hour: str | None = None
        request_end_hour: str | None = None
        start_local: _datetime | None = None
        effective_end_local: _datetime | None = None
        truncated = False
        raw_end_local: _datetime | None = None
        if canonical_granularity in ("hourly", "part_of_day"):
            (
                start_local, effective_end_local,
                request_start_hour, request_end_hour,
                truncated, raw_end_local,
            ) = _compute_hourly_window(
                now_local, location_tz, canonical_granularity,
                day_offset, days_count,
            )

        forecast_total_days = day_offset + days_count
        data = _fetch_forecast(
            lat=lat, lon=lon,
            request_tz_name=request_tz_name,
            granularity=canonical_granularity,
            forecast_days=forecast_total_days,
            request_start_hour=request_start_hour,
            request_end_hour=request_end_hour,
        )
        if data is None:
            return "error: сервис погоды не отвечает, попробуй позже"

        if canonical_granularity == "daily":
            return _render_daily(data, day_offset, days_count, display)
        elif canonical_granularity == "part_of_day":
            return _render_part_of_day(
                data, display, location_tz, start_local, effective_end_local,
            )
        else:  # hourly
            return _render_hourly(
                data, display, location_tz,
                start_local, effective_end_local, raw_end_local, truncated,
            )

    return get_weather


def _render_current(
    current: dict | None, sunrise: object = None, sunset: object = None,
) -> str | None:
    """#176: строка текущих условий «Сейчас: ☀️ +20°, ощущается +18°, ветер 10 м/с (порывы 31),
    влажность 49%, давление 760 мм. Светло 03:44–21:17». None если блока нет / нет температуры."""
    if not current:
        return None
    try:
        t = current.get("temperature_2m")
        if t is None:
            return None
        t = float(t)
        code = current.get("weather_code")
        emoji = _WEATHER_EMOJI.get(int(code), "·") if code is not None else None
        prefix = f"{emoji} " if emoji else ""  # без кода погоды — без пустого значка
        parts = [f"Сейчас: {prefix}{_format_signed_temp(t)}"]
        feels = current.get("apparent_temperature")
        if feels is not None and abs(float(feels) - t) >= 2:
            parts.append(f"ощущается {_format_signed_temp(float(feels))}")
        wind = current.get("wind_speed_10m")
        if wind is not None:
            w = f"ветер {round(float(wind))} м/с"
            gust = current.get("wind_gusts_10m")
            if gust is not None and float(gust) >= 12:
                w += f" (порывы {round(float(gust))})"
            parts.append(w)
        hum = current.get("relative_humidity_2m")
        if hum is not None:
            parts.append(f"влажность {int(round(float(hum)))}%")
        pressure = _safe_float(current.get("pressure_msl"))  # #176 хвост: давление в мм рт.ст.
        if pressure is not None:
            parts.append(f"давление {round(pressure * _HPA_TO_MMHG)} мм")
        line = ", ".join(parts)
        sr, ss = _hhmm(sunrise), _hhmm(sunset)  # #176 хвост: светлое время
        if sr and ss:
            line += f". Светло {sr}–{ss}"
        return line
    except (ValueError, TypeError):
        return None


def _render_daily(
    data: dict, day_offset: int, days_count: int, display: str,
) -> str:
    """Original F1 format: per-day lines. #176: + вероятность/ощущается/порывы; для сегодня —
    строка текущих условий (только когда поля/блок current пришли в ответе)."""
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    codes = daily.get("weathercode") or []
    precip = daily.get("precipitation_sum") or []
    fmax = daily.get("apparent_temperature_max") or []
    fmin = daily.get("apparent_temperature_min") or []
    probs = daily.get("precipitation_probability_max") or []
    gusts = daily.get("wind_gusts_10m_max") or []
    uvs = daily.get("uv_index_max") or []                 # #176 хвост
    sunrises = daily.get("sunrise") or []                 # #176 хвост
    sunsets = daily.get("sunset") or []                   # #176 хвост

    if not dates:
        return "error: пустой прогноз"

    def _at(arr: list, i: int) -> float | None:
        return _safe_float(arr[i]) if i < len(arr) else None

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
                feels_max=_at(fmax, i), feels_min=_at(fmin, i),
                prob=_at(probs, i), gust=_at(gusts, i), uv=_at(uvs, i),
            )
        except (ValueError, TypeError, IndexError):
            continue
        lines.append(line)

    if not lines:
        return "error: не удалось распарсить прогноз"
    body = f"{display}:\n" + "\n".join(lines)
    # #176: для сегодня — текущие условия первой строкой (если блок current пришёл);
    # рассвет/закат берём из daily текущего дня (day_offset).
    cur = None
    if day_offset == 0:
        # daily-массивы начинаются с СЕГОДНЯ → индекс [0] = сегодня (валидно только в этой ветке
        # day_offset==0; если когда-то покажем светлое время для др. дней — индекс пересчитать).
        sr = sunrises[0] if sunrises else None
        ss = sunsets[0] if sunsets else None
        cur = _render_current(data.get("current"), sunrise=sr, sunset=ss)
    return f"{cur}\n{body}" if cur else body


def _render_part_of_day(
    data: dict,
    display: str,
    location_tz: _tzinfo,
    start_local: _datetime | None,
    end_local: _datetime | None,
) -> str:
    """Group hourly into 4 buckets/day, render one line per bucket."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weathercode") or []
    precips = hourly.get("precipitation") or []
    winds = hourly.get("windspeed_10m") or []

    if not times:
        return "error: пустой прогноз"

    # Defensive post-filter (Codex r3 Alternative — guardrail vs API quirks/DST).
    # Code-reviewer MEDIUM: append к filtered массивам атомарно — иначе если API
    # вернёт mismatched-length arrays, downstream `_bucket_hours_by_part_of_day`
    # будет misalign индексы. Open-Meteo контракт гарантирует equal-length, но
    # belt-and-suspenders: skip всю строку если хоть один массив short.
    if start_local is not None and end_local is not None:
        f_times, f_temps, f_codes, f_precips, f_winds = [], [], [], [], []
        for i, t_iso in enumerate(times):
            try:
                dt_naive = _datetime.fromisoformat(t_iso)
                dt = dt_naive.replace(tzinfo=location_tz)
            except (ValueError, TypeError):
                continue
            if not (start_local <= dt <= end_local):
                continue
            if not (i < len(temps) and i < len(codes)
                    and i < len(precips) and i < len(winds)):
                # Malformed API response — drop row рather than misalign downstream.
                continue
            f_times.append(t_iso)
            f_temps.append(temps[i])
            f_codes.append(codes[i])
            f_precips.append(precips[i])
            f_winds.append(winds[i])
        times, temps, codes, precips, winds = f_times, f_temps, f_codes, f_precips, f_winds

    if not times:
        return "error: пустой прогноз"

    buckets = _bucket_hours_by_part_of_day(times, temps, codes, precips, winds)
    if not buckets:
        return "error: не удалось распарсить прогноз"

    # Multi-day: group by date with per-date headers; single-day: one header.
    out: list[str] = []
    current_date: _date | None = None
    for day, label, code, tavg, precip_sum, wind_avg in buckets:
        if day != current_date:
            if current_date is not None:
                out.append("")  # blank between days
            out.append(f"{display}, {_format_day_header(day)}:")
            current_date = day
        out.append(_format_part_of_day_line(label, code, tavg, precip_sum, wind_avg))

    return "\n".join(out)


def _render_hourly(
    data: dict,
    display: str,
    location_tz: _tzinfo,
    start_local: _datetime | None,
    effective_end_local: _datetime | None,
    raw_end_local: _datetime | None,
    truncated: bool,
) -> str:
    """One line per hour, grouped by date."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weathercode") or []
    precips = hourly.get("precipitation") or []
    winds = hourly.get("windspeed_10m") or []
    probs = hourly.get("precipitation_probability") or []  # #176
    gusts = hourly.get("wind_gusts_10m") or []             # #176

    if not times:
        return "error: пустой почасовой прогноз"

    out: list[str] = [f"{display}:"]
    current_date: _date | None = None
    rendered = 0
    for i, t_iso in enumerate(times):
        try:
            dt_naive = _datetime.fromisoformat(t_iso)
            dt = dt_naive.replace(tzinfo=location_tz)
        except (ValueError, TypeError):
            continue
        # Defensive post-filter (Codex r3 Alternative — guardrail).
        if start_local is not None and effective_end_local is not None:
            if not (start_local <= dt <= effective_end_local):
                continue

        if dt.date() != current_date:
            out.append(f"{_format_day_header(dt.date())}:")
            current_date = dt.date()

        try:
            line = _format_hour_line(
                dt,
                code=int(codes[i]) if i < len(codes) else -1,
                temp=float(temps[i]) if i < len(temps) else 0.0,
                precip_mm=float(precips[i]) if i < len(precips) else 0.0,
                wind_ms=float(winds[i]) if i < len(winds) else 0.0,
                prob=_safe_float(probs[i]) if i < len(probs) else None,
                gust=_safe_float(gusts[i]) if i < len(gusts) else None,
            )
        except (ValueError, TypeError, IndexError):
            continue
        out.append(line)
        rendered += 1

    if rendered == 0:
        return "error: пустой почасовой прогноз"

    if truncated and raw_end_local is not None:
        out.append(f"…(показаны первые {_HOURLY_CAP_HOURS} часов)")

    return "\n".join(out)
