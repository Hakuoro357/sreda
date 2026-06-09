"""#115 Ф0 — okv2 codec contract (red-before-impl).

AC-envelope: malformed okv2 / unknown status / reserved payload status / non-object
payload → ToolOkParseError (mapped to ToolOutputContractViolation by the per-tool
parser). encode↔parse round-trips; discriminator comes only from the envelope prefix.
"""

from __future__ import annotations

import json

import pytest

from sreda.services.tool_schemas.tool_ok_codec import (
    OKV2_PREFIX,
    ToolOkParseError,
    encode_tool_ok,
    is_okv2,
    parse_tool_ok,
)

ACCEPTED = frozenset({"added", "empty", "replay"})


def test_encode_shape():
    wire = encode_tool_ok("added", {"added_count": 2, "created": ["молоко", "хлеб"]})
    assert wire.startswith("okv2:added:")
    # payload is valid JSON after the second colon
    body = wire.split(":", 2)[2]
    assert json.loads(body) == {"added_count": 2, "created": ["молоко", "хлеб"]}


def test_round_trip_names_with_separators():
    # names with ':' ',' and spaces must survive (the whole reason for okv2)
    payload = {"created": ["молоко 2,5%", "сыр: гауда", "хлеб"]}
    status, got = parse_tool_ok(encode_tool_ok("added", payload), ACCEPTED)
    assert status == "added"
    assert got == payload


def test_is_okv2_detection():
    assert is_okv2("okv2:added:{}")
    assert not is_okv2("ok:added:2:ids=[sh_x]")
    assert not is_okv2("error: boom")


def test_parse_rejects_non_okv2():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok("ok:added:2", ACCEPTED)


def test_parse_rejects_too_few_segments():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok("okv2:added", ACCEPTED)


def test_parse_rejects_empty_payload():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok("okv2:added:", ACCEPTED)


def test_parse_rejects_bad_json():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok("okv2:added:{not json", ACCEPTED)


def test_parse_rejects_non_object_payload():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok('okv2:added:["a","b"]', ACCEPTED)


def test_parse_rejects_unknown_status():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok('okv2:bogus:{"created":["x"]}', ACCEPTED)


def test_parse_rejects_reserved_status_in_payload():
    # payload-level "status" must never override the envelope discriminator (Kimi R4)
    with pytest.raises(ToolOkParseError):
        parse_tool_ok('okv2:added:{"status":"empty","created":["x"]}', ACCEPTED)


def test_encode_rejects_reserved_status_in_payload():
    with pytest.raises(ToolOkParseError):
        encode_tool_ok("added", {"status": "empty"})


def test_encode_rejects_colon_in_status():
    with pytest.raises(ToolOkParseError):
        encode_tool_ok("ad:ded", {"created": ["x"]})


def test_encode_rejects_non_serializable_payload():
    # Codex Ф0 R1 [MAJOR]: a non-JSON value is a producer bug → fail closed,
    # never silently str()-ified onto the wire.
    with pytest.raises(ToolOkParseError):
        encode_tool_ok("added", {"created": [object()]})


def test_encode_rejects_nan_inf():
    # Codex Ф0 R2 [MAJOR]: strict JSON — no NaN/Infinity on the wire.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ToolOkParseError):
            encode_tool_ok("added", {"x": bad})


def test_parse_rejects_nan_inf_tokens():
    with pytest.raises(ToolOkParseError):
        parse_tool_ok('okv2:added:{"x":NaN}', ACCEPTED)
    with pytest.raises(ToolOkParseError):
        parse_tool_ok('okv2:added:{"x":Infinity}', ACCEPTED)


def test_parse_error_message_does_not_echo_raw():
    # Codex Ф0 R1 [MINOR]: exception text must not leak raw tool content.
    secret = "sh_deadbeefcafe и тайное имя"
    try:
        parse_tool_ok(f"okv2:bogus:{{\"x\":\"{secret}\"}}", ACCEPTED)
    except ToolOkParseError as exc:
        assert secret not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ToolOkParseError")


def test_status_only_from_envelope_not_payload():
    # even a benign payload never contributes the discriminator
    status, payload = parse_tool_ok('okv2:replay:{"replayed":["молоко"]}', ACCEPTED)
    assert status == "replay"
    assert "status" not in payload
