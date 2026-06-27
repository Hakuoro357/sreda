"""Общий разбор шапок архитектурных доков (vex-assistant#248).

ЕДИНЫЙ источник «карты файл→док»: используется и проверкой свежести
(`check_docs_freshness.py`), и определителем задетых доков (`affected_docs.py`).
Держать парсинг в одном месте — чтобы карта, которой доверяет doc-sync агент,
и карта, которую сверяет CI, не могли разойтись.

Шапка дока (markdown):
    **Source code:** `путь/к/ядру.py` [, `ещё.py` ...]   — отслеживается на свежесть
    **Related code:** `путь/смежный.py` ...               — НЕ отслеживается; сигнал-пометка
    **Verified-against:** `<git-sha>`                      — коммит, на котором док сверен

Док ENROLLED (отслеживаемый) ⟺ есть И `Source code`, И `Verified-against`.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

SRC_RE = re.compile(r"^\*\*Source code:\*\*\s*(.+)$", re.M)
RELATED_RE = re.compile(r"^\*\*Related code:\*\*\s*(.+)$", re.M)
VER_RE = re.compile(r"^\*\*Verified-against:\*\*\s*`?([0-9a-fA-F]{7,40})`?", re.M)
PATH_RE = re.compile(r"`([^`]+)`")


def _paths_from(line: str) -> tuple[str, ...]:
    """Бэктик-пути из строки шапки. Нормализуем '\\'→'/'; берём токен, если в нём есть '/' ИЛИ '.'
    (путь или файл с расширением), отсекая инлайн-символы кода (`chat`, `_build_graph`).
    Файлы без расширения в корне (Dockerfile/Makefile) писать с префиксом `./`, чтобы попали в карту."""
    out: list[str] = []
    for raw in PATH_RE.findall(line):
        p = raw.strip().replace("\\", "/")
        if "/" in p or "." in p:
            out.append(p)
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class DocHeader:
    source_paths: tuple[str, ...]
    related_paths: tuple[str, ...]
    verified_against: str | None


@dataclasses.dataclass(frozen=True)
class EnrolledDoc:
    path: pathlib.Path
    source_paths: tuple[str, ...]
    related_paths: tuple[str, ...]
    verified_against: str


def parse_doc_header(text: str) -> DocHeader:
    sm = SRC_RE.search(text)
    rm = RELATED_RE.search(text)
    vm = VER_RE.search(text)
    return DocHeader(
        source_paths=_paths_from(sm.group(1)) if sm else (),
        related_paths=_paths_from(rm.group(1)) if rm else (),
        verified_against=vm.group(1) if vm else None,
    )


def scan_docs(docs_dir: str | pathlib.Path = "docs") -> tuple[list[EnrolledDoc], list[pathlib.Path]]:
    """Обойти docs/**/*.md → (enrolled, not_enrolled).

    enrolled — есть Source code И Verified-against. not_enrolled — есть Source code, но нет
    Verified-against (легаси-доки в бэкфилле; проверку свежести НЕ валят). Доки без Source code
    (индекс/копи/runbook'и) пропускаются.
    """
    docs_dir = pathlib.Path(docs_dir)
    enrolled: list[EnrolledDoc] = []
    not_enrolled: list[pathlib.Path] = []
    for md in sorted(docs_dir.rglob("*.md")):
        h = parse_doc_header(md.read_text(encoding="utf-8"))
        if not h.source_paths:
            continue
        if h.verified_against:
            enrolled.append(EnrolledDoc(md, h.source_paths, h.related_paths, h.verified_against))
        else:
            not_enrolled.append(md)
    return enrolled, not_enrolled
