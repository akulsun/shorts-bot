"""Logging setup: readable console output plus a persistent log file."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONSOLE_FORMAT = "%(asctime)s  %(levelname)-5s  %(message)s"
_FILE_FORMAT = "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure the root logger once per process."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    resolved = _resolve_level(level)
    root.setLevel(resolved)

    # Windows consoles may be cp1252; video titles can contain any unicode.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError as exc:
            root.warning("could not open log file %s: %s", log_file, exc)
        else:
            file_handler.setFormatter(
                logging.Formatter(_FILE_FORMAT, "%Y-%m-%d %H:%M:%S")
            )
            root.addHandler(file_handler)

    # Keep third-party chatter out of the console.
    for noisy in (
        "urllib3",
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "filelock",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)


def _resolve_level(level: str) -> int:
    value = logging.getLevelName(str(level).upper())
    return value if isinstance(value, int) else logging.INFO
