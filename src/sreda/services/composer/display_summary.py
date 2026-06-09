"""Deterministic ``display_summary`` builder for write-tool outputs (#115).

Owner decision (2026-06-09): the user-facing reply is produced by the LIVE VOICE
(LLM "mouth") from the structured names + a response template — there is NO baked
final sentence and NO deterministic guard. This builder produces the single safe
field (``display_summary``) that the presenter hands to the voice: a deterministic,
sanitized, length-capped rendering of the affected NAMES grouped by category. The
voice rephrases it in Среда's tone; the machine guarantee is only that the names
are present here (red-first tests), per ПРАВИЛО #7.

Determinism (required so red-first tests are stable — Kimi M4):
* names are sorted;
* each name is whitespace/control-collapsed and trimmed to ``max_name_len`` (+ ``…``);
* at most ``max_names`` names per group are listed, then an exact ``«и ещё N»`` remainder
  (the structured model fields always carry ALL names — the cap is display-only, Codex R3);
* an empty outcome (no groups, no not-found) yields a fixed neutral phrase, no names.

Pure (no I/O).
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

MAX_NAMES = 10
MAX_NAME_LEN = 60
_EMPTY_PHRASE = "Изменений нет."

_WS_RE = re.compile(r"\s+")
# Control chars (C0/C1 except the whitespace we normalise) → stripped.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_name(name: object, *, max_len: int = MAX_NAME_LEN) -> str:
    """Collapse whitespace, strip control chars + guillemets, trim to ``max_len``.

    Guillemets (« ») are stripped because the builder wraps each name in them as an
    unambiguous boundary (Codex Ф0 R1 [MAJOR]); a name must not be able to forge a
    closing quote and break out of the data delimiter when the live voice reads it.
    """
    text = "" if name is None else str(name)
    text = _CTRL_RE.sub("", text)
    text = text.replace("«", "").replace("»", "")
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max(0, max_len - 1)].rstrip() + "…"
    return text


def _render_group(names: Sequence[str], *, max_names: int, max_name_len: int) -> str:
    cleaned = [sanitize_name(n, max_len=max_name_len) for n in names]
    cleaned = [n for n in cleaned if n]
    cleaned.sort()
    if not cleaned:
        return ""
    # Wrap each name in guillemets so a name containing ',', ':' or '.' (the whole
    # reason #115 exists) cannot forge a new group/sentence for the live voice.
    if len(cleaned) > max_names:
        shown = [f"«{n}»" for n in cleaned[:max_names]]
        remainder = len(cleaned) - max_names
        return ", ".join(shown) + f" и ещё {remainder}"
    return ", ".join(f"«{n}»" for n in cleaned)


def build_display_summary(
    parts: Iterable[tuple[str, Sequence[str]]],
    *,
    not_found_count: int = 0,
    max_names: int = MAX_NAMES,
    max_name_len: int = MAX_NAME_LEN,
) -> str:
    """Build the safe ``display_summary`` string handed to the live voice.

    ``parts`` is an ordered list of ``(label, names)``; only non-empty groups are
    rendered, as ``"<label>: a, b, c"``. ``not_found_count`` (ids that don't exist /
    aren't owned — no name knowable) appends ``"N не нашла."``. Empty everything →
    a fixed neutral phrase (no names).
    """
    segments: list[str] = []
    for label, names in parts:
        rendered = _render_group(names, max_names=max_names, max_name_len=max_name_len)
        if rendered:
            segments.append(f"{label}: {rendered}.")
    if not_found_count and not_found_count > 0:
        segments.append(f"{not_found_count} не нашла.")
    if not segments:
        return _EMPTY_PHRASE
    return " ".join(segments)


__all__ = [
    "MAX_NAMES",
    "MAX_NAME_LEN",
    "sanitize_name",
    "build_display_summary",
]
