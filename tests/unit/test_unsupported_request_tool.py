"""Unit tests for the log_unsupported_request tool."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from sreda.runtime.tools import build_memory_tools


@pytest.fixture(autouse=True)
def _restore_feature_requests_propagation():
    """Other tests may have called ``configure_logging`` which disables
    propagation on ``sreda.feature_requests``. Without propagation
    ``caplog`` can't see records. Force-enable before each test here;
    restore previous state after."""
    logger = logging.getLogger("sreda.feature_requests")
    prev = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = prev


def _tools_by_name(tools):
    return {t.name: t for t in tools}


def _fake_embedding_client():
    client = MagicMock()
    client.embed.return_value = [0.0] * 8
    return client


def _get_tool():
    tools = build_memory_tools(
        session=MagicMock(),
        tenant_id="tenant_1",
        user_id="user_1",
        embedding_client=_fake_embedding_client(),
    )
    return _tools_by_name(tools)["log_unsupported_request"]


def test_logs_at_info_with_structured_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger="sreda.feature_requests")

    tool = _get_tool()
    result = tool.invoke(
        {
            "user_asked": "Закажи мне такси домой",
            "reason": "нет интеграции с Яндекс.Такси",
        }
    )

    assert result == "ok:logged"
    records = [r for r in caplog.records if r.name == "sreda.feature_requests"]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "tenant_1" in msg
    assert "user_1" in msg
    assert "Закажи мне такси домой" in msg
    assert "Яндекс.Такси" in msg


def test_missing_fields_return_error_and_log_nothing(caplog) -> None:
    caplog.set_level(logging.INFO, logger="sreda.feature_requests")
    tool = _get_tool()

    r1 = tool.invoke({"user_asked": "", "reason": "x"})
    r2 = tool.invoke({"user_asked": "x", "reason": ""})

    assert r1.startswith("error:")
    assert r2.startswith("error:")
    records = [r for r in caplog.records if r.name == "sreda.feature_requests"]
    assert records == []


def test_truncates_overlong_inputs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="sreda.feature_requests")
    tool = _get_tool()
    long_input = "a" * 500

    tool.invoke({"user_asked": long_input, "reason": long_input})

    records = [r for r in caplog.records if r.name == "sreda.feature_requests"]
    msg = records[0].getMessage()
    # Each field capped at 200 chars. A run of 201 'a' in a row must
    # not exist — enforces the cap regardless of incidental 'a's in
    # surrounding log metadata.
    assert "a" * 201 not in msg
    assert "a" * 200 in msg
