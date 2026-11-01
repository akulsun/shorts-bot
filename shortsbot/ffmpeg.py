"""Thin wrapper around the ffmpeg / ffprobe binaries.

All subprocess handling lives here so stages never build shell commands and
never have to interpret raw stderr.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import CancelledError, FFmpegError, DependencyError

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
    cwd: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [exe, "-hide_banner"]
    if nostdin:
        # ffmpeg-only: stops ffmpeg from eating console input during long jobs.
        command.append("-nostdin")
    command.extend(args)

    if cancel_event is None:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=_CREATE_NO_WINDOW,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            raise DependencyError(
                f"{exe} not found on PATH (needed for: {desc}). "
                f"Install it and make sure it is on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError(f"{desc} timed out after {timeout}s") from exc

    popen_kwargs: dict[str, object] = {}
    if os.name != "nt" and cancel_event is not None:
        # Lets the whole ffmpeg tree be signalled on cancellation.
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            cwd=str(cwd) if cwd is not None else None,
            **popen_kwargs,  # type: ignore[arg-type]
        )
    except FileNotFoundError as exc:
        raise DependencyError(
            f"{exe} not found on PATH (needed for: {desc}). "
            f"Install it and make sure it is on PATH."
        ) from exc

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        wait = 0.2
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                _drain(process)
                raise FFmpegError(f"{desc} timed out after {timeout}s")
            wait = min(wait, remaining)

        try:
            stdout, stderr = process.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                _stop_process(process)
                _drain(process)
                raise CancelledError(f"{desc} cancelled")
            continue
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a child and every process it spawned.

    On Windows, Chocolatey's `ffmpeg.exe` is a shim that launches the real
    ffmpeg.exe. Terminating only the shim leaves the render running and keeps
    the stdout/stderr pipes open, which makes cancellation wait for the whole
    render. `taskkill /T` kills the real child too. On POSIX the child was
    started in its own session, so signalling its process group has the same
    effect.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                timeout=5,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.terminate()
    except OSError:
        pass


def _stop_process(process: subprocess.Popen[str]) -> None:
    """Terminate a child process tree, escalating to kill if it does not exit."""
    if process.poll() is not None:
        return
    _terminate_tree(process)
    try:
        process.wait(timeout=3)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass


def _drain(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect remaining output after a stop without ever hanging the UI."""
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        return "", ""
    return stdout or "", stderr or ""


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def run_ffmpeg(
    args: Sequence[str],
    *,
    desc: str,
    timeout: float | None = None,
    cwd: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Run ffmpeg, raising FFmpegError with the end of stderr on failure.

    `cwd` lets callers pass filtergraph paths relative to a folder whose name
    may contain spaces or apostrophes; those characters cannot be escaped
    reliably inside an ffmpeg filter option.

    When `cancel_event` is supplied, the child process is polled and terminated
    as soon as the event is set; CancelledError is raised instead of returning.
    """
    proc = _run(
        "ffmpeg",
        ["-y", "-loglevel", "error", *args],
        desc=desc,
        timeout=timeout,
        nostdin=True,
        cwd=cwd,
        cancel_event=cancel_event,
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
            str(Path(path).resolve()),
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
