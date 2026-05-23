"""Phase D.2 (Issue #68): validate_no_backup_leak script.

Plan: plans/mellow-discovering-conway-final.md, Section 5.

Tests:
- Synthetic config с / без exclude → LEAK or OK
- Trailing slash normalization
- Symlinked parent → resolved canonical, still LEAK
- `/` ancestor → LEAK (covers everything)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from validate_no_backup_leak import (
    canonicalize, covers, extract_paths, has_exclude_for, validate_file, main,
)


def test_extract_paths_strips_trailing_slash():
    paths = extract_paths("path=/var/lib/\ninclude=/etc/foo")
    str_paths = sorted(p.as_posix() for p in paths)
    assert "/var/lib" in str_paths
    assert "/etc/foo" in str_paths


def test_extract_paths_preserves_root():
    paths = extract_paths("path=/")
    str_paths = [p.as_posix() for p in paths]
    assert "/" in str_paths


def test_covers_root_includes_everything(tmp_path):
    assert covers(Path("/"), tmp_path) is True


def test_covers_ancestor_true(tmp_path):
    target = tmp_path / "private" / "llm-traces"
    target.mkdir(parents=True)
    assert covers(tmp_path, target) is True


def test_covers_non_ancestor_false(tmp_path):
    other = tmp_path / "other"
    target = tmp_path / "private" / "llm-traces"
    other.mkdir(); target.mkdir(parents=True)
    assert covers(other, target) is False


def test_covers_resolves_symlink_parent(tmp_path):
    """Symlink test (Unix only — Windows requires admin для symlinks)."""
    if os.name == "nt":
        pytest.skip("symlinks require admin on Windows")
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real_target = real_parent / "private" / "llm-traces"
    real_target.mkdir(parents=True)
    # Create symlink pointing TO real_parent
    link = tmp_path / "link"
    link.symlink_to(real_parent)
    # Config includes via symlink — should resolve и cover real_target
    assert covers(link, real_target) is True


def test_has_exclude_for_finds_exclude_line():
    text = """
    path = /var/lib
    exclude = /var/lib/sreda/private/llm-traces
    """
    assert has_exclude_for(text, Path("/var/lib/sreda/private/llm-traces")) is True


def test_has_exclude_for_missing_returns_false():
    text = """
    path = /var/lib
    backup_target = /var/backups
    """
    assert has_exclude_for(text, Path("/var/lib/sreda/private/llm-traces")) is False


def test_validate_file_leak_when_ancestor_no_exclude(tmp_path):
    cfg = tmp_path / "leak.conf"
    cfg.write_text("path=/var/lib\n", encoding="utf-8")
    leaks = validate_file(cfg, Path("/var/lib/sreda/private/llm-traces"))
    assert len(leaks) == 1
    assert "LEAK" in leaks[0]


def test_validate_file_ok_when_ancestor_with_exclude(tmp_path):
    cfg = tmp_path / "safe.conf"
    cfg.write_text(
        "path=/var/lib\nexclude=/var/lib/sreda/private/llm-traces\n",
        encoding="utf-8",
    )
    leaks = validate_file(cfg, Path("/var/lib/sreda/private/llm-traces"))
    assert leaks == []


def test_validate_file_ok_when_non_parent(tmp_path):
    cfg = tmp_path / "elsewhere.conf"
    cfg.write_text("path=/var/log\nbackup=/var/backups\n", encoding="utf-8")
    leaks = validate_file(cfg, Path("/var/lib/sreda/private/llm-traces"))
    assert leaks == []


def test_validate_file_leak_root_ancestor_no_exclude(tmp_path):
    cfg = tmp_path / "root_include.conf"
    cfg.write_text("path=/\n", encoding="utf-8")
    leaks = validate_file(cfg, Path("/var/lib/sreda/private/llm-traces"))
    assert len(leaks) == 1


def test_validate_file_ok_root_ancestor_with_exclude(tmp_path):
    cfg = tmp_path / "root_exclude.conf"
    cfg.write_text(
        "path=/\nexclude_files=/var/lib/sreda/private/llm-traces\n",
        encoding="utf-8",
    )
    leaks = validate_file(cfg, Path("/var/lib/sreda/private/llm-traces"))
    assert leaks == []


def test_main_exits_1_on_leak(tmp_path, capsys):
    cfg_dir = tmp_path / "etc" / "backup"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "leak.conf").write_text("path=/var/lib\n", encoding="utf-8")
    rc = main([
        "--config-root", str(tmp_path),
        "--config-glob", "etc/backup/*.conf",
        "--trace-path", "/var/lib/sreda/private/llm-traces",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "LEAK" in captured.out


def test_main_exits_0_on_clean(tmp_path, capsys):
    cfg_dir = tmp_path / "etc" / "backup"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "safe.conf").write_text(
        "path=/var/log\n", encoding="utf-8",
    )
    rc = main([
        "--config-root", str(tmp_path),
        "--config-glob", "etc/backup/*.conf",
        "--trace-path", "/var/lib/sreda/private/llm-traces",
    ])
    assert rc == 0


def test_canonicalize_nonexistent_path_no_raise():
    """Если path не существует — canonicalize doesn't raise."""
    p = Path("/does/not/exist/anywhere")
    result = canonicalize(p)
    # Either returns canonicalized parent + name, or path itself — no raise
    assert isinstance(result, Path)
