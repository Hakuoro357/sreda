"""R7 (M-R3-3): _jsonify recursive normalization для non-JSON-native types.

Plan: plans/mellow-discovering-conway-final.md — Section 9.
Issue: #68.

LangChain payloads могут содержать non-JSON objects (bytes, pydantic
models, dataclasses, datetime, unknown classes). json.dumps raises на
них — trace row skipped именно когда unusual provider/tool payload
появляется (debug-critical моменты).

_jsonify recursive normalize:
- bytes → {"__bytes_b64__": base64}
- pydantic BaseModel → recursive(.model_dump())
- dataclass → recursive(dataclasses.asdict())
- datetime/date → .isoformat()
- list/tuple/set → [recursive(x) for x in]
- dict → {str(k): recursive(v) for k,v in}
- unknown → {"__unrepr__": type_name, "__repr__": repr(...)[:500]}

Plus json.dumps(..., default=_jsonify) catches any miss.
"""
from __future__ import annotations

import base64
import dataclasses
import json
from datetime import datetime, date, timezone

import pytest

from sreda.services.llm_trace import _jsonify


def test_none_passthrough():
    assert _jsonify(None) is None


def test_primitives_passthrough():
    assert _jsonify(True) is True
    assert _jsonify(42) == 42
    assert _jsonify(3.14) == 3.14
    assert _jsonify("hello") == "hello"


def test_bytes_base64_encoded():
    result = _jsonify(b"\x00\x01\xff")
    assert result == {"__bytes_b64__": base64.b64encode(b"\x00\x01\xff").decode("ascii")}
    # round-trip survives json
    assert json.dumps(result)


def test_datetime_isoformat():
    dt = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    assert _jsonify(dt) == dt.isoformat()
    d = date(2026, 5, 23)
    assert _jsonify(d) == "2026-05-23"


def test_dataclass_to_dict():
    @dataclasses.dataclass
    class Item:
        name: str
        value: int
    result = _jsonify(Item(name="x", value=42))
    assert result == {"name": "x", "value": 42}


def test_pydantic_model_to_dict():
    pytest.importorskip("pydantic")
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str
        value: int

    result = _jsonify(Item(name="x", value=42))
    assert result == {"name": "x", "value": 42}


def test_nested_list_recurse():
    result = _jsonify([1, "two", b"three", {"k": b"v"}])
    assert result[0] == 1
    assert result[1] == "two"
    assert "__bytes_b64__" in result[2]
    assert "__bytes_b64__" in result[3]["k"]


def test_nested_dict_recurse():
    result = _jsonify({"outer": {"inner": b"data"}})
    assert "__bytes_b64__" in result["outer"]["inner"]


def test_dict_key_stringified():
    result = _jsonify({42: "value", "str": 1})
    assert "42" in result
    assert "str" in result


def test_set_to_list():
    result = _jsonify({"a", "b"})
    assert isinstance(result, list)
    assert set(result) == {"a", "b"}


def test_unknown_type_repr_fallback():
    class CustomObj:
        def __repr__(self) -> str:
            return "CustomObj(state=opaque)"

    result = _jsonify(CustomObj())
    assert result == {"__unrepr__": "CustomObj", "__repr__": "CustomObj(state=opaque)"}


def test_repr_truncated_at_500():
    class HugeObj:
        def __repr__(self) -> str:
            return "x" * 1000

    result = _jsonify(HugeObj())
    assert len(result["__repr__"]) == 500


def test_final_jsonify_result_serializable():
    """Whole point of _jsonify: result MUST be json.dumps-safe."""
    payload = {
        "bytes": b"\x00",
        "dt": datetime(2026, 5, 23, tzinfo=timezone.utc),
        "nested": [{"date": date(2026, 5, 23)}],
    }
    normalized = _jsonify(payload)
    json.dumps(normalized)  # MUST NOT raise
