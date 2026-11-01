"""Stage 5: download -> transcribe -> select -> render in one command.

Each stage is the same module used by the individual commands, so cached
artifacts are reused automatically. Rendering is isolated per clip: one bad
clip is reported and the rest still render.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import audio, download, render, selection, transcribe
from .config import Config
from .errors import CancelledError
from .formatting import format_size
from .models import Clip
from .state import State

log = logging.getLogger(__name__)

_DOWNLOAD_END = 0.10
_TRANSCRIBE_END = 0.65
_SELECT_END = 0.70
_RENDER_END = 1.00


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Overall progress update for the GUI (and any future caller)."""

    stage: str
    fraction: float
    message: str


@dataclass(frozen=True, slots=True)
class ClipRun:
    clip: Clip
    result: render.RenderResult | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class RunResult:
    state: State
    source: download.SourceInfo
    selection: selection.SelectionResult
    clips: tuple[ClipRun, ...]
    removed: tuple[tuple[Path, int], ...] = ()

    @property
    def successes(self) -> tuple[ClipRun, ...]:
        return tuple(item for item in self.clips if item.result is not None)

    @property
    def failures(self) -> tuple[ClipRun, ...]:
        return tuple(item for item in self.clips if item.result is None)

    @property
    def return_code(self) -> int:
        return 1 if self.failures else 0


def run_pipeline(
    cfg: Config,
    source: str,
    *,
    max_clips: int | None = None,
    clean: bool = False,
    progress: Callable[[ProgressEvent], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """Run every stage for one source, reusing whatever is already cached.

    `progress` and `cancel_event` are optional, so the CLI path is unchanged.
    Cancellation is cooperative: checks happen between stages/clips and at the
    safe points exposed by each stage.
    """
    _check_cancel(cancel_event)
    state = State.for_source(cfg, source)

    _emit(progress, "download", 0.0, "Downloading source...")
    log.info("[1/4] download: %s", state.video_id)
    info = download.resolve_source(
        cfg,
        state,
        on_progress=lambda fraction: _emit(
            progress,
            "download",
            _DOWNLOAD_END * fraction,
            "Downloading source...",
        ),
        cancel_event=cancel_event,
    )
    _check_cancel(cancel_event)
    audio.extract_audio(state, info, cancel_event=cancel_event)
    _check_cancel(cancel_event)
    _emit(progress, "download", _DOWNLOAD_END, "Download complete")

    log.info("[2/4] transcribe")
    _emit(progress, "transcribe", _DOWNLOAD_END, "Transcribing audio...")
    transcribe.transcribe(
        cfg,
        state,
        on_progress=lambda fraction: _emit(
            progress,
            "transcribe",
            _DOWNLOAD_END + (_TRANSCRIBE_END - _DOWNLOAD_END) * fraction,
            "Transcribing audio...",
        ),
        cancel_event=cancel_event,
    )
    _check_cancel(cancel_event)
    _emit(progress, "transcribe", _TRANSCRIBE_END, "Transcription complete")

    log.info("[3/4] select")
    _emit(progress, "select", _TRANSCRIBE_END, "Selecting clips...")
    chosen = selection.select_clips(cfg, state, max_clips=max_clips)
    _check_cancel(cancel_event)
    _emit(progress, "select", _SELECT_END, "Clip selection complete")

    total = len(chosen.outcome.clips)
    log.info("[4/4] render: %d clip(s)", total)
    _emit(progress, "render", _SELECT_END, f"Rendering {total} clip(s)...")
    clips: list[ClipRun] = []
    for position, clip in enumerate(chosen.outcome.clips, start=1):
        _check_cancel(cancel_event)
        _emit(
            progress,
            "render",
            _SELECT_END
            + (_RENDER_END - _SELECT_END) * ((position - 1) / total),
            f"Rendering clip #{clip.index} ({position}/{total})...",
        )
        log.info(
            "[4/4] render clip #%d (%d/%d)", clip.index, position, total
        )
        cached = render.cached_render(cfg, state, clip.index)
        if cached is not None:
            clips.append(ClipRun(clip=clip, result=cached))
            _emit(
                progress,
                "render",
                _SELECT_END + (_RENDER_END - _SELECT_END) * (position / total),
                f"Rendered clip #{clip.index} ({position}/{total})",
            )
            continue
        try:
            result = render.render_clip(
                cfg, state, clip.index, cancel_event=cancel_event
            )
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one clip must not stop the batch
            log.error("clip #%d failed: %s", clip.index, exc)
            clips.append(ClipRun(clip=clip, error=str(exc)))
            _emit(
                progress,
                "render",
                _SELECT_END + (_RENDER_END - _SELECT_END) * (position / total),
                f"Clip #{clip.index} failed ({position}/{total})",
            )
            continue
        clips.append(ClipRun(clip=clip, result=result))
        _emit(
            progress,
            "render",
            _SELECT_END + (_RENDER_END - _SELECT_END) * (position / total),
            f"Rendered clip #{clip.index} ({position}/{total})",
        )

    _check_cancel(cancel_event)

    result = RunResult(
        state=state,
        source=info,
        selection=chosen,
        clips=tuple(clips),
    )

    if clean:
        _check_cancel(cancel_event)
        if result.successes and not result.failures:
            removed = _clean_temp(state)
            result = RunResult(
                state=state,
                source=info,
                selection=chosen,
                clips=result.clips,
                removed=removed,
            )
        else:
            log.warning(
                "temp files kept: the run did not render every selected clip"
            )

    _emit(progress, "render", _RENDER_END, "Done")
    return result


def _emit(
    progress: Callable[[ProgressEvent], None] | None,
    stage: str,
    fraction: float,
    message: str,
) -> None:
    if progress is None:
        return
    event = ProgressEvent(
        stage=stage,
        fraction=max(0.0, min(1.0, float(fraction))),
        message=message,
    )
    try:
        progress(event)
    except Exception:  # noqa: BLE001 - UI callbacks must not break a run
        log.debug("progress callback failed", exc_info=True)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("run cancelled")


# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #


def _clean_temp(state: State) -> tuple[tuple[Path, int], ...]:
    """Delete the downloaded source and WAV, but never a user's local video."""
    workdir = state.workdir.resolve()
    candidates: list[Path] = []

    info = state.get("source_info")
    if isinstance(info, dict) and info.get("path"):
        candidates.append(Path(str(info["path"])))
    audio_path = state.get("audio_path")
    if audio_path:
        candidates.append(Path(str(audio_path)))

    removed: list[tuple[Path, int]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
            if path in seen or not path.is_relative_to(workdir):
                continue
            seen.add(path)
            if not path.exists():
                continue
            size = path.stat().st_size
            path.unlink()
        except OSError as exc:
            log.warning("could not remove %s: %s", candidate, exc)
            continue
        removed.append((path, size))
        log.info("removed %s (%s)", path.name, format_size(size))

    if removed:
        state.set("cleaned", [str(path) for path, _ in removed])
    return tuple(removed)
