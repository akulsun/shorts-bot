"""Selector interface: the swap point for clip picking.

Any implementation (heuristic today, an LLM later) takes a Transcript plus the
selection settings and returns clips ordered by descending score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..config import SelectionConfig
from ..models import Clip, Transcript

if TYPE_CHECKING:
    from ..state import State


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    """Selected clips plus a human-readable reason when the list is empty."""

    clips: tuple[Clip, ...]
    reason: str = ""


class Selector(Protocol):
    def select(
        self,
        transcript: Transcript,
        cfg: SelectionConfig,
        state: State | None = None,
    ) -> SelectionOutcome:
        ...
