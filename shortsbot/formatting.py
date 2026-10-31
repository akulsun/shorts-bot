"""Small human-readable helpers for logs and CLI summaries."""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Seconds -> 'm:ss' or 'h:mm:ss'."""
    total = int(round(max(0.0, seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_size(num_bytes: int) -> str:
    """Bytes -> '12.3 MB'."""
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
