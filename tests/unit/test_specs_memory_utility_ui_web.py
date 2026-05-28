"""Integration tests for the final 4 families closing Sub-A4 to 55/55:
- memory (3 tools)
- utility (1 tool)
- ui (1 tool)
- web (3 tools)
"""

from __future__ import annotations

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
    FETCH_URL_TOOL_SPEC,
    FetchUrlToolInput,
    WEATHER_TOOL_SPEC,
    WEB_SEARCH_TOOL_SPEC,
    WEB_SPECS,
    WeatherToolInput,
    WebSearchToolInput,
)


# ---------------------------------------------------------------------------
# Family-level invariants
# ---------------------------------------------------------------------------


def test_memory_specs_construct() -> None:
    assert len(MEMORY_SPECS) == 3
    assert {s.name for s in MEMORY_SPECS} == {
        "save_core_fact", "save_episode", "recall_memory",
    }


def test_utility_specs_construct() -> None:
    assert len(UTILITY_SPECS) == 1
    assert UTILITY_SPECS[0].name == "log_unsupported_request"


def test_ui_specs_construct() -> None:
    assert len(UI_SPECS) == 1
    assert UI_SPECS[0].name == "reply_with_buttons"


def test_web_specs_construct() -> None:
    assert len(WEB_SPECS) == 3
    assert {s.name for s in WEB_SPECS} == {
        "weather_tool", "web_search_tool", "fetch_url_tool",
    }


def test_all_four_families_pass_quality_strict() -> None:
    assert_production_registry_quality(MEMORY_SPECS)
    assert_production_registry_quality(UTILITY_SPECS)
    assert_production_registry_quality(UI_SPECS)
    assert_production_registry_quality(WEB_SPECS)


def test_manifest_matches_specs() -> None:
    for family, expected in [
        ("memory", {"save_core_fact", "save_episode", "recall_memory"}),
        ("utility", {"log_unsupported_request"}),
        ("ui", {"reply_with_buttons"}),
        ("web", {"weather_tool", "web_search_tool", "fetch_url_tool"}),
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
    import json
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
# Web input + parser
# ---------------------------------------------------------------------------


def test_weather_input_real() -> None:
    parsed = WeatherToolInput.model_validate({"location": "Москва"})
    assert parsed.location == "Москва"


def test_weather_input_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        WeatherToolInput.model_validate({"location": "  "})


def test_web_search_input_real() -> None:
    parsed = WebSearchToolInput.model_validate({"query": "рецепт борща"})
    assert parsed.query == "рецепт борща"


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


def test_weather_parser_ok() -> None:
    parsed = parse_tool_output(
        "weather_tool",
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
        parsed = parse_tool_output("weather_tool", raw)
        assert isinstance(parsed, HousewifeToolError)


def test_web_search_parser_empty() -> None:
    parsed = parse_tool_output("web_search_tool", "no results")
    assert isinstance(parsed, WebSearchToolEmpty)


def test_web_search_parser_with_results() -> None:
    parsed = parse_tool_output(
        "web_search_tool",
        "Title 1\nhttps://example.com/1\nSnippet 1\n\nTitle 2\nhttps://example.com/2\nSnippet 2",
    )
    assert isinstance(parsed, WebSearchToolOk)


def test_web_search_parser_error_paths() -> None:
    parsed = parse_tool_output("web_search_tool", "error: empty query")
    assert isinstance(parsed, HousewifeToolError)


def test_fetch_url_parser_ok() -> None:
    parsed = parse_tool_output(
        "fetch_url_tool",
        "Article content extracted via readability-lxml",
    )
    assert isinstance(parsed, FetchUrlToolOk)


@pytest.mark.parametrize("raw", [
    "error: empty url",
    "error: timeout after 30s",
    "error: http 404",
    "error: ConnectionError",
])
def test_fetch_url_parser_error_paths(raw) -> None:
    parsed = parse_tool_output("fetch_url_tool", raw)
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
    ("weather_tool", "Москва +5°"),
    ("weather_tool", "error: empty location"),
    ("web_search_tool", "no results"),
    ("web_search_tool", "Some search results here"),
    ("fetch_url_tool", "Page content"),
    ("fetch_url_tool", "error: timeout after 30s"),
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


def test_migrated_count_is_55() -> None:
    """Sub-A4 closure: every housewife + cross-skill tool typed."""
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert len(MIGRATED_TOOL_SPECS) == 55, (
        f"Expected 55 typed ToolSpec, got {len(MIGRATED_TOOL_SPECS)}. "
        f"Sub-A4 migration target was 100% — recount expected after "
        f"manifest changes."
    )


def test_all_55_specs_pass_production_quality() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
