#!/usr/bin/env python3
"""Issue #68 — deploy-time guard: ensure no backup/log config leaks LLM traces.

LLM trace files на ``/var/lib/sreda/private/llm-traces/`` contain decrypted
PII (memories, user text, tool args). 152-ФЗ requires we NEVER copy this
off-box. Implicit private-path convention + dedicated tmpfiles config —
не достаточно: backup/log-shipping config может include parent path
(``/``, ``/var/lib``, ``/var``) and copy private data implicitly.

This script resolves include roots с canonical paths и проверяет каждый
config: если ancestor of trace path included WITHOUT explicit exclude —
LEAK detected, exit 1.

Strategy:
1. Glob known backup/log-shipping config files.
2. Extract path-like tokens с regex.
3. Resolve canonical via ``Path.resolve()`` (handles symlinks).
4. Normalize trailing slash (except root).
5. Check ancestor relationship vs TRACE_PATH (canonical).
6. Если ancestor without matching exclude в same config → LEAK.

Plan: plans/mellow-discovering-conway-final.md, Section 5.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

DEFAULT_TRACE_PATH = Path("/var/lib/sreda/private/llm-traces")
DEFAULT_CONFIG_GLOBS = [
    "etc/backup*/*.conf",
    "etc/backup*/*.yaml",
    "etc/rsync*/*.conf",
    "etc/systemd/journald.conf",
    "etc/systemd/journald.conf.d/*.conf",
    "etc/filebeat/*.yml",
    "etc/promtail/*.yml",
    "etc/datadog-agent/*.yaml",
    "etc/borgmatic*/*.yaml",
    "etc/rsnapshot.conf",
]

PATH_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9_-])(/[/A-Za-z0-9_.\-]*)')
EXCLUDE_KEYWORDS = (
    "exclude", "skip", "ignore", "deny",
    "ExcludePath", "exclude_files", "Excludes",
)

logger = logging.getLogger("sreda.llm_trace.validate_backup")


def canonicalize(p: Path) -> Path:
    """Resolve symlinks + collapse `.` / `..`. If target doesn't exist —
    use ``parent.resolve()`` + name (preserves as much canonical info как
    possible without raising)."""
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p


def _strip_comments(text: str) -> str:
    """Drop comment lines (``#``, ``;``, ``//`` after lstrip) и inline
    trailing comments (whitespace-prefixed ``#``/``;``).

    Without this, ``extract_paths`` would pull path-like tokens из
    commented-out include/exclude lines and produce false-positives or
    false-negatives in the LEAK check. See Codex review on PR #48
    (MAJOR — validate_no_backup_leak.py:99-113).
    """
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith(("#", ";", "//")):
            continue
        for marker in (" #", "\t#", " ;", "\t;"):
            idx = stripped.find(marker)
            if idx >= 0:
                stripped = stripped[:idx]
                break
        cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines)


def extract_paths(text: str) -> list[Path]:
    """Conservative path extraction from arbitrary config text.

    Strips trailing slashes (kept ``/`` as-is). Deduplicates. Skips
    commented-out lines (see ``_strip_comments``).
    """
    out: set[Path] = set()
    for tok in PATH_TOKEN_RE.findall(_strip_comments(text)):
        norm = tok.rstrip("/") or "/"
        try:
            out.add(Path(norm))
        except Exception:
            pass
    return sorted(out, key=lambda x: str(x))


def covers(include_root: Path, target: Path) -> bool:
    """True if ``include_root`` is ``/`` or canonical ancestor of ``target``.

    Resolves both sides (symlink-safe). Falls back to string-prefix check
    on Python < 3.9 (no ``is_relative_to``).
    """
    canon_root = canonicalize(include_root)
    canon_target = canonicalize(target)
    if str(canon_root) == "/":
        return True
    if canon_root == canon_target:
        return True
    try:
        return canon_target.is_relative_to(canon_root)
    except AttributeError:
        # Python < 3.9 fallback
        return str(canon_target).startswith(str(canon_root) + "/")


def has_exclude_for(text: str, target: Path) -> bool:
    """Check if config text has explicit exclude-line referencing ``target``.

    Compares против POSIX form of target — cross-platform safe (configs
    использовать forward-slashes regardless of host OS for validator).

    Skips comment lines (``#``, ``;``, ``//`` prefixes after lstrip) и
    inline comments (whitespace + ``#``/``;``). Без этого validator
    давал false-negative — config с ``path=/var/lib`` + закомментированным
    ``# exclude=/var/lib/sreda/private/llm-traces`` проходил проверку
    хотя exclude НЕ активен. See Codex review on PR #48
    (MAJOR — validate_no_backup_leak.py:99-113).
    """
    target_posix = target.as_posix()
    target_alt = target_posix + "/"
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith(("#", ";", "//")):
            continue
        # Strip trailing inline comment (best-effort — only after whitespace
        # to avoid mangling '#'/';' inside paths or quoted values).
        for marker in (" #", "\t#", " ;", "\t;"):
            idx = stripped.find(marker)
            if idx >= 0:
                stripped = stripped[:idx]
                break
        lower = stripped.lower()
        if not any(kw.lower() in lower for kw in EXCLUDE_KEYWORDS):
            continue
        if target_posix in stripped or target_alt in stripped:
            return True
    return False


def validate_file(path: Path, trace_path: Path) -> list[str]:
    """Returns list of LEAK descriptions для one config file."""
    leaks: list[str] = []
    try:
        text = path.read_text(errors="ignore")
    except Exception as e:
        logger.debug("cannot read %s: %s", path, e)
        return leaks
    for inc in extract_paths(text):
        if covers(inc, trace_path):
            if not has_exclude_for(text, trace_path):
                leaks.append(
                    f"LEAK: {path} includes {inc.as_posix()} "
                    f"(ancestor of {trace_path.as_posix()}) WITHOUT exclude"
                )
    return leaks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace-path", type=Path, default=DEFAULT_TRACE_PATH,
        help=f"Trace root path (default: {DEFAULT_TRACE_PATH})",
    )
    parser.add_argument(
        "--config-root", type=Path, default=Path("/"),
        help="Filesystem root для glob expansion (default: /)",
    )
    parser.add_argument(
        "--config-glob", action="append", default=None,
        help="Additional/override config globs (relative to --config-root)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    globs = args.config_glob if args.config_glob else DEFAULT_CONFIG_GLOBS
    trace_path = args.trace_path
    leaks_total: list[str] = []
    files_checked = 0
    for pat in globs:
        # Strip leading slash if present (config_root.glob уже relative)
        pat_clean = pat.lstrip("/")
        for path in args.config_root.glob(pat_clean):
            if not path.is_file():
                continue
            files_checked += 1
            leaks_total.extend(validate_file(path, trace_path))

    for leak in leaks_total:
        print(leak)
    if leaks_total:
        logger.error("validate_no_backup_leak FAILED: %d leaks across %d configs",
                     len(leaks_total), files_checked)
        return 1
    logger.info("validate_no_backup_leak: OK (checked %d configs)",
                files_checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
