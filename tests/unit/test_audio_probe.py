"""Unit tests для `audio_probe.ffprobe_duration`.

Phase 2 voice flow зависит от accurate duration для quota charging.
2026-05-08 prod incident: MAX-юзер 142322319 не мог отправить voice
потому что MAX container не записывает `format.duration`. Эти тесты
zafiksируют fallback chain: format → streams → byte-estimate.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from sreda.services.audio_probe import FfprobeError, ffprobe_duration


def _mock_subprocess_run(returncode=0, stdout="", stderr=""):
    """Helper: build a fake CompletedProcess return value."""
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---- Happy path: format.duration ----


def test_format_duration_extracted() -> None:
    """Standard TG OGG/Opus — format.duration set."""
    fake_out = json.dumps({
        "format": {"duration": "12.345"},
        "streams": [],
    })
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(b"x" * 1000) == pytest.approx(12.345)


# ---- 2026-05-08 incident: format.duration missing → fallback ----


def test_format_missing_streams_duration_used() -> None:
    """MAX OGG/Opus — format.duration пустой, но streams[].duration set."""
    fake_out = json.dumps({
        "format": {},
        "streams": [{"duration": "8.0"}],
    })
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(b"x" * 1000) == pytest.approx(8.0)


def test_format_missing_streams_first_zero_skips_to_next() -> None:
    """Если первый stream имеет duration=0 (не valid) — пробуем следующий."""
    fake_out = json.dumps({
        "format": {},
        "streams": [
            {"duration": "0"},      # invalid
            {"duration": "5.5"},     # valid
        ],
    })
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(b"x" * 1000) == pytest.approx(5.5)


def test_format_missing_streams_missing_byte_estimate_used() -> None:
    """Когда format.duration И streams нет — используем byte estimate.
    1500 bytes/sec (12 kbps Opus voice, conservative — Codex r4 MAJOR)."""
    fake_out = json.dumps({"format": {}, "streams": []})
    audio_bytes = b"x" * 4500  # 4500 / 1500 = 3.0s
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(audio_bytes) == pytest.approx(3.0)


def test_byte_estimate_capped_at_free_daily_ceiling() -> None:
    """Byte estimate сверху ограничен 300s (дневной free-лимит
    SREDA_FREE_VOICE_SECONDS_DAILY). Аудит 2026-07-18 (svc-features #10):
    прежний кап 30s систематически занижал длительность голосовых >30s
    (TG такие позволяет) → undercharge voice_stt_seconds в money-path."""
    fake_out = json.dumps({"format": {}, "streams": []})
    huge_bytes = b"x" * 1_000_000  # would estimate to 666s
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(huge_bytes) == pytest.approx(300.0)


def test_zero_byte_audio_with_no_metadata_raises() -> None:
    """Empty audio + no metadata → cannot estimate → FfprobeError."""
    fake_out = json.dumps({"format": {}, "streams": []})
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        with pytest.raises(FfprobeError, match="byte estimate also failed"):
            ffprobe_duration(b"")


# ---- Error paths ----


def test_ffprobe_binary_missing_raises() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(FfprobeError, match="ffprobe binary not found"):
            ffprobe_duration(b"x" * 100)


def test_ffprobe_nonzero_exit_raises() -> None:
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_run(returncode=1, stderr="bad input"),
    ):
        with pytest.raises(FfprobeError, match="ffprobe exited 1"):
            ffprobe_duration(b"x" * 100)


def test_ffprobe_invalid_json_raises() -> None:
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_run(stdout="not json"),
    ):
        with pytest.raises(FfprobeError, match="ffprobe output not JSON"):
            ffprobe_duration(b"x" * 100)


def test_ffprobe_timeout_raises() -> None:
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=10),
    ):
        with pytest.raises(FfprobeError, match="ffprobe timed out"):
            ffprobe_duration(b"x" * 100)


def test_format_duration_negative_falls_back_to_streams() -> None:
    """Defensive: format.duration='-1' (invalid) → fallback to streams."""
    fake_out = json.dumps({
        "format": {"duration": "-1"},  # invalid
        "streams": [{"duration": "3.0"}],
    })
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(b"x" * 100) == pytest.approx(3.0)


def test_format_duration_inf_falls_back_to_streams() -> None:
    """Codex r4 MINOR: float('inf') passes `d > 0` but breaks quota
    math. isfinite() guard rejects, falls back to next source."""
    fake_out = json.dumps({
        "format": {"duration": "Infinity"},  # math.isinf → reject
        "streams": [{"duration": "5.0"}],
    })
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(b"x" * 100) == pytest.approx(5.0)


def test_stream_duration_nan_falls_back_to_byte_estimate() -> None:
    """isfinite() also rejects nan. Both format and stream nan →
    byte estimate fallback."""
    fake_out = json.dumps({
        "format": {"duration": "NaN"},
        "streams": [{"duration": "NaN"}],
    })
    # 3000 bytes / 1500 bytes-per-sec = 2.0s estimate
    with patch("subprocess.run", return_value=_mock_subprocess_run(stdout=fake_out)):
        assert ffprobe_duration(b"x" * 3000) == pytest.approx(2.0)
