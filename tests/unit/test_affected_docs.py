"""Тесты определителя задетых доков (scripts/affected_docs.py, #248).

Карта «изменённые файлы → задетые enrolled-доки». Пересечение по Source code = hard;
пересечение только по Related code = soft (сигнал-пометка, решение владельца).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from affected_docs import compute_affected, normalize  # noqa: E402
from docs_registry import EnrolledDoc  # noqa: E402

_DOC = EnrolledDoc(
    path=Path("docs/architecture/react-preflight-pipeline.md"),
    source_paths=("src/sreda/runtime/react_preflight.py",),
    related_paths=("src/sreda/runtime/react_loop.py",),
    verified_against="75f9ab8",
)


def test_hard_match_on_source():
    res = compute_affected({"src/sreda/runtime/react_preflight.py"}, [_DOC])
    assert len(res) == 1
    assert res[0]["doc"].endswith("react-preflight-pipeline.md")
    assert res[0]["soft"] is False
    assert res[0]["matched"] == ["src/sreda/runtime/react_preflight.py"]
    assert res[0]["verified_against"] == "75f9ab8"


def test_soft_match_on_related_only():
    res = compute_affected({"src/sreda/runtime/react_loop.py"}, [_DOC])
    assert len(res) == 1
    assert res[0]["soft"] is True
    assert res[0]["matched"] == ["src/sreda/runtime/react_loop.py"]


def test_source_wins_over_related_when_both_changed():
    res = compute_affected(
        {"src/sreda/runtime/react_preflight.py", "src/sreda/runtime/react_loop.py"}, [_DOC])
    assert len(res) == 1
    assert res[0]["soft"] is False  # любой hard-источник → не soft


def test_empty_on_unrelated_change():
    assert compute_affected({"src/sreda/other.py"}, [_DOC]) == []


def test_empty_input():
    assert compute_affected(set(), [_DOC]) == []


def test_normalize_backslashes_and_dotslash():
    assert normalize("src\\sreda\\runtime\\react_preflight.py") == "src/sreda/runtime/react_preflight.py"
    assert normalize("./src/a.py") == "src/a.py"
