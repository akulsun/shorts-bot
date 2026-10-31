"""Thin wrapper around the ffmpeg / ffprobe binaries.

All subprocess handling lives here so stages never build shell commands and
never have to interpret raw stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import FFmpegError, DependencyError

_STDERR_TAIL_LINES = 25
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def _run(
    exe: str,
    args: Sequence[str],
    *,
    desc: str,
    timeout: float | None = None,
    nostdin: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [exe, "-hide_banner"]
    if nostdin:
        # ffmpeg-only: stops ffmpeg from eating console input during long jobs.
        command.append("-nostdin")
    command.extend(args)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise DependencyError(
            f"{exe} not found on PATH (needed for: {desc}). "
            f"Install it and make sure it is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{desc} timed out after {timeout}s") from exc


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def run_ffmpeg(
    args: Sequence[str],
    *,
    desc: str,
    timeout: float | None = None,
) -> None:
    """Run ffmpeg, raising FFmpegError with the end of stderr on failure."""
    proc = _run(
        "ffmpeg", ["-y", "-loglevel", "error", *args], desc=desc, timeout=timeout, nostdin=True
    )
    if proc.returncode != 0:
        raise FFmpegError(
            f"{desc} failed (ffmpeg exit {proc.returncode}):\n{_tail(proc.stderr)}"
        )


def ffmpeg_output(args: Sequence[str], *, desc: str = "ffmpeg") -> str:
    """Run ffmpeg and return combined stdout+stderr (for capability queries)."""
    proc = _run("ffmpeg", list(args), desc=desc, nostdin=True)
    if proc.returncode != 0:
        raise FFmpegError(
            f"{desc} failed (ffmpeg exit {proc.returncode}):\n{_tail(proc.stderr)}"
        )
    return proc.stdout + proc.stderr


def ffmpeg_version() -> str:
    """First line of `ffmpeg -version`, e.g. 'ffmpeg version 9.0.2-...'."""
    output = ffmpeg_output(["-version"])
    return output.splitlines()[0].strip() if output.strip() else "unknown"


def has_encoder(name: str) -> bool:
    return _has_list_entry(ffmpeg_output(["-encoders"]), name)


def has_filter(name: str) -> bool:
    return _has_list_entry(ffmpeg_output(["-filters"]), name)


def _has_list_entry(listing: str, name: str) -> bool:
    for line in listing.splitlines():
        parts = line.split()
        # Rows look like: " V....D libx264   H.264 ..." / " T.. ass  V->V ..."
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False


def probe(path: Path) -> MediaInfo:
    """Read duration, dimensions, fps and audio presence from a media file."""
    proc = _run(
        "ffprobe",
        [
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        desc=f"probing {Path(path).name}",
        timeout=120,
    )
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffprobe failed for {path} (exit {proc.returncode}):\n{_tail(proc.stderr)}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if video is None and not has_audio:
        raise FFmpegError(f"{path} contains no audio or video streams")

    duration = _first_float(
        data.get("format", {}).get("duration"),
        video.get("duration") if video else None,
        default=0.0,
    )
    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0) if video else 0,
        height=int(video.get("height") or 0) if video else 0,
        fps=_parse_rate(video.get("r_frame_rate")) if video else 0.0,
        has_audio=has_audio,
    )


def escape_filter_path(path: Path) -> str:
    """Format an absolute path for use inside an ffmpeg filtergraph option.

    Windows paths must use forward slashes and an escaped drive colon, e.g.
    'C\\:/Users/me/temp/clip.ass'. Callers wrap the result in single quotes:
    ass=filename='<escaped>'.
    """
    text = Path(path).resolve().as_posix()
    text = text.replace("\\", "/")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    return text


def _parse_rate(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    if "/" in value:
        numerator, _, denominator = value.partition("/")
        try:
            den = float(denominator)
            return round(float(numerator) / den, 3) if den else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(value), 3)
    except ValueError:
        return 0.0


def _first_float(*values: object, default: float) -> float:
    for value in values:
        try:
            if value is not None:
                return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return default
