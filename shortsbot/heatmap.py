"""YouTube replay heatmap scoring.

Loads crowdsourced viewer replay data extracted by yt-dlp to identify
parts of the video that viewers watched repeatedly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import jsonio

log = logging.getLogger(__name__)

HEATMAP_FILENAME = "heatmap.json"


def load_heatmap(workdir: Path | str | None) -> list[dict[str, Any]] | None:
    """Load heatmap.json from the video work directory if present."""
    if not workdir:
        return None
    path = Path(workdir) / HEATMAP_FILENAME
    if not path.exists():
        return None
    try:
        data = jsonio.read_json(path)
        if isinstance(data, list) and data:
            return data
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
    return None


def score_heatmap_window(
    heatmap: list[dict[str, Any]] | None, start: float, end: float
) -> tuple[float, float, float]:
    """Return (peak_value, avg_value, normalized_score) in [0.0, 1.0] for [start, end].

    If no heatmap data is available, returns (0.5, 0.5, 0.5).
    """
    if not heatmap or end <= start:
        return 0.5, 0.5, 0.5

    overlapping: list[float] = []
    for item in heatmap:
        if not isinstance(item, dict):
            continue
        try:
            item_start = float(item.get("start_time", 0.0))
            item_end = float(item.get("end_time", 0.0))
            value = float(item.get("value", 0.0))
            if item_end > start and item_start < end:
                overlapping.append(value)
        except (TypeError, ValueError):
            continue

    if not overlapping:
        return 0.5, 0.5, 0.5

    peak_val = max(overlapping)
    avg_val = sum(overlapping) / len(overlapping)

    # In YouTube heatmap, values are typically normalized 0.0 to 1.0 (or 0 to 100).
    if peak_val > 1.0:
        peak_val /= 100.0
        avg_val /= 100.0

    peak_norm = min(1.0, max(0.0, peak_val))
    avg_norm = min(1.0, max(0.0, avg_val))
    score = 0.65 * peak_norm + 0.35 * avg_norm
    return round(peak_norm, 4), round(avg_norm, 4), round(float(score), 4)
