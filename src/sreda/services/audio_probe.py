"""ffprobe wrapper — extract duration from audio bytes для voice quota.

Phase 2 of free-tier-subscription plan. Voice flow needs accurate
duration ПЕРЕД STT call to charge `usage_ledger['voice_stt_seconds']`
correctly. ffprobe (от ffmpeg) даёт sub-millisecond accuracy для
OGG/Opus, MP3, WAV, MP4/AAC.

Dependency: `ffprobe` binary должен быть в PATH. Verified на prod
2026-05-07: ffprobe 6.1.1 (apt install ffmpeg).

Usage:
    from sreda.services.audio_probe import ffprobe_duration, FfprobeError
    try:
        seconds = ffprobe_duration(audio_bytes)
    except FfprobeError as e:
        # ffprobe missing OR audio corrupted OR unsupported format
        # → reject voice with text fallback, не charge quota
        ...

Errors handling rationale:
- subprocess.TimeoutExpired — audio file might be malformed; raise
  FfprobeError, don't hang voice flow.
- non-zero exit code — unsupported format / corrupted bytes; raise.
- JSON parse error — ffprobe output unexpected; raise.
- duration field missing — same.

Все эти cases в voice flow → text fallback («не удалось обработать
голосовое»), без charging quota.
"""

from __future__ import annotations

import json
import subprocess


class FfprobeError(Exception):
    """Raised когда ffprobe failed extract duration. Caller should
    fallback к text reply, NOT charge voice quota."""


def ffprobe_duration(audio_bytes: bytes, *, timeout_sec: float = 10.0) -> float:
    """Return audio duration in seconds, или raise FfprobeError.

    Args:
        audio_bytes: raw audio bytes (OGG/Opus, MP3, WAV, MP4/AAC).
        timeout_sec: hard cap для ffprobe subprocess. Default 10s
            (voice files обычно <30s, ffprobe parses metadata only —
            actual decode не triggered).

    Returns:
        Duration в seconds, float.

    Raises:
        FfprobeError: ffprobe missing, audio corrupted, format
            unsupported, or output unparseable.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", "-i", "pipe:0"],
            input=audio_bytes,
            capture_output=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise FfprobeError(
            "ffprobe binary not found in PATH — install ffmpeg на VDS"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FfprobeError(
            f"ffprobe timed out after {timeout_sec}s (malformed audio?)"
        ) from exc

    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-200:]
        raise FfprobeError(
            f"ffprobe exited {proc.returncode}: {stderr_tail}"
        )

    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        duration = float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise FfprobeError(
            f"ffprobe output unparseable: {exc}"
        ) from exc

    if duration <= 0:
        raise FfprobeError(f"ffprobe returned non-positive duration: {duration}")
    return duration
