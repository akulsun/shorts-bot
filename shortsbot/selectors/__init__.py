"""Clip selectors and the swap point used by `main.py select`."""

from __future__ import annotations

from ..config import ConfigError
from .base import SelectionOutcome, Selector
from .gemini import GeminiSelector
from .heuristic import HeuristicSelector
from .hybrid import HybridSelector

CLIPS_FILENAME = "clips.json"

_REGISTRY: dict[str, type] = {
    "gemini": GeminiSelector,
    "heuristic": HeuristicSelector,
    "hybrid": HybridSelector,
}


def load_selector(name: str) -> Selector:
    """Instantiate the selector named in selection.selector."""
    key = (name or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ConfigError(
            f"unknown selector '{name}' in selection.selector. "
            f"Available: {', '.join(sorted(_REGISTRY))}"
        )
    return factory()


__all__ = [
    "CLIPS_FILENAME",
    "GeminiSelector",
    "HeuristicSelector",
    "HybridSelector",
    "Selector",
    "SelectionOutcome",
    "load_selector",
]
