"""Atomic JSON read/write for cached stage artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import StageError


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically so a crash never leaves a half-written cache."""
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp, path)
    except OSError as exc:
        raise StageError(f"cannot write {path}: {exc}") from exc


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StageError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StageError(f"{path} is not valid JSON: {exc}") from exc
