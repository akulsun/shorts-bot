"""Stage 5: download -> transcribe -> select -> render in one command.

Each stage is the same module used by the individual commands, so cached
artifacts are reused automatically. Rendering is isolated per clip: one bad
clip is reported and the rest still render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import audio, download, render, selection, transcribe
from .config import Config
from .formatting import format_size
from .models import Clip
from .state import State

log = logging.getLogger(__name__)


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
) -> RunResult:
    """Run every stage for one source, reusing whatever is already cached."""
    state = State.for_source(cfg, source)

    log.info("[1/4] download: %s", state.video_id)
    info = download.resolve_source(cfg, state)
    audio.extract_audio(state, info)

    log.info("[2/4] transcribe")
    transcribe.transcribe(cfg, state)

    log.info("[3/4] select")
    chosen = selection.select_clips(cfg, state, max_clips=max_clips)

    total = len(chosen.outcome.clips)
    log.info("[4/4] render: %d clip(s)", total)
    clips: list[ClipRun] = []
    for position, clip in enumerate(chosen.outcome.clips, start=1):
        log.info(
            "[4/4] render clip #%d (%d/%d)", clip.index, position, total
        )
        cached = render.cached_render(cfg, state, clip.index)
        if cached is not None:
            clips.append(ClipRun(clip=clip, result=cached))
            continue
        try:
            result = render.render_clip(cfg, state, clip.index)
        except Exception as exc:  # noqa: BLE001 - one clip must not stop the batch
            log.error("clip #%d failed: %s", clip.index, exc)
            clips.append(ClipRun(clip=clip, error=str(exc)))
            continue
        clips.append(ClipRun(clip=clip, result=result))

    result = RunResult(
        state=state,
        source=info,
        selection=chosen,
        clips=tuple(clips),
    )

    if clean:
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

    return result


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
