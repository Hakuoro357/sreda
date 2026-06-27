"""Тесты общего разбора шапок архитектурных доков (scripts/docs_registry.py, #248).

Один источник «карты файл→док» для check_docs_freshness.py и affected_docs.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make scripts/ importable (как в test_cleanup_llm_traces.py)
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from docs_registry import parse_doc_header, scan_docs  # noqa: E402


def test_parse_header_full():
    text = (
        "# Title\n"
        "**Owner:** x\n"
        "**Source code:** `src/a.py`, `src/b.py` (описание)\n"
        "**Related code:** `src/c.py` (смежный)\n"
        "**Verified-against:** `abc1234` (2026-06-27)\n\n"
        "тело\n"
    )
    h = parse_doc_header(text)
    assert tuple(h.source_paths) == ("src/a.py", "src/b.py")
    assert tuple(h.related_paths) == ("src/c.py",)
    assert h.verified_against == "abc1234"


def test_parse_header_no_verified():
    text = "**Source code:** `src/d.py`\n"
    h = parse_doc_header(text)
    assert tuple(h.source_paths) == ("src/d.py",)
    assert h.related_paths == ()
    assert h.verified_against is None


def test_parse_header_no_source():
    h = parse_doc_header("# просто индекс, без Source code\n")
    assert h.source_paths == ()
    assert h.verified_against is None


def test_parse_header_root_and_config_paths_kept_inline_symbols_dropped():
    # MAJOR-B: корневые/конфиг-файлы (с расширением) и обратные слэши — трекаемы;
    # инлайн-символы кода в бэктиках на строке шапки — отсекаются.
    text = (
        "**Source code:** `pyproject.toml`, `requirements.txt`, `configs\\tool.toml`\n"
        "**Related code:** `react_loop.py` (узел `chat`, `_build_graph`)\n"
    )
    h = parse_doc_header(text)
    assert tuple(h.source_paths) == ("pyproject.toml", "requirements.txt", "configs/tool.toml")
    assert tuple(h.related_paths) == ("react_loop.py",)  # `chat`/`_build_graph` отброшены


def test_scan_docs_partitions(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "architecture").mkdir(parents=True)
    (docs / "architecture" / "enrolled.md").write_text(
        "**Source code:** `src/e.py`\n**Verified-against:** `deadbeef`\n", encoding="utf-8")
    (docs / "architecture" / "legacy.md").write_text(
        "**Source code:** `src/legacy.py`\n", encoding="utf-8")
    (docs / "README.md").write_text("# индекс, без Source code\n", encoding="utf-8")

    enrolled, not_enrolled = scan_docs(docs)
    assert [d.path.name for d in enrolled] == ["enrolled.md"]
    assert enrolled[0].verified_against == "deadbeef"
    assert tuple(enrolled[0].source_paths) == ("src/e.py",)
    assert [p.name for p in not_enrolled] == ["legacy.md"]
