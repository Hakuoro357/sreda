"""Тесты для weather_tool — Open-Meteo backed forecast."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime as _datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sreda.services import weather_tool as wt


def _R(payload):
    """Build a fake httpx response with .raise_for_status() and .json()."""
    return type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self, _p=payload: _p,
    })()


def _mk_geo_response(name: str, lat: float, lon: float, tz: str = "Europe/Moscow"):
    """Имитирует ответ Open-Meteo geocoding."""
    return {
        "results": [
            {
                "id": 1, "name": name, "latitude": lat, "longitude": lon,
                "elevation": 200.0, "country": "Russia", "timezone": tz,
            }
        ]
    }


def _mk_forecast_response(
    dates: list[str], tmax: list[float], tmin: list[float],
    precip: list[float], codes: list[int],
):
    return {
        "daily": {
            "time": dates,
            "temperature_2m_max": tmax,
            "temperature_2m_min": tmin,
            "precipitation_sum": precip,
            "weathercode": codes,
        }
    }


def _mk_forecast_response_with_hourly(
    *,
    daily_dates: list[str] | None = None,
    daily_tmax: list[float] | None = None,
    daily_tmin: list[float] | None = None,
    daily_precip: list[float] | None = None,
    daily_codes: list[int] | None = None,
    hourly_times: list[str],
    hourly_temps: list[float],
    hourly_codes: list[int],
    hourly_precips: list[float],
    hourly_winds: list[float],
):
    """Открытый builder для hourly fixture, с минимальным дневным фоллбеком."""
    return {
        "daily": {
            "time": daily_dates or [],
            "temperature_2m_max": daily_tmax or [],
            "temperature_2m_min": daily_tmin or [],
            "precipitation_sum": daily_precip or [],
            "weathercode": daily_codes or [],
        },
        "hourly": {
            "time": hourly_times,
            "temperature_2m": hourly_temps,
            "weathercode": hourly_codes,
            "precipitation": hourly_precips,
            "windspeed_10m": hourly_winds,
        },
    }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def test_format_day_line_clear_no_precip():
    line = wt._format_day_line(
        _date(2026, 4, 29), code=0, tmax=12.0, tmin=4.0, precip_mm=0.0,
    )
    assert "ср 29 апр" in line
    assert "+12°" in line
    assert "+4°" in line
    assert "без осадков" in line
    assert "☀️" in line


def test_format_day_line_rain_with_precip():
    line = wt._format_day_line(
        _date(2026, 4, 30), code=63, tmax=6.0, tmin=-1.0, precip_mm=3.1,
    )
    assert "🌧️" in line
    assert "+6°" in line
    assert "-1°" in line
    assert "3.1мм" in line


def test_format_day_line_unknown_weathercode_falls_back():
    line = wt._format_day_line(
        _date(2026, 5, 1), code=999, tmax=10.0, tmin=5.0, precip_mm=0.0,
    )
    # Unknown code → fallback symbol but не падаем
    assert "+10°" in line


# ----------------------------------------------------------------------
# Tool — full flow with mocked HTTP
# ----------------------------------------------------------------------


def test_get_weather_today_single_day():
    """Happy path: get_weather('Сходня') возвращает forecast на сегодня."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Сходня", 55.95, 37.30)
    fc = _mk_forecast_response(
        dates=["2026-04-29"],
        tmax=[5.0], tmin=[1.0], precip=[0.0], codes=[2],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [
            type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: geo,
            })(),
            type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: fc,
            })(),
        ]
        result = tool.invoke({"location": "Сходня"})

    assert "Сходня:" in result
    assert "+5°" in result
    assert "ср 29 апр" in result


def test_get_weather_uses_cached_geocode_on_repeat():
    """Второй вызов того же города не должен повторять geocoding API."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-04-29", "2026-04-30"],
        tmax=[5.0, 7.0], tmin=[1.0, 0.0], precip=[0.0, 0.5], codes=[2, 63],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        # 1st call: geocode + forecast = 2 GETs
        # 2nd call: только forecast = 1 GET
        mock_get.side_effect = [
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: geo})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
        ]
        tool.invoke({"location": "Москва", "days_count": 2})
        tool.invoke({"location": "Москва", "days_count": 2})
    # 2 + 1 = 3 calls (НЕ 4 — геокодинг закэширован)
    assert mock_get.call_count == 3


def test_get_weather_geocode_normalisation_case_insensitive():
    """Cache hit для 'москва' / 'МОСКВА' / 'Москва' — один и тот же."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-04-29"], tmax=[5.0], tmin=[1.0], precip=[0.0], codes=[0],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: geo})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
        ]
        tool.invoke({"location": "Москва"})
        tool.invoke({"location": "москва"})  # lowercase
        tool.invoke({"location": "  МОСКВА  "})  # uppercase + spaces
    # 1 geocode + 3 forecasts = 4 calls
    assert mock_get.call_count == 4


def test_get_weather_unknown_city_returns_error():
    wt._GEO_CACHE.clear()
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.return_value = type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"results": []},
        })()
        result = tool.invoke({"location": "NoSuchCity_XYZ"})
    assert result.startswith("error:")
    assert "не нашла" in result


def test_get_weather_empty_location_returns_error():
    tool = wt.build_weather_tool()
    assert tool.invoke({"location": "  "}).startswith("error:")


def test_get_weather_clamps_oob_offsets():
    """day_offset > MAX → clip to MAX-1; days_count > available → clip."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-04-29", "2026-04-30"],
        tmax=[5.0, 6.0], tmin=[1.0, 0.0], precip=[0.0, 0.0], codes=[0, 0],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: geo})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
        ]
        # day_offset=999, days_count=999 — должно clip'нуться
        result = tool.invoke({
            "location": "Москва", "day_offset": 999, "days_count": 999,
        })
    # Tool не упал, результат — что-то осмысленное (либо forecast,
    # либо error если запросили offset за пределами).
    assert "error:" in result or "Москва:" in result


def test_render_daily_includes_probability_feels_gusts_176():
    """#176: дневной рендер выдаёт вероятность осадков, ощущается, порывы — когда поля есть."""
    data = {
        "daily": {
            "time": ["2026-06-19"],
            "temperature_2m_max": [21.0], "temperature_2m_min": [14.0],
            "precipitation_sum": [9.0], "weathercode": [63],
            "apparent_temperature_max": [18.0], "apparent_temperature_min": [10.0],
            "precipitation_probability_max": [63], "wind_gusts_10m_max": [32.0],
        }
    }
    out = wt._render_daily(data, day_offset=0, days_count=1, display="Москва")
    assert "63%" in out, out            # вероятность осадков
    assert "ощущ" in out.lower(), out   # ощущается
    assert "32" in out, out             # порывы


def test_render_daily_today_prepends_current_176():
    """#176: для сегодня (day_offset=0) при наличии current — строка текущих условий."""
    data = {
        "daily": {"time": ["2026-06-19"], "temperature_2m_max": [21.0],
                  "temperature_2m_min": [14.0], "precipitation_sum": [0.0], "weathercode": [3]},
        "current": {"temperature_2m": 20.1, "apparent_temperature": 16.0,
                    "weather_code": 3, "wind_speed_10m": 10.2, "wind_gusts_10m": 31.3,
                    "relative_humidity_2m": 49, "precipitation": 0.0},
    }
    out = wt._render_daily(data, day_offset=0, days_count=1, display="Москва")
    assert "ейчас" in out, out   # «Сейчас:»
    assert "16" in out, out      # ощущается +16 (отличается от +20 → показано)


def test_render_daily_no_current_unchanged_176():
    """#176 регресс: без current/новых полей — прежний формат (заголовок первой строкой)."""
    data = {"daily": {"time": ["2026-04-29"], "temperature_2m_max": [5.0],
                      "temperature_2m_min": [1.0], "precipitation_sum": [0.0], "weathercode": [0]}}
    out = wt._render_daily(data, day_offset=0, days_count=1, display="Москва")
    assert out.split("\n")[0] == "Москва:", out
    assert "Сейчас" not in out, out


def test_render_hourly_includes_probability_176():
    """#176: почасовой рендер показывает вероятность осадков, когда поле есть."""
    data = {"hourly": {
        "time": ["2026-06-19T15:00"], "temperature_2m": [19.0], "weathercode": [61],
        "precipitation": [1.2], "windspeed_10m": [4.0],
        "precipitation_probability": [70], "wind_gusts_10m": [9.0],
    }}
    tz = ZoneInfo("Europe/Moscow")
    start = _datetime(2026, 6, 19, 0, 0, tzinfo=tz)
    end = _datetime(2026, 6, 19, 23, 0, tzinfo=tz)
    out = wt._render_hourly(data, "Москва", tz, start, end, end, False)
    assert "70%" in out, out


def test_get_weather_multi_day_format():
    """7-day forecast форматируется одной строкой на день."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-04-29", "2026-04-30", "2026-05-01"],
        tmax=[5.0, 6.0, 7.0],
        tmin=[1.0, 0.0, -1.0],
        precip=[0.0, 0.5, 1.2],
        codes=[0, 2, 63],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: geo})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
        ]
        result = tool.invoke({"location": "Москва", "days_count": 3})
    lines = [ln for ln in result.split("\n") if ln.strip()]
    # 1 header (Москва:) + 3 day lines = 4 строк
    assert len(lines) == 4
    assert lines[0] == "Москва:"


def test_get_weather_day_offset_skips_past_days():
    """day_offset=2 → пропустить первые 2 элемента из daily.time."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-04-29", "2026-04-30", "2026-05-01"],
        tmax=[5.0, 6.0, 7.0],
        tmin=[1.0, 0.0, -1.0],
        precip=[0.0, 0.0, 0.0],
        codes=[0, 0, 0],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: geo})(),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: fc})(),
        ]
        result = tool.invoke({
            "location": "Москва", "day_offset": 2, "days_count": 1,
        })
    assert "1 мая" in result
    # 29 апр и 30 апр НЕ должны попасть в результат
    assert "29 апр" not in result
    assert "30 апр" not in result


# ======================================================================
# Phase B: hourly + part_of_day granularity tests (Codex r1-r6 reviewed).
# ======================================================================


# ----------------------------------------------------------------------
# Helpers — pure functions
# ----------------------------------------------------------------------


def test_normalize_granularity_canonical():
    assert wt._normalize_granularity("daily") == "daily"
    assert wt._normalize_granularity("hourly") == "hourly"
    assert wt._normalize_granularity("part_of_day") == "part_of_day"


def test_normalize_granularity_alias_hour_to_hourly():
    """LLM может прислать 'hour' вместо 'hourly' — должно нормализоваться."""
    assert wt._normalize_granularity("hour") == "hourly"
    assert wt._normalize_granularity("hours") == "hourly"
    assert wt._normalize_granularity("почасовой") == "hourly"


def test_normalize_granularity_unknown_returns_none():
    """Codex r1 MAJOR: silent fallback опасен. Unknown → None → caller возвращает explicit error."""
    assert wt._normalize_granularity("bogus") is None
    assert wt._normalize_granularity("") is None
    assert wt._normalize_granularity(None) is None


def test_resolve_timezone_valid_returns_tuple():
    tz, name = wt._resolve_timezone("Asia/Tokyo")
    assert tz.utcoffset(_datetime(2026, 5, 8)).total_seconds() == 9 * 3600
    assert name == "Asia/Tokyo"


def test_resolve_timezone_invalid_falls_back_to_moscow():
    """Codex r3 MAJOR — fallback chain. r4 MAJOR — canonical name returned."""
    tz, name = wt._resolve_timezone("Junk/Bogus")
    # Should fall back to Europe/Moscow (or UTC if Moscow not available)
    assert name in ("Europe/Moscow", "UTC")
    assert tz is not None


def test_resolve_timezone_none_falls_back():
    tz, name = wt._resolve_timezone(None)
    assert name in ("Europe/Moscow", "UTC")


def test_dominant_weather_code_max_precip_wins():
    """When precip > 0.1 in any hour, code of max-precip hour wins."""
    codes = [0, 0, 63, 63, 63, 0]    # rain hours = indexes 2-4
    precips = [0.0, 0.0, 1.0, 2.0, 1.5, 0.0]
    assert wt._dominant_weather_code(codes, precips) == 63


def test_dominant_weather_code_dry_severity_ranked():
    """Codex r1 MAJOR — dry bucket: severity-ranked, не первый час."""
    codes = [0, 0, 2, 3, 3, 0]       # mostly clear → overcast
    precips = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    # Severity rank: 3 (overcast) > 2 (partly cloudy) > 0 (clear)
    assert wt._dominant_weather_code(codes, precips) == 3


def test_dominant_weather_code_empty():
    assert wt._dominant_weather_code([], []) == 0


# ----------------------------------------------------------------------
# _compute_hourly_window — single source of bounds
# ----------------------------------------------------------------------


def test_compute_hourly_window_today_starts_at_now_hour():
    """Codex r2 MAJOR: today hourly = remaining hours, не next 48h."""
    location_tz = ZoneInfo("Europe/Moscow")
    now_local = _datetime(2026, 5, 8, 14, 30, tzinfo=location_tz)
    start, end_eff, req_start, req_end, truncated, raw_end = wt._compute_hourly_window(
        now_local, location_tz, "hourly", day_offset=0, days_count=1,
    )
    assert start.hour == 14 and start.minute == 0
    assert start.date() == _date(2026, 5, 8)
    assert end_eff.date() == _date(2026, 5, 8)
    assert end_eff.hour == 23
    assert truncated is False
    assert req_start == "2026-05-08T14:00"
    assert req_end == "2026-05-08T23:00"


def test_compute_hourly_window_future_uses_full_day():
    """day_offset=2 → start=00:00 целевого дня."""
    location_tz = ZoneInfo("Europe/Moscow")
    now_local = _datetime(2026, 5, 8, 14, 30, tzinfo=location_tz)
    start, end_eff, req_start, req_end, truncated, _ = wt._compute_hourly_window(
        now_local, location_tz, "hourly", day_offset=2, days_count=1,
    )
    assert start == _datetime(2026, 5, 10, 0, 0, tzinfo=location_tz)
    assert end_eff.date() == _date(2026, 5, 10)
    assert end_eff.hour == 23
    assert truncated is False
    assert req_start == "2026-05-10T00:00"
    assert req_end == "2026-05-10T23:00"


def test_compute_hourly_window_multi_day_today_starts_at_now():
    """Codex r3 MAJOR: hourly + day_offset=0 + days_count>1 → start=now (НЕ 00:00)."""
    location_tz = ZoneInfo("Europe/Moscow")
    now_local = _datetime(2026, 5, 8, 14, 30, tzinfo=location_tz)
    start, end_eff, req_start, req_end, truncated, _ = wt._compute_hourly_window(
        now_local, location_tz, "hourly", day_offset=0, days_count=2,
    )
    assert start.hour == 14
    assert start.date() == _date(2026, 5, 8)
    # End = end of tomorrow (within 48h cap)
    assert end_eff.date() == _date(2026, 5, 9)
    assert end_eff.hour == 23
    assert truncated is False  # 14:00→T+1 23:00 = 34h, < 48h


def test_compute_hourly_window_multi_day_caps_at_48h():
    """days_count=7 → effective_end_local capped at start+47h, truncated=True."""
    location_tz = ZoneInfo("Europe/Moscow")
    now_local = _datetime(2026, 5, 8, 14, 0, tzinfo=location_tz)
    start, end_eff, req_start, req_end, truncated, raw_end = wt._compute_hourly_window(
        now_local, location_tz, "hourly", day_offset=0, days_count=7,
    )
    assert truncated is True
    delta_hours = int((end_eff - start).total_seconds() // 3600)
    assert delta_hours == 47  # 47h offset = 48 inclusive entries
    # raw_end_local — uncapped, до конца 7-го дня
    assert raw_end > end_eff


def test_compute_hourly_window_part_of_day_starts_at_midnight():
    """part_of_day всегда начинается с 00:00 (для bucket'инга всего дня)."""
    location_tz = ZoneInfo("Europe/Moscow")
    now_local = _datetime(2026, 5, 8, 14, 30, tzinfo=location_tz)
    start, end_eff, req_start, req_end, truncated, _ = wt._compute_hourly_window(
        now_local, location_tz, "part_of_day", day_offset=0, days_count=1,
    )
    assert start == _datetime(2026, 5, 8, 0, 0, tzinfo=location_tz)
    assert end_eff.hour == 23
    assert truncated is False


def test_compute_hourly_window_uses_location_local_tz():
    """Codex r2 MAJOR: window вычисляется в location_tz, не server tz."""
    tokyo = ZoneInfo("Asia/Tokyo")
    now_tokyo = _datetime(2026, 5, 8, 14, 30, tzinfo=tokyo)
    start, _, req_start, _, _, _ = wt._compute_hourly_window(
        now_tokyo, tokyo, "hourly", day_offset=0, days_count=1,
    )
    # start_hour должен отражать TOKYO time, не UTC
    assert req_start == "2026-05-08T14:00"
    assert start.tzinfo == tokyo


# ----------------------------------------------------------------------
# Tool integration — granularity branches via mocked HTTP
# ----------------------------------------------------------------------


def _mk_24h_hourly(date_str: str, base_temp: float = 5.0):
    """24-hour hourly fixture для одного дня."""
    times = [f"{date_str}T{h:02d}:00" for h in range(24)]
    temps = [base_temp + (h - 12) * 0.5 for h in range(24)]
    codes = [0] * 24
    precips = [0.0] * 24
    winds = [3.0] * 24
    return times, temps, codes, precips, winds


def test_get_weather_unknown_granularity_returns_explicit_error():
    """Codex MAJOR: unknown granularity — fail-loud, не silent fallback."""
    tool = wt.build_weather_tool()
    result = tool.invoke({"location": "Москва", "granularity": "bogus"})
    assert result.startswith("error: unsupported granularity")
    assert "bogus" in result
    assert "daily" in result and "hourly" in result and "part_of_day" in result


def test_daily_mode_does_not_request_hourly():
    """Backward compat: daily не должен слать hourly/start_hour/end_hour."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-05-08"], tmax=[10.0], tmin=[3.0],
        precip=[0.0], codes=[0],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        tool.invoke({"location": "Москва"})
    # Find the forecast call (2nd). Inspect params.
    forecast_call = mock_get.call_args_list[1]
    params = forecast_call.kwargs.get("params") or forecast_call.args[1] if len(forecast_call.args) > 1 else forecast_call.kwargs.get("params")
    assert "hourly" not in params
    assert "start_hour" not in params
    assert "end_hour" not in params
    assert params.get("forecast_days") == 1


def test_hourly_mode_includes_hourly_vars_and_window():
    """Codex r2 CRITICAL: hourly param обязателен. Plus start_hour/end_hour set."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    times, temps, codes, precips, winds = _mk_24h_hourly("2026-05-08")
    fc = _mk_forecast_response_with_hourly(
        hourly_times=times, hourly_temps=temps, hourly_codes=codes,
        hourly_precips=precips, hourly_winds=winds,
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        tool.invoke({"location": "Москва", "granularity": "hourly"})
    forecast_call = mock_get.call_args_list[1]
    params = forecast_call.kwargs.get("params")
    assert "hourly" in params
    assert "temperature_2m" in params["hourly"]
    assert "windspeed_10m" in params["hourly"]
    assert "start_hour" in params
    assert "end_hour" in params
    # Phase 0 finding: forecast_days НЕ должен быть для hourly
    assert "forecast_days" not in params


def test_wind_speed_unit_set_to_ms():
    """Codex r1 MAJOR: API default = km/h, явно set'им ms."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response(
        dates=["2026-05-08"], tmax=[10.0], tmin=[3.0],
        precip=[0.0], codes=[0],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        tool.invoke({"location": "Москва"})
    forecast_call = mock_get.call_args_list[1]
    params = forecast_call.kwargs.get("params")
    assert params.get("wind_speed_unit") == "ms"


def test_part_of_day_groups_into_4_buckets_with_wind():
    """24h fixture → 4 bucket-строки, каждая содержит «м/с»."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    # #103 (stale-test fix): use TODAY dynamically. part_of_day with the
    # default day_offset=0 windows 00:00..23:00 of now_local.date(); a
    # hardcoded past date (was "2026-05-08") gets dropped by the tool's
    # defensive post-filter → "error: пустой прогноз".
    today = _datetime.now(tz=ZoneInfo("Europe/Moscow")).date()
    times, temps, codes, precips, winds = _mk_24h_hourly(today.isoformat())
    fc = _mk_forecast_response_with_hourly(
        hourly_times=times, hourly_temps=temps, hourly_codes=codes,
        hourly_precips=precips, hourly_winds=winds,
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        result = tool.invoke({
            "location": "Москва", "granularity": "part_of_day",
        })
    # Codex r2 MINOR: wind в каждой строке
    assert result.count("м/с") >= 4
    for label in ("ночь", "утро", "день", "вечер"):
        assert label in result


def test_hourly_returns_hour_lines():
    """granularity=hourly, 24h fixture для tomorrow → строки с HH:00 + м/с."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    # Use tomorrow's date dynamically — tool computes start_local from now().
    tomorrow = (_datetime.now(tz=ZoneInfo("Europe/Moscow")) + _timedelta_days(1)).date()
    times, temps, codes, precips, winds = _mk_24h_hourly(tomorrow.isoformat())
    fc = _mk_forecast_response_with_hourly(
        hourly_times=times, hourly_temps=temps, hourly_codes=codes,
        hourly_precips=precips, hourly_winds=winds,
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        result = tool.invoke({
            "location": "Москва", "granularity": "hourly",
            "day_offset": 1, "days_count": 1,  # tomorrow → no past-hour filter issue
        })
    # Tomorrow at 09:00 should be in output (24h fixture covers full day).
    assert "09:00" in result
    assert "м/с" in result


def test_hourly_caps_at_48_with_truncation_marker():
    """168h fixture → ≤48 hour rows + truncation marker."""
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    # #103 (stale-test fix): base the 7-day fixture on TOMORROW dynamically
    # (day_offset=1). A hardcoded past start date (was 2026-05-08) is dropped
    # by the tool's defensive hour-window post-filter → "error: пустой прогноз".
    base_day = (_datetime.now(tz=ZoneInfo("Europe/Moscow")) + _timedelta_days(1)).date()
    # 168 hours over 7 days
    times, temps, codes, precips, winds = [], [], [], [], []
    for day_offset in range(7):
        d = base_day + _timedelta_days(day_offset)
        ts, te, tc, tp, tw = _mk_24h_hourly(d.isoformat())
        times += ts
        temps += te
        codes += tc
        precips += tp
        winds += tw
    fc = _mk_forecast_response_with_hourly(
        hourly_times=times, hourly_temps=temps, hourly_codes=codes,
        hourly_precips=precips, hourly_winds=winds,
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        result = tool.invoke({
            "location": "Москва", "granularity": "hourly",
            "day_offset": 1, "days_count": 7,
        })
    assert "показаны первые 48" in result
    # Count hour rows (each starts with "  " indent + emoji + HH:00)
    hour_rows = [ln for ln in result.split("\n") if " " in ln and ":00" in ln and ln.startswith("  ")]
    assert len(hour_rows) <= 48


def _timedelta_days(n: int):
    """Helper for date arithmetic in tests."""
    from datetime import timedelta
    return timedelta(days=n)


def test_hourly_empty_array_returns_error():
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    fc = _mk_forecast_response_with_hourly(
        hourly_times=[], hourly_temps=[], hourly_codes=[],
        hourly_precips=[], hourly_winds=[],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo), _R(fc)]
        result = tool.invoke({"location": "Москва", "granularity": "hourly"})
    assert result.startswith("error:")
    assert "почасовой" in result.lower() or "пустой" in result.lower()


def test_invalid_geocode_timezone_falls_back_to_request_too():
    """Codex r4 MAJOR: geocode tz='Junk/Bogus' → params['timezone'] = 'Europe/Moscow' (или UTC)."""
    wt._GEO_CACHE.clear()
    # geocode возвращает мусорный tz
    geo_bad = {
        "results": [{
            "id": 1, "name": "X", "latitude": 0.0, "longitude": 0.0,
            "elevation": 0.0, "country": "Z", "timezone": "Junk/Bogus",
        }]
    }
    fc = _mk_forecast_response(
        dates=["2026-05-08"], tmax=[10.0], tmin=[3.0],
        precip=[0.0], codes=[0],
    )
    tool = wt.build_weather_tool()
    with patch.object(wt.httpx, "get") as mock_get:
        mock_get.side_effect = [_R(geo_bad), _R(fc)]
        result = tool.invoke({"location": "X"})
    # Не упал
    assert not result.startswith("error: ZoneInfo")
    forecast_call = mock_get.call_args_list[1]
    params = forecast_call.kwargs.get("params")
    # Canonical fallback name
    assert params.get("timezone") in ("Europe/Moscow", "UTC")


def test_geocode_cache_lock_no_deadlock():
    """Concurrent _geocode для same city — оба возвращают cached tuple, deadlock'а нет.

    НЕ assert exact HTTP count — pattern допускает 2 calls на concurrent miss
    (Codex r1 MAJOR — singleflight оставлен за рамками v1).
    """
    wt._GEO_CACHE.clear()
    geo = _mk_geo_response("Москва", 55.75, 37.62)
    with patch.object(wt.httpx, "get", return_value=_R(geo)):
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(wt._geocode, "Москва")
            f2 = ex.submit(wt._geocode, "Москва")
            r1 = f1.result(timeout=5)
            r2 = f2.result(timeout=5)
    assert r1 == r2
    assert r1 is not None
    # cache содержит one entry
    assert "москва" in wt._GEO_CACHE


# ----------------------------------------------------------------------
# System prompt anchoring — Codex CRITICAL
# ----------------------------------------------------------------------


def test_system_prompt_mentions_get_weather_granularity():
    """Codex CRITICAL: handlers.py system prompt должен учить LLM про granularity."""
    from sreda.runtime import handlers
    prompt = handlers._CORE_SYSTEM_PROMPT
    assert "get_weather" in prompt
    assert "granularity" in prompt
    # Должны быть хотя бы 2 из 3 intent mappings
    intent_mappings = sum([
        "hourly" in prompt and ("во сколько" in prompt or "почасов" in prompt.lower()),
        "part_of_day" in prompt and ("утро" in prompt.lower() or "одеться" in prompt.lower()),
        "daily" in prompt,
    ])
    assert intent_mappings >= 2
