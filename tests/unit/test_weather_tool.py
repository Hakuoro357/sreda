"""Тесты для weather_tool — Open-Meteo backed forecast."""

from __future__ import annotations

from datetime import date as _date
from unittest.mock import patch

from sreda.services import weather_tool as wt


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
