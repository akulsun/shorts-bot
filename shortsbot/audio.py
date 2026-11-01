"""Stage 1b: extract a 16 kHz mono WAV for whisper.

This is the single audio contract of the pipeline: everything downstream
(transcription, caption timing) assumes 16 kHz mono PCM s16le.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .download import SourceInfo
from .errors import FFmpegError
from .ffmpeg import probe, run_ffmpeg
from .formatting import format_duration, format_size
from .state import State

log = logging.getLogger(__name__)

AUDIO_FILENAME = "audio.wav"
MIN_PLAUSIBLE_BYTES = 1024


def extract_audio(
    state: State,
    source: SourceInfo,
    *,
    force: bool = False,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Produce temp/<video_id>/audio.wav, reusing a previous result if valid."""
    output = state.workdir / AUDIO_FILENAME

    if not force and _cached_audio(state, output):
        log.info("using cached audio: %s", output)
        return output

    state.mark_running("audio")
    try:
        _run_extraction(source.path, output, cancel_event=cancel_event)
        duration = _verify(output, expected=source.duration)
    except Exception as exc:
        state.mark_failed("audio", str(exc))
        raise

    state.set("audio_path", str(output))
    state.mark_done(
        "audio",
        path=str(output),
        duration=round(duration, 3),
        bytes=output.stat().st_size,
    )
    log.info(
        "audio ready: %s (16 kHz mono, %s, %s)",
        output.name,
        format_duration(duration),
        format_size(output.stat().st_size),
    )
    return output


def _cached_audio(state: State, output: Path) -> bool:
    if not state.is_done("audio"):
        return False
    if not output.exists():
        log.warning("cached audio is gone from disk; extracting again")
        return False
    if output.stat().st_size < MIN_PLAUSIBLE_BYTES:
        log.warning("cached audio looks empty; extracting again")
        return False
    return True


def _run_extraction(
    video: Path,
    output: Path,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    log.info("extracting audio from %s", video.name)
    run_ffmpeg(
        [
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        desc=f"extracting audio from {video.name}",
        cancel_event=cancel_event,
    )


def _verify(path: Path, *, expected: float) -> float:
    """Sanity-check the produced WAV; returns its duration in seconds."""
    if not path.exists() or path.stat().st_size < MIN_PLAUSIBLE_BYTES:
        raise FFmpegError("audio extraction produced an empty file")

    media = probe(path)
    if not media.has_audio:
        raise FFmpegError(f"{path.name} contains no audio after extraction")
    if media.duration <= 0:
        raise FFmpegError(f"{path.name} reports a zero duration")

    if expected > 0:
        drift = abs(media.duration - expected)
        if drift > max(5.0, expected * 0.02):
            log.warning(
                "audio is %s but the video is %s; the source may be truncated",
                format_duration(media.duration),
                format_duration(expected),
            )
    return media.duration
