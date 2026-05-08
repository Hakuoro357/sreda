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
import math
import subprocess


class FfprobeError(Exception):
    """Raised когда ffprobe failed extract duration. Caller should
    fallback к text reply, NOT charge voice quota."""


# 2026-05-08 prod incident (tenant_max_142322319): MAX отправил
# OGG/Opus где `format.duration` не записан в container metadata.
# ffprobe вернул успешный exit code 0, но `data["format"]["duration"]`
# missing → KeyError → FfprobeError → юзер видел «не получилось
# обработать голосовое». TG voice работал (TG ставит duration);
# MAX-Web/Android — нет.
#
# Fallback chain:
#   1. format.duration (container metadata — TG style)
#   2. streams[*].duration (stream-level — некоторые MAX containers)
#   3. byte-size estimate (OGG/Opus voice ≈ 16 kbps = 2 KB/s)
#
# Codex r4 MAJOR: bitrate denominator 2000 (16 kbps) недооценивает
# duration для low-bitrate Opus (12 kbps). Real 45s @ 12 kbps =
# 67500 bytes → 33s estimate → undercharge. Снижено до 1500 bytes/sec
# (12 kbps) — typical MAX voice Opus rate. Even если real bitrate
# выше — мы получим conservative over-estimate, а не under-charge.
#
# Codex r4 MINOR: cap aligned с free-tier policy. Free tier ceiling
# 30s/voice (`SREDA_FREE_VOICE_SECONDS_DAILY=300` = 5 min/day, по
# воспроизведённому MAX-cap'у `_VOICE_MAX_BYTES`). Превышать смысла
# нет — sandard MAX voice уже отбрасывается на download stage если
# больше 1MB (~30s).
_VOICE_BITRATE_BYTES_PER_SEC = 1500  # 12 kbps OGG/Opus voice (conservative)
_BYTE_ESTIMATE_MAX_SEC = 30.0  # Free-tier per-voice ceiling


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
            unsupported, or all fallback paths failed.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             # 2026-05-08 fix: query format AND stream level. Some
             # MAX containers don't set format.duration but DO set
             # stream.duration. Single subprocess call, both checked.
             "-show_entries", "format=duration:stream=duration",
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
    except (json.JSONDecodeError, ValueError) as exc:
        raise FfprobeError(f"ffprobe output not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FfprobeError(f"ffprobe output not a dict: {type(data).__name__}")

    duration: float | None = None

    # Codex r4 MINOR: isfinite guard — protect от inf/nan, которые
    # формально проходили `d > 0` но ломали quota math downstream.

    # 1) format.duration (TG style — container metadata).
    fmt = data.get("format") or {}
    fmt_dur = fmt.get("duration") if isinstance(fmt, dict) else None
    if fmt_dur:
        try:
            d = float(fmt_dur)
            if math.isfinite(d) and d > 0:
                duration = d
        except (TypeError, ValueError):
            pass

    # 2) streams[*].duration (MAX-Web/Android style).
    if duration is None:
        streams = data.get("streams") or []
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                sd = stream.get("duration")
                if not sd:
                    continue
                try:
                    d = float(sd)
                    if math.isfinite(d) and d > 0:
                        duration = d
                        break
                except (TypeError, ValueError):
                    continue

    # 3) Byte-size estimate for typical voice OGG/Opus.
    if duration is None:
        size = len(audio_bytes)
        estimated = size / _VOICE_BITRATE_BYTES_PER_SEC if size else 0.0
        if estimated > 0:
            return min(estimated, _BYTE_ESTIMATE_MAX_SEC)
        raise FfprobeError(
            "ffprobe returned no duration in format or streams; "
            f"byte estimate also failed (size={size})"
        )

    return duration
