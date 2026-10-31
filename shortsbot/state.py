"""Per-video working state stored in temp/<video_id>/state.json.

Every stage records its status here so stages can be re-run independently and
a later stage can refuse to start when its input is missing.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .errors import StageError

STATE_VERSION = 1

STAGE_COMMANDS = {
    "download": "python main.py download <url-or-path>",
    "audio": "python main.py download <url-or-path>",
    "transcribe": "python main.py transcribe <video_id>",
    "select": "python main.py select <video_id>",
    "render": "python main.py render <video_id> --clip <index>",
}


def source_id(source: str) -> str:
    """Stable short id for a URL or local path (used as the temp folder name)."""
    key = source.strip()
    if "://" in key:
        key = key.rstrip("/")
    else:
        candidate = Path(key).expanduser()
        try:
            key = os.path.normcase(str(candidate.resolve()))
        except OSError:
            key = os.path.normcase(str(candidate))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class State:
    video_id: str
    workdir: Path
    data: dict[str, Any] = field(default_factory=dict)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def for_source(cls, cfg: Config, source: str) -> "State":
        """Load or create the state for a URL / local file."""
        return cls.load(cfg, source_id(source), source=source)

    @classmethod
    def load(
        cls, cfg: Config, video_id: str, *, source: str | None = None
    ) -> "State":
        workdir = cfg.paths.temp / video_id
        path = workdir / "state.json"

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StageError(f"cannot read state file {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise StageError(f"state file {path} is not a JSON object")
        else:
            if source is None:
                raise StageError(
                    f"no work folder for '{video_id}' in {cfg.paths.temp}. "
                    f"Run 'python main.py download <url-or-path>' first, "
                    f"then use the id it prints."
                )
            data = {
                "version": STATE_VERSION,
                "video_id": video_id,
                "source": source.strip(),
                "created_at": _now(),
                "stages": {},
                "data": {},
            }
            try:
                workdir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StageError(f"cannot create work folder {workdir}: {exc}") from exc

        return cls(video_id=video_id, workdir=workdir, data=data)

    # -- persistence ------------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self.workdir / "state.json"

    def save(self) -> None:
        payload = json.dumps(self.data, indent=2, ensure_ascii=False)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise StageError(f"cannot write state file {self.path}: {exc}") from exc

    # -- stage bookkeeping -------------------------------------------------- #

    @property
    def source(self) -> str:
        return str(self.data.get("source", ""))

    def stage_status(self, stage: str) -> str | None:
        entry = self.data.get("stages", {}).get(stage)
        return entry.get("status") if isinstance(entry, dict) else None

    def is_done(self, stage: str) -> bool:
        return self.stage_status(stage) == "done"

    def mark_running(self, stage: str) -> None:
        self._set_stage(stage, {"status": "running", "at": _now()})

    def mark_done(self, stage: str, **info: Any) -> None:
        entry: dict[str, Any] = {"status": "done", "at": _now()}
        entry.update(info)
        self._set_stage(stage, entry)

    def mark_failed(self, stage: str, message: str) -> None:
        self._set_stage(stage, {"status": "failed", "at": _now(), "error": message})

    def _set_stage(self, stage: str, entry: dict[str, Any]) -> None:
        stages = self.data.setdefault("stages", {})
        stages[stage] = entry
        self.save()

    def require(self, stage: str) -> None:
        """Raise a helpful error when a prerequisite stage has not run."""
        if self.is_done(stage):
            return
        status = self.stage_status(stage)
        hint = STAGE_COMMANDS.get(stage, "")
        reason = f"is {status}" if status else "has not run"
        raise StageError(
            f"stage '{stage}' {reason} for {self.video_id}. "
            f"Run it first: {hint}"
        )

    # -- free-form values --------------------------------------------------- #

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get("data", {}).get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data.setdefault("data", {})[key] = value
        self.save()
