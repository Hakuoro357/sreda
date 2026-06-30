"""Integration tests for the final 4 families closing Sub-A4 to 55/55:
- memory (3 tools): save_core_fact, save_episode, recall_memory
- utility (1 tool): log_unsupported_request
- ui (1 tool): reply_with_buttons
- web (3 tools): get_weather, web_search, fetch_url

R2 fixes after Codex R1 CRITICAL/MAJOR/MEDIUM:
- web tool names match @lc_tool runtime (get_weather/web_search/fetch_url,
  NOT *_tool builder factory names) — see specs_web.py header
- WeatherToolInput exposes day_offset/days_count/granularity per
  runtime weather_tool.py:502 signature
- FetchUrl input validator mirrors runtime SSRF guard (localhost /
  private / link-local rejection) per web_search_tool.py:73
- FetchUrlToolOk parses JSON envelope per web_search_tool.py:419
- Bridge test: every web ToolSpec.name maps to a real @lc_tool callable
"""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    FetchUrlToolOk,
    HousewifeToolError,
    LogUnsupportedRequestOk,
    PARSERS,
    RecallMemoryOk,
    ReplyWithButtonsOk,
    SaveCoreFactOk,
    SaveEpisodeOk,
    WeatherToolOk,
    WebSearchToolEmpty,
    WebSearchToolOk,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
)
from sreda.services.tool_schemas.specs_memory import (
    MEMORY_SPECS,
    RECALL_MEMORY_SPEC,
    RecallMemoryInput,
    SAVE_CORE_FACT_SPEC,
    SAVE_EPISODE_SPEC,
    SaveCoreFactInput,
    SaveEpisodeInput,
)
from sreda.services.tool_schemas.specs_ui import (
    REPLY_WITH_BUTTONS_SPEC,
    ReplyWithButtonsInput,
    UI_SPECS,
)
from sreda.services.tool_schemas.specs_utility import (
    LOG_UNSUPPORTED_REQUEST_SPEC,
    LogUnsupportedRequestInput,
    UTILITY_SPECS,
)
from sreda.services.tool_schemas.specs_web import (
    FETCH_URL_SPEC,
    FetchUrlToolInput,
    GET_WEATHER_SPEC,
    WEB_SEARCH_SPEC,
    WEB_SPECS,
    WeatherToolInput,
    WebSearchToolInput,
)


# ---------------------------------------------------------------------------
# Family-level invariants
# ---------------------------------------------------------------------------


def test_memory_specs_construct() -> None:
    assert len(MEMORY_SPECS) == 4
    assert {s.name for s in MEMORY_SPECS} == {
        "save_core_fact", "create_memory_category", "save_episode", "recall_memory",
    }


def test_utility_specs_construct() -> None:
    assert len(UTILITY_SPECS) == 1
    assert UTILITY_SPECS[0].name == "log_unsupported_request"


def test_ui_specs_construct() -> None:
    assert len(UI_SPECS) == 1
    assert UI_SPECS[0].name == "reply_with_buttons"


def test_web_specs_construct() -> None:
    """Codex R1 CRITICAL: names MUST match runtime @lc_tool callables,
    not the build_*_tool factory names."""
    assert len(WEB_SPECS) == 3
    assert {s.name for s in WEB_SPECS} == {
        "get_weather", "web_search", "fetch_url",
    }


def test_all_four_families_pass_quality_strict() -> None:
    assert_production_registry_quality(MEMORY_SPECS)
    assert_production_registry_quality(UTILITY_SPECS)
    assert_production_registry_quality(UI_SPECS)
    assert_production_registry_quality(WEB_SPECS)


def test_manifest_matches_specs() -> None:
    for family, expected in [
        ("memory", {"save_core_fact", "create_memory_category", "save_episode", "recall_memory"}),
        ("utility", {"log_unsupported_request"}),
        ("ui", {"reply_with_buttons"}),
        ("web", {"get_weather", "web_search", "fetch_url"}),
    ]:
        manifest = {
            name for name, fam in TOOL_FAMILY_MANIFEST.items() if fam == family
        }
        assert manifest == expected, f"{family}: {manifest} vs {expected}"


@pytest.mark.parametrize("spec", [
    *MEMORY_SPECS, *UTILITY_SPECS, *UI_SPECS, *WEB_SPECS,
], ids=lambda s: s.name)
def test_every_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS


# ---------------------------------------------------------------------------
# Bridge test: ToolSpec.name ↔ runtime @lc_tool callable name parity
# (Codex R1 CRITICAL — prevents future drift like weather_tool/get_weather)
# ---------------------------------------------------------------------------


def test_web_spec_names_match_runtime_lc_tool_callables() -> None:
    """Bridge test: each web ToolSpec.name must match the .name attr
    of the LangChain tool produced by the build_*_tool factory.

    Without this, dispatcher's ``tools_by_name.get(spec.name)`` returns
    None and typed planner calls hit ``error:unknown_tool`` at runtime
    despite all schemas/parsers being green."""
    from sreda.services.weather_tool import build_weather_tool
    from sreda.services.web_search_tool import (
        build_fetch_url_tool,
        build_web_search_tool,
    )

    weather = build_weather_tool()
    web_search = build_web_search_tool()
    fetch_url = build_fetch_url_tool()

    assert GET_WEATHER_SPEC.name == weather.name == "get_weather"
    assert WEB_SEARCH_SPEC.name == web_search.name == "web_search"
    assert FETCH_URL_SPEC.name == fetch_url.name == "fetch_url"


# ---------------------------------------------------------------------------
# Memory input + parser
# ---------------------------------------------------------------------------


def test_save_core_fact_input_real() -> None:
    parsed = SaveCoreFactInput.model_validate({"content": "у меня двое детей"})
    assert parsed.content == "у меня двое детей"


def test_save_core_fact_input_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        SaveCoreFactInput.model_validate({"content": "   "})


def test_save_core_fact_input_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        SaveCoreFactInput.model_validate({"content": "x", "extra": "y"})


def test_save_episode_input_real() -> None:
    parsed = SaveEpisodeInput.model_validate({"summary": "сегодня устала"})
    assert parsed.summary == "сегодня устала"


def test_recall_memory_input_defaults() -> None:
    parsed = RecallMemoryInput.model_validate({"query": "дети"})
    assert parsed.query == "дети"
    assert parsed.top_k == 3


def test_recall_memory_input_top_k_bounds() -> None:
    parsed = RecallMemoryInput.model_validate({"query": "x", "top_k": 10})
    assert parsed.top_k == 10
    with pytest.raises(ValidationError):
        RecallMemoryInput.model_validate({"query": "x", "top_k": 0})
    with pytest.raises(ValidationError):
        RecallMemoryInput.model_validate({"query": "x", "top_k": 11})


def test_save_core_fact_parser_ok() -> None:
    parsed = parse_tool_output("save_core_fact", "saved_core:abc123def")
    assert isinstance(parsed, SaveCoreFactOk)
    assert parsed.memory_id == "abc123def"


def test_save_core_fact_parser_empty_content_error() -> None:
    parsed = parse_tool_output("save_core_fact", "error: empty content")
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "empty_content"


def test_save_episode_parser_ok() -> None:
    parsed = parse_tool_output("save_episode", "saved_episode:xyz789")
    assert isinstance(parsed, SaveEpisodeOk)
    assert parsed.memory_id == "xyz789"


def test_recall_memory_parser_empty_results() -> None:
    parsed = parse_tool_output("recall_memory", "[]")
    assert isinstance(parsed, RecallMemoryOk)
    assert parsed.hits == []


def test_recall_memory_parser_with_hits() -> None:
    payload = json.dumps([
        {"content": "у меня двое детей", "source": "memory:core",
         "score": 0.85, "metadata": {}},
        {"content": "купить молоко", "source": "checklist:cl_xxx",
         "score": 0.72, "metadata": {"list_title": "Покупки"}},
    ], ensure_ascii=False)
    parsed = parse_tool_output("recall_memory", payload)
    assert isinstance(parsed, RecallMemoryOk)
    assert len(parsed.hits) == 2
    assert parsed.hits[0].score == 0.85
    assert parsed.hits[1].source == "checklist:cl_xxx"


def test_recall_memory_parser_rejects_non_list_json() -> None:
    parsed = parse_tool_output("recall_memory", '{"not": "a list"}')
    assert isinstance(parsed, ToolOutputContractViolation)


def test_recall_memory_parser_rejects_malformed_hit() -> None:
    parsed = parse_tool_output("recall_memory", '[{"missing_required_fields": true}]')
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Utility input + parser
# ---------------------------------------------------------------------------


def test_log_unsupported_request_input_real() -> None:
    parsed = LogUnsupportedRequestInput.model_validate({
        "user_asked": "закажи такси",
        "reason": "нет интеграции с такси-сервисом",
    })
    assert parsed.user_asked == "закажи такси"


def test_log_unsupported_request_input_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        LogUnsupportedRequestInput.model_validate({
            "user_asked": "", "reason": "x",
        })


def test_log_unsupported_request_parser_ok() -> None:
    parsed = parse_tool_output("log_unsupported_request", "ok:logged")
    assert isinstance(parsed, LogUnsupportedRequestOk)


def test_log_unsupported_request_parser_error() -> None:
    parsed = parse_tool_output(
        "log_unsupported_request",
        "error: both user_asked and reason required",
    )
    assert isinstance(parsed, HousewifeToolError)


# ---------------------------------------------------------------------------
# UI input + parser
# ---------------------------------------------------------------------------


def test_reply_with_buttons_input_real() -> None:
    parsed = ReplyWithButtonsInput.model_validate({
        "text": "Собрать меню?",
        "buttons": ["Да, собери", "Не сейчас"],
    })
    assert len(parsed.buttons) == 2


def test_reply_with_buttons_input_rejects_too_few() -> None:
    with pytest.raises(ValidationError):
        ReplyWithButtonsInput.model_validate({
            "text": "x", "buttons": ["Один"],
        })


def test_reply_with_buttons_input_rejects_too_many() -> None:
    with pytest.raises(ValidationError):
        ReplyWithButtonsInput.model_validate({
            "text": "x", "buttons": ["A", "B", "C", "D", "E"],
        })


def test_reply_with_buttons_input_rejects_long_label() -> None:
    """20-char cap on button labels."""
    with pytest.raises(ValidationError):
        ReplyWithButtonsInput.model_validate({
            "text": "x",
            "buttons": ["Да", "А" * 21],
        })


def test_reply_with_buttons_parser_ok() -> None:
    for n in (2, 3, 4):
        parsed = parse_tool_output("reply_with_buttons", f"ok:buttons:{n}")
        assert isinstance(parsed, ReplyWithButtonsOk)
        assert parsed.button_count == n


def test_reply_with_buttons_parser_rejects_out_of_range() -> None:
    for n in (0, 1, 5, 9):
        parsed = parse_tool_output("reply_with_buttons", f"ok:buttons:{n}")
        assert isinstance(parsed, ToolOutputContractViolation), f"n={n}"


def test_reply_with_buttons_parser_error_paths() -> None:
    for raw in (
        "error: buttons disabled in this context",
        "error: need at least 2 buttons",
    ):
        parsed = parse_tool_output("reply_with_buttons", raw)
        assert isinstance(parsed, HousewifeToolError)


# ---------------------------------------------------------------------------
# Web — Weather (Codex R1 MAJOR: full runtime signature)
# ---------------------------------------------------------------------------


def test_weather_input_minimal() -> None:
    """Defaults: day_offset=0, days_count=1, granularity=daily."""
    parsed = WeatherToolInput.model_validate({"location": "Москва"})
    assert parsed.location == "Москва"
    assert parsed.day_offset == 0
    assert parsed.days_count == 1
    assert parsed.granularity == "daily"


def test_weather_input_full_args() -> None:
    parsed = WeatherToolInput.model_validate({
        "location": "Питер",
        "day_offset": 1,
        "days_count": 7,
        "granularity": "hourly",
    })
    assert parsed.day_offset == 1
    assert parsed.days_count == 7
    assert parsed.granularity == "hourly"


def test_weather_input_rejects_empty_location() -> None:
    with pytest.raises(ValidationError):
        WeatherToolInput.model_validate({"location": "  "})


@pytest.mark.parametrize("day_offset,days_count", [
    (-1, 1),   # below zero
    (14, 1),   # exceeds 13 cap (0..13 valid)
    (0, 0),    # zero count rejected
    (0, 15),   # exceeds 14 cap
])
def test_weather_input_rejects_out_of_range_offset_or_count(
    day_offset, days_count,
) -> None:
    with pytest.raises(ValidationError):
        WeatherToolInput.model_validate({
            "location": "X",
            "day_offset": day_offset,
            "days_count": days_count,
        })


@pytest.mark.parametrize("day_offset,days_count", [
    (10, 5),   # 10+5=15 > 14
    (13, 2),   # 13+2=15 > 14
    (7, 8),    # 7+8=15 > 14
])
def test_weather_input_rejects_horizon_overflow(day_offset, days_count) -> None:
    """day_offset + days_count must fit within 14-day Open-Meteo window."""
    with pytest.raises(ValidationError):
        WeatherToolInput.model_validate({
            "location": "X",
            "day_offset": day_offset,
            "days_count": days_count,
        })


@pytest.mark.parametrize("day_offset,days_count", [
    (0, 14),   # full window from today
    (13, 1),   # last possible day, single
    (7, 7),    # mid-range, fits exactly
])
def test_weather_input_accepts_horizon_at_boundary(
    day_offset, days_count,
) -> None:
    parsed = WeatherToolInput.model_validate({
        "location": "X",
        "day_offset": day_offset,
        "days_count": days_count,
    })
    assert parsed.day_offset + parsed.days_count <= 14


def test_weather_input_rejects_unknown_granularity() -> None:
    with pytest.raises(ValidationError):
        WeatherToolInput.model_validate({
            "location": "X", "granularity": "monthly",
        })


def test_weather_parser_ok() -> None:
    parsed = parse_tool_output(
        "get_weather",
        "Москва\n☀️ сегодня: +5° · без осадков",
    )
    assert isinstance(parsed, WeatherToolOk)
    assert "Москва" in parsed.raw_text


def test_weather_parser_error_paths() -> None:
    for raw in (
        "error: empty location",
        "error: не нашла город 'XXX' — уточни",
        "error: сервис погоды не отвечает, попробуй позже",
        "error: пустой прогноз",
    ):
        parsed = parse_tool_output("get_weather", raw)
        assert isinstance(parsed, HousewifeToolError)


def test_weather_parser_city_not_found_stable_code() -> None:
    """Codex R1 HIGH catch: ``error: не нашла город 'X' — уточни`` has
    a dynamic ``'X'`` value; planner needs a stable ``error_code`` to
    branch on. Verify both quoted and unquoted variants map to
    ``city_not_found``."""
    for city_form in ("'XXX'", "'Москвабад'", "'Q W'"):
        raw = f"error: не нашла город {city_form} — уточни"
        parsed = parse_tool_output("get_weather", raw)
        assert isinstance(parsed, HousewifeToolError)
        assert parsed.error_code == "city_not_found", (
            f"expected stable city_not_found for {city_form!r}, "
            f"got {parsed.error_code!r}"
        )


# ---------------------------------------------------------------------------
# Web — Search
# ---------------------------------------------------------------------------


def test_web_search_input_real() -> None:
    parsed = WebSearchToolInput.model_validate({"query": "рецепт борща"})
    assert parsed.query == "рецепт борща"


def test_web_search_parser_empty() -> None:
    parsed = parse_tool_output("web_search", "no results")
    assert isinstance(parsed, WebSearchToolEmpty)


def test_web_search_parser_with_results() -> None:
    parsed = parse_tool_output(
        "web_search",
        "Title 1\nhttps://example.com/1\nSnippet 1\n\nTitle 2\nhttps://example.com/2\nSnippet 2",
    )
    assert isinstance(parsed, WebSearchToolOk)


def test_web_search_parser_error_paths() -> None:
    parsed = parse_tool_output("web_search", "error: empty query")
    assert isinstance(parsed, HousewifeToolError)


# ---------------------------------------------------------------------------
# Web — fetch_url input (Codex R1 MAJOR: SSRF mirror)
# ---------------------------------------------------------------------------


def test_fetch_url_input_real_http() -> None:
    parsed = FetchUrlToolInput.model_validate({
        "url": "http://example.com/article",
    })
    assert parsed.url.startswith("http://")


def test_fetch_url_input_real_https() -> None:
    parsed = FetchUrlToolInput.model_validate({
        "url": "https://example.com/article",
    })
    assert parsed.url.startswith("https://")


def test_fetch_url_input_rejects_non_http() -> None:
    with pytest.raises(ValidationError):
        FetchUrlToolInput.model_validate({"url": "ftp://example.com"})
    with pytest.raises(ValidationError):
        FetchUrlToolInput.model_validate({"url": "javascript:alert(1)"})
    with pytest.raises(ValidationError):
        FetchUrlToolInput.model_validate({"url": "example.com"})  # no scheme


def test_fetch_url_input_rejects_url_with_space() -> None:
    with pytest.raises(ValidationError):
        FetchUrlToolInput.model_validate({
            "url": "https://example.com/path with space",
        })


@pytest.mark.parametrize("blocked_host", [
    "http://localhost/admin",
    "http://localhost:8080/x",
    "https://127.0.0.1/data",
    "http://127.0.0.1:5432/",
    "http://0.0.0.0/",
    "http://[::1]/admin",
    "http://192.168.1.1/router",      # private IPv4
    "http://10.0.0.1/internal",       # private IPv4
    "http://172.16.0.1/private",      # private IPv4
    "http://169.254.169.254/latest",  # link-local (AWS metadata)
])
def test_fetch_url_input_ssrf_guard_blocks_unsafe_hosts(blocked_host) -> None:
    """Codex R1 MAJOR: schema mirrors runtime SSRF guard from
    web_search_tool.py:73 — reject localhost / private / link-local
    /reserved hosts at planner-input time."""
    with pytest.raises(ValidationError):
        FetchUrlToolInput.model_validate({"url": blocked_host})


# ---------------------------------------------------------------------------
# Web — fetch_url JSON envelope parser (Codex R1 MEDIUM/HIGH catch)
# ---------------------------------------------------------------------------


def test_fetch_url_parser_ok_json_envelope_html() -> None:
    """Codex R2 MAJOR: runtime extractor literals are {html, json, raw}
    — NOT readability. Readability-lxml is used internally to produce
    the ``html`` branch but never surfaces as a literal value.
    Codex R3 MINOR (medium): length must equal len(text)."""
    text = "Article content extracted via html branch"
    envelope = json.dumps({
        "url": "https://example.com/article",
        "final_url": "https://example.com/article",
        "status": 200,
        "extractor": "html",
        "truncated": False,
        "length": len(text),
        "text": text,
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, FetchUrlToolOk)
    assert parsed.http_status == 200
    assert parsed.extractor == "html"
    assert parsed.truncated is False
    assert parsed.length == len(text)


def test_fetch_url_parser_ok_extractor_json() -> None:
    """Codex R2 MAJOR: JSON-content-type responses produce
    extractor='json' per web_search_tool.py:404. Must NOT degrade
    to ToolOutputContractViolation."""
    text = '{"result": "ok"}'
    envelope = json.dumps({
        "url": "https://api.example.com/data",
        "final_url": "https://api.example.com/data",
        "status": 200,
        "extractor": "json",
        "truncated": False,
        "length": len(text),
        "text": text,
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, FetchUrlToolOk)
    assert parsed.extractor == "json"


def test_fetch_url_parser_rejects_legacy_readability_extractor() -> None:
    """Defence in depth: ``readability`` is NOT a runtime literal."""
    envelope = json.dumps({
        "url": "https://x.com/", "final_url": "https://x.com/",
        "status": 200, "extractor": "readability",
        "truncated": False, "length": 1, "text": "x",
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_fetch_url_parser_rejects_length_mismatch() -> None:
    """Codex R3 MINOR (medium): runtime computes length=len(text)
    at web_search_tool.py:426. Schema validator rejects tampered
    envelopes where these disagree."""
    envelope = json.dumps({
        "url": "https://x.com/", "final_url": "https://x.com/",
        "status": 200, "extractor": "html",
        "truncated": False,
        "length": 9999,                    # claimed
        "text": "actually short",          # actual len=14
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_fetch_url_parser_ok_with_redirect() -> None:
    """Final ``length`` ≤ _MAX_FETCH_CHARS + banner overhead per
    web_search_tool.py:414-426. Real worst-case ~12080 chars."""
    envelope = json.dumps({
        "url": "http://bit.ly/x",
        "final_url": "https://www.example.com/long",
        "status": 200,
        "extractor": "html",
        "truncated": True,
        "length": 12_075,
        "text": "x" * 12_075,
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, FetchUrlToolOk)
    assert parsed.url != parsed.final_url
    assert parsed.truncated is True


def test_fetch_url_parser_rejects_non_json() -> None:
    """Pre-R1 schema accepted raw text — now rejected as malformed."""
    parsed = parse_tool_output(
        "fetch_url",
        "Article content extracted via readability-lxml",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_fetch_url_parser_rejects_non_dict_json() -> None:
    parsed = parse_tool_output("fetch_url", '["a", "b"]')
    assert isinstance(parsed, ToolOutputContractViolation)


def test_fetch_url_parser_rejects_envelope_with_invalid_status() -> None:
    envelope = json.dumps({
        "url": "https://x.com/", "final_url": "https://x.com/",
        "status": 9999,  # out of range
        "extractor": "raw", "truncated": False, "length": 10, "text": "x",
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_fetch_url_parser_rejects_envelope_with_invalid_extractor() -> None:
    envelope = json.dumps({
        "url": "https://x.com/", "final_url": "https://x.com/",
        "status": 200,
        "extractor": "magic-soup-3000",  # not in Literal
        "truncated": False, "length": 10, "text": "x",
    })
    parsed = parse_tool_output("fetch_url", envelope)
    assert isinstance(parsed, ToolOutputContractViolation)


@pytest.mark.parametrize("raw", [
    "error: empty url",
    "error: timeout after 30s",
    "error: http 404",
    "error: ConnectionError",
])
def test_fetch_url_parser_error_paths(raw) -> None:
    parsed = parse_tool_output("fetch_url", raw)
    assert isinstance(parsed, HousewifeToolError)


# ---------------------------------------------------------------------------
# TypeAdapter parser→output_model parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,raw", [
    ("save_core_fact", "saved_core:abc"),
    ("save_core_fact", "error: empty content"),
    ("save_episode", "saved_episode:xyz"),
    ("recall_memory", "[]"),
    ("recall_memory", '[{"content":"x","source":"memory:core","score":0.8,"metadata":{}}]'),
    ("log_unsupported_request", "ok:logged"),
    ("reply_with_buttons", "ok:buttons:3"),
    ("get_weather", "Москва +5°"),
    ("get_weather", "error: empty location"),
    ("web_search", "no results"),
    ("web_search", "Some search results here"),
    ("fetch_url", json.dumps({
        "url": "https://x.com/", "final_url": "https://x.com/",
        "status": 200, "extractor": "html",
        "truncated": False, "length": 2, "text": "ok",
    })),
    ("fetch_url", "error: timeout after 30s"),
])
def test_parsers_validate_against_spec_output_model(tool, raw) -> None:
    all_specs = [*MEMORY_SPECS, *UTILITY_SPECS, *UI_SPECS, *WEB_SPECS]
    spec = next(s for s in all_specs if s.name == tool)
    parsed = parse_tool_output(tool, raw)
    assert not isinstance(parsed, ToolOutputContractViolation), (
        f"unexpected violation for {tool} / {raw!r}"
    )
    adapter = TypeAdapter(spec.output_model)
    validated = adapter.validate_python(parsed.model_dump())
    assert validated.status == parsed.status


def test_typeadapter_rejects_sentinel() -> None:
    all_specs = [*MEMORY_SPECS, *UTILITY_SPECS, *UI_SPECS, *WEB_SPECS]
    for spec in all_specs:
        adapter = TypeAdapter(spec.output_model)
        with pytest.raises(ValidationError):
            adapter.validate_python({
                "status": "contract_violation",
                "raw_output": "garbage",
                "tool_name": spec.name,
                "timestamp": "2026-05-27T00:00:00Z",
            })


# ---------------------------------------------------------------------------
# 100% migration milestone
# ---------------------------------------------------------------------------


def test_migrated_count_is_57() -> None:
    """Sub-A4 closure: every housewife + cross-skill tool typed.
    #143 Phase B добавил list_checklist_items → 56; #262b create_memory_category → 57."""
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert len(MIGRATED_TOOL_SPECS) == 57, (
        f"Expected 57 typed ToolSpec, got {len(MIGRATED_TOOL_SPECS)}. "
        f"Sub-A4 migration target was 100% — recount expected after "
        f"manifest changes."
    )


def test_all_55_specs_pass_production_quality() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
