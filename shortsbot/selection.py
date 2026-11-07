"""Shared clip-selection stage used by `select` and the end-to-end `run`."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from . import jsonio, transcribe
from .config import Config, SelectionConfig
from .selectors import CLIPS_FILENAME, SelectionOutcome, load_selector
from .state import State


@dataclass(frozen=True, slots=True)
class SelectionResult:
    outcome: SelectionOutcome
    selection: SelectionConfig
    path: Path


def select_clips(
    cfg: Config, state: State, *, max_clips: int | None = None
) -> SelectionResult:
    """Select clips for this work folder and overwrite clips.json."""
    state.require("transcribe")
    transcript = transcribe.load_transcript(state)

    selection = cfg.selection
    if max_clips is not None:
        selection = replace(selection, max_clips=max_clips)

    outcome = load_selector(selection.selector).select(
        transcript, selection, state=state
    )

    path = state.workdir / CLIPS_FILENAME
    jsonio.write_json(
        path,
        {
            "selector": selection.selector,
            "settings": {
                "min_seconds": selection.min_seconds,
                "max_seconds": selection.max_seconds,
                "max_clips": selection.max_clips,
            },
            "reason": outcome.reason,
            "clips": [clip.to_dict() for clip in outcome.clips],
        },
    )
    state.mark_done(
        "select",
        path=str(path),
        clips=len(outcome.clips),
        selector=selection.selector,
    )
    return SelectionResult(outcome=outcome, selection=selection, path=path)
