"""Phase D.1 (Issue #68): cleanup_llm_traces script.

Plan: plans/mellow-discovering-conway-final.md, Section 12.

Tests folder-name parsing strategy (NOT mtime) — folder name = source of truth.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

# Make scripts/ importable
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from cleanup_llm_traces import cleanup, DAY_FOLDER_RE


def _make_day_folder(root: Path, name: str) -> Path:
    """Create folder with a fake trace file inside."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trace_fake.jsonl").write_text("{}\n", encoding="utf-8")
    return folder


def test_cleanup_removes_old_folders(tmp_path):
    today = datetime.now(timezone.utc).date()
    old = today - timedelta(days=10)
    recent = today - timedelta(days=2)
    _make_day_folder(tmp_path, old.isoformat())
    _make_day_folder(tmp_path, recent.isoformat())

    deleted, kept = cleanup(tmp_path, keep_days=5, dry_run=False)
    assert deleted == 1
    assert kept == 1
    assert not (tmp_path / old.isoformat()).exists()
    assert (tmp_path / recent.isoformat()).exists()


def test_cleanup_dry_run_does_not_delete(tmp_path):
    today = datetime.now(timezone.utc).date()
    old = today - timedelta(days=10)
    _make_day_folder(tmp_path, old.isoformat())
    deleted, kept = cleanup(tmp_path, keep_days=5, dry_run=True)
    assert deleted == 1
    assert (tmp_path / old.isoformat()).exists()


def test_cleanup_keeps_unparseable_folder_names(tmp_path):
    """Unknown folder structure — skip, не trogaem."""
    _make_day_folder(tmp_path, "archive")
    _make_day_folder(tmp_path, "2026-13-99")  # invalid date but parseable regex format
    today = datetime.now(timezone.utc).date()
    _make_day_folder(tmp_path, today.isoformat())
    deleted, kept = cleanup(tmp_path, keep_days=5, dry_run=False)
    # "archive" — does not match DAY_FOLDER_RE → не cleaned (skipped)
    # "2026-13-99" — matches regex но invalid date → ValueError → skip
    # today — kept
    assert (tmp_path / "archive").exists()
    assert (tmp_path / "2026-13-99").exists()
    assert (tmp_path / today.isoformat()).exists()
    assert kept == 1
    assert deleted == 0


def test_cleanup_uses_folder_name_not_mtime(tmp_path):
    """Folder с old name но recent mtime — должна быть удалена."""
    today = datetime.now(timezone.utc).date()
    old_name = (today - timedelta(days=10)).isoformat()
    folder = _make_day_folder(tmp_path, old_name)
    # Force mtime to NOW (simulate cron access)
    import os, time
    now_ts = time.time()
    os.utime(folder, (now_ts, now_ts))

    deleted, kept = cleanup(tmp_path, keep_days=5, dry_run=False)
    # Despite mtime=now, folder-name says it's 10 days old → deleted
    assert deleted == 1
    assert not folder.exists()


def test_cleanup_keep_days_zero_keeps_today_only(tmp_path):
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    _make_day_folder(tmp_path, today.isoformat())
    _make_day_folder(tmp_path, yesterday.isoformat())
    deleted, kept = cleanup(tmp_path, keep_days=0, dry_run=False)
    # cutoff = today - 0 days = today. yesterday < today → deleted.
    assert deleted == 1
    assert kept == 1
    assert (tmp_path / today.isoformat()).exists()
    assert not (tmp_path / yesterday.isoformat()).exists()


def test_cleanup_missing_root_returns_zero(tmp_path):
    """Если root не существует — log warn, не падаем."""
    missing = tmp_path / "nope"
    deleted, kept = cleanup(missing, keep_days=5)
    assert deleted == 0
    assert kept == 0


def test_day_folder_regex():
    assert DAY_FOLDER_RE.match("2026-05-23")
    assert not DAY_FOLDER_RE.match("2026-5-23")  # no zero-pad
    assert not DAY_FOLDER_RE.match("archive")
    assert not DAY_FOLDER_RE.match("2026-05-23-extra")
