"""Display-free helpers for the desktop GUI.

This module deliberately imports no Tk/CustomTkinter, so settings and queue
behaviour can be unit tested on a headless machine.
"""

from __future__ import annotations

import logging
import math
import queue
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import (
    CROP_MODES,
    MODEL_SIZES,
    Config,
    save_config_values,
)
from .download import is_url
from .errors import ConfigError


# --------------------------------------------------------------------------- #
# Queue messages
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LogEvent:
    text: str
    level: str


@dataclass(frozen=True, slots=True)
class JobSucceeded:
    result: Any


@dataclass(frozen=True, slots=True)
class JobFailed:
    message: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class JobCancelled:
    pass


class QueueLogHandler(logging.Handler):
    """Forward formatted log records to the GUI queue (thread-safe)."""

    def __init__(
        self,
        target: queue.Queue[Any],
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level)
        self._target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = LogEvent(text=self.format(record), level=record.levelname)
            self._target.put_nowait(event)
        except Exception:  # noqa: BLE001 - logging must never crash the app
            self.handleError(record)


def drain_queue(
    source: queue.Queue[Any], limit: int = 250
) -> list[Any]:
    """Take a bounded batch so one busy queue cannot freeze the UI loop."""
    items: list[Any] = []
    for _ in range(max(0, int(limit))):
        try:
            items.append(source.get_nowait())
        except queue.Empty:
            break
    return items


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GuiSettings:
    max_clips: int
    model_size: str
    crop_mode: str
    min_seconds: float
    max_seconds: float

    @classmethod
    def from_config(cls, cfg: Config) -> "GuiSettings":
        return cls(
            max_clips=cfg.selection.max_clips,
            model_size=cfg.transcription.model_size,
            crop_mode=cfg.render.crop_mode,
            min_seconds=cfg.selection.min_seconds,
            max_seconds=cfg.selection.max_seconds,
        )

    @classmethod
    def from_form(
        cls,
        *,
        max_clips: str,
        model_size: str,
        crop_mode: str,
        min_seconds: str,
        max_seconds: str,
    ) -> "GuiSettings":
        settings = cls(
            max_clips=_whole_number(max_clips, "Clips"),
            model_size=str(model_size).strip(),
            crop_mode=str(crop_mode).strip(),
            min_seconds=_number(min_seconds, "Min seconds"),
            max_seconds=_number(max_seconds, "Max seconds"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.max_clips < 1:
            raise ConfigError("Clips must be at least 1.")
        if self.model_size not in MODEL_SIZES:
            raise ConfigError(
                "Whisper model must be one of "
                f"{', '.join(MODEL_SIZES)} (got {self.model_size!r})."
            )
        if self.crop_mode not in CROP_MODES:
            raise ConfigError(
                "Crop mode must be one of "
                f"{', '.join(CROP_MODES)} (got {self.crop_mode!r})."
            )
        if self.min_seconds < 5:
            raise ConfigError("Min seconds must be at least 5.")
        if self.min_seconds >= self.max_seconds:
            raise ConfigError(
                "Min seconds must be smaller than Max seconds."
            )

    def apply(self, cfg: Config) -> Config:
        """Return an in-memory Config for this run; nothing is written."""
        return replace(
            cfg,
            selection=replace(
                cfg.selection,
                max_clips=self.max_clips,
                min_seconds=self.min_seconds,
                max_seconds=self.max_seconds,
            ),
            transcription=replace(
                cfg.transcription, model_size=self.model_size
            ),
            render=replace(cfg.render, crop_mode=self.crop_mode),
        )

    def as_updates(self) -> dict[str, Any]:
        return {
            "selection.max_clips": self.max_clips,
            "selection.min_seconds": self.min_seconds,
            "selection.max_seconds": self.max_seconds,
            "transcription.model_size": self.model_size,
            "render.crop_mode": self.crop_mode,
        }


def save_settings(settings: GuiSettings, cfg: Config) -> Config:
    """Validate, then atomically write the five GUI-controlled settings."""
    settings.validate()
    return save_config_values(cfg.config_path, settings.as_updates())


def validate_source(text: str) -> str:
    """Normalise a URL / local path and fail early for a missing file."""
    source = str(text).strip().strip('"').strip("'")
    if not source:
        raise ConfigError("Enter a YouTube URL or choose a local video file.")
    if is_url(source):
        return source

    path = Path(source).expanduser()
    if not path.exists():
        raise ConfigError(f"File not found: {path}")
    if not path.is_file():
        raise ConfigError(f"Not a file: {path}")
    return str(path.resolve())


def _whole_number(value: str, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{label} must be a whole number.") from None


def _number(value: str, label: str) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise ConfigError(f"{label} must be a finite number.")
    return number
