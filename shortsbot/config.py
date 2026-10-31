"""Load config.yaml into validated, frozen dataclasses.

Rules:
  * every tunable lives in config.yaml -- no paths are hardcoded in modules;
  * relative paths resolve against the directory holding config.yaml;
  * missing keys fall back to DEFAULTS; unknown keys are an error (typos fail loudly).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
COMPUTE_TYPES = ("int8", "int8_float32", "float32", "float16")
CROP_MODES = ("blur_pad", "center")
X264_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)

DEFAULTS: dict[str, Any] = {
    "logging": {
        "level": "INFO",
        "file": "temp/shortsbot.log",
    },
    "paths": {
        "downloads": "downloads",
        "temp": "temp",
        "output": "output",
    },
    "download": {
        "max_height": 1080,
        "cookies_from_browser": None,
    },
    "transcription": {
        "model_size": "small",
        "compute_type": "int8",
        "cpu_threads": 6,
        "language": None,
        "vad_filter": True,
        "beam_size": 5,
    },
    "selection": {
        "selector": "heuristic",
        "min_seconds": 20,
        "max_seconds": 60,
        "max_clips": 3,
        "keywords": [
            "mistake",
            "secret",
            "never",
            "biggest",
            "worst",
            "free",
            "money",
            "how to",
            "why",
            "what if",
            "truth",
            "nobody",
            "everyone",
            "finally",
        ],
        "weights": {
            "speech_density": 1.0,
            "keywords": 0.8,
            "hook": 1.0,
            "completeness": 0.6,
            "filler": 0.5,
            "silence": 0.5,
        },
    },
    "render": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "crop_mode": "blur_pad",
        "preset": "veryfast",
        "crf": 21,
        "audio_bitrate": "128k",
    },
    "captions": {
        "enabled": True,
        "font": "Arial",
        "font_size": 72,
        "max_words_per_line": 4,
        "margin_v": 380,
        "outline": 4,
        "bold": True,
    },
}


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str
    file: Path


@dataclass(frozen=True, slots=True)
class PathsConfig:
    downloads: Path
    temp: Path
    output: Path


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    max_height: int
    cookies_from_browser: str | None


@dataclass(frozen=True, slots=True)
class TranscriptionConfig:
    model_size: str
    compute_type: str
    cpu_threads: int
    language: str | None
    vad_filter: bool
    beam_size: int


@dataclass(frozen=True, slots=True)
class SelectionWeights:
    speech_density: float
    keywords: float
    hook: float
    completeness: float
    filler: float
    silence: float


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    selector: str
    min_seconds: float
    max_seconds: float
    max_clips: int
    keywords: tuple[str, ...]
    weights: SelectionWeights


@dataclass(frozen=True, slots=True)
class RenderConfig:
    width: int
    height: int
    fps: int
    crop_mode: str
    preset: str
    crf: int
    audio_bitrate: str


@dataclass(frozen=True, slots=True)
class CaptionsConfig:
    enabled: bool
    font: str
    font_size: int
    max_words_per_line: int
    margin_v: int
    outline: int
    bold: bool


@dataclass(frozen=True, slots=True)
class Config:
    config_path: Path
    root: Path
    logging: LoggingConfig
    paths: PathsConfig
    download: DownloadConfig
    transcription: TranscriptionConfig
    selection: SelectionConfig
    render: RenderConfig
    captions: CaptionsConfig


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_config(path: str | Path | None = None) -> Config:
    """Read, validate and freeze the configuration."""
    config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")

    _reject_unknown(raw, DEFAULTS)
    data = _deep_merge(DEFAULTS, raw)
    root = config_path.parent

    logging_cfg = _parse_logging(_section(data, "logging"), root)
    paths_cfg = _parse_paths(_section(data, "paths"), root)
    download_cfg = _parse_download(_section(data, "download"))
    transcription_cfg = _parse_transcription(_section(data, "transcription"))
    selection_cfg = _parse_selection(_section(data, "selection"))
    render_cfg = _parse_render(_section(data, "render"))
    captions_cfg = _parse_captions(_section(data, "captions"))

    _validate(selection_cfg, render_cfg, captions_cfg)

    return Config(
        config_path=config_path,
        root=root,
        logging=logging_cfg,
        paths=paths_cfg,
        download=download_cfg,
        transcription=transcription_cfg,
        selection=selection_cfg,
        render=render_cfg,
        captions=captions_cfg,
    )


def ensure_directories(cfg: Config) -> None:
    """Create the working directories declared in the config."""
    for path in (
        cfg.paths.downloads,
        cfg.paths.temp,
        cfg.paths.output,
        cfg.logging.file.parent,
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"cannot create directory {path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    return value


def _int(section: dict[str, Any], key: str, label: str) -> int:
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer (got {value!r})")
    return value


def _float(section: dict[str, Any], key: str, label: str) -> float:
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number (got {value!r})")
    return float(value)


def _bool(section: dict[str, Any], key: str, label: str) -> bool:
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be true or false (got {value!r})")
    return value


def _str(section: dict[str, Any], key: str, label: str) -> str:
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string (got {value!r})")
    return value.strip()


def _opt_str(section: dict[str, Any], key: str, label: str) -> str | None:
    value = section[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string or null (got {value!r})")
    value = value.strip()
    return value or None


def _choice(
    section: dict[str, Any], key: str, label: str, allowed: tuple[str, ...]
) -> str:
    value = _str(section, key, label)
    if value not in allowed:
        raise ConfigError(
            f"{label} must be one of {', '.join(allowed)} (got {value!r})"
        )
    return value


def _path(section: dict[str, Any], key: str, label: str, root: Path) -> Path:
    value = _str(section, key, label)
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path)


def _parse_logging(section: dict[str, Any], root: Path) -> LoggingConfig:
    level = _str(section, "level", "logging.level").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(
            "logging.level must be one of DEBUG, INFO, WARNING, ERROR "
            f"(got {level!r})"
        )
    return LoggingConfig(level=level, file=_path(section, "file", "logging.file", root))


def _parse_paths(section: dict[str, Any], root: Path) -> PathsConfig:
    return PathsConfig(
        downloads=_path(section, "downloads", "paths.downloads", root),
        temp=_path(section, "temp", "paths.temp", root),
        output=_path(section, "output", "paths.output", root),
    )


def _parse_download(section: dict[str, Any]) -> DownloadConfig:
    max_height = _int(section, "max_height", "download.max_height")
    if not 144 <= max_height <= 4320:
        raise ConfigError("download.max_height must be between 144 and 4320")
    return DownloadConfig(
        max_height=max_height,
        cookies_from_browser=_opt_str(
            section, "cookies_from_browser", "download.cookies_from_browser"
        ),
    )


def _parse_transcription(section: dict[str, Any]) -> TranscriptionConfig:
    cpu_threads = _int(section, "cpu_threads", "transcription.cpu_threads")
    if not 1 <= cpu_threads <= 32:
        raise ConfigError("transcription.cpu_threads must be between 1 and 32")
    beam_size = _int(section, "beam_size", "transcription.beam_size")
    if not 1 <= beam_size <= 10:
        raise ConfigError("transcription.beam_size must be between 1 and 10")
    return TranscriptionConfig(
        model_size=_choice(
            section, "model_size", "transcription.model_size", MODEL_SIZES
        ),
        compute_type=_choice(
            section, "compute_type", "transcription.compute_type", COMPUTE_TYPES
        ),
        cpu_threads=cpu_threads,
        language=_opt_str(section, "language", "transcription.language"),
        vad_filter=_bool(section, "vad_filter", "transcription.vad_filter"),
        beam_size=beam_size,
    )


def _parse_selection(section: dict[str, Any]) -> SelectionConfig:
    weights_raw = section["weights"]
    if not isinstance(weights_raw, dict):
        raise ConfigError("selection.weights must be a mapping")
    weights = SelectionWeights(
        speech_density=_float(weights_raw, "speech_density", "selection.weights.speech_density"),
        keywords=_float(weights_raw, "keywords", "selection.weights.keywords"),
        hook=_float(weights_raw, "hook", "selection.weights.hook"),
        completeness=_float(weights_raw, "completeness", "selection.weights.completeness"),
        filler=_float(weights_raw, "filler", "selection.weights.filler"),
        silence=_float(weights_raw, "silence", "selection.weights.silence"),
    )

    keywords_raw = section["keywords"]
    if not isinstance(keywords_raw, list) or not all(
        isinstance(item, str) for item in keywords_raw
    ):
        raise ConfigError("selection.keywords must be a list of strings")

    return SelectionConfig(
        selector=_str(section, "selector", "selection.selector"),
        min_seconds=_float(section, "min_seconds", "selection.min_seconds"),
        max_seconds=_float(section, "max_seconds", "selection.max_seconds"),
        max_clips=_int(section, "max_clips", "selection.max_clips"),
        keywords=tuple(k.lower() for k in keywords_raw),
        weights=weights,
    )


def _parse_render(section: dict[str, Any]) -> RenderConfig:
    return RenderConfig(
        width=_int(section, "width", "render.width"),
        height=_int(section, "height", "render.height"),
        fps=_int(section, "fps", "render.fps"),
        crop_mode=_choice(section, "crop_mode", "render.crop_mode", CROP_MODES),
        preset=_choice(section, "preset", "render.preset", X264_PRESETS),
        crf=_int(section, "crf", "render.crf"),
        audio_bitrate=_str(section, "audio_bitrate", "render.audio_bitrate"),
    )


def _parse_captions(section: dict[str, Any]) -> CaptionsConfig:
    return CaptionsConfig(
        enabled=_bool(section, "enabled", "captions.enabled"),
        font=_str(section, "font", "captions.font"),
        font_size=_int(section, "font_size", "captions.font_size"),
        max_words_per_line=_int(
            section, "max_words_per_line", "captions.max_words_per_line"
        ),
        margin_v=_int(section, "margin_v", "captions.margin_v"),
        outline=_int(section, "outline", "captions.outline"),
        bold=_bool(section, "bold", "captions.bold"),
    )


def _validate(
    selection: SelectionConfig, render: RenderConfig, captions: CaptionsConfig
) -> None:
    if selection.min_seconds < 5:
        raise ConfigError("selection.min_seconds must be at least 5")
    if selection.min_seconds >= selection.max_seconds:
        raise ConfigError(
            "selection.min_seconds must be smaller than selection.max_seconds"
        )
    if selection.max_clips < 1:
        raise ConfigError("selection.max_clips must be at least 1")
    for name, value in (
        ("speech_density", selection.weights.speech_density),
        ("keywords", selection.weights.keywords),
        ("hook", selection.weights.hook),
        ("completeness", selection.weights.completeness),
        ("filler", selection.weights.filler),
        ("silence", selection.weights.silence),
    ):
        if value < 0:
            raise ConfigError(f"selection.weights.{name} must not be negative")

    if render.width <= 0 or render.height <= 0:
        raise ConfigError("render.width and render.height must be positive")
    if render.width % 2 or render.height % 2:
        raise ConfigError("render.width and render.height must be even (H.264 requirement)")
    if not 1 <= render.fps <= 120:
        raise ConfigError("render.fps must be between 1 and 120")
    if not 0 <= render.crf <= 51:
        raise ConfigError("render.crf must be between 0 and 51")

    if captions.font_size < 8:
        raise ConfigError("captions.font_size must be at least 8")
    if captions.max_words_per_line < 1:
        raise ConfigError("captions.max_words_per_line must be at least 1")
    if captions.margin_v < 0:
        raise ConfigError("captions.margin_v must not be negative")


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _reject_unknown(
    actual: dict[str, Any], template: dict[str, Any], prefix: str = ""
) -> None:
    for key, value in actual.items():
        if key not in template:
            raise ConfigError(f"unknown config key: {prefix}{key}")
        if isinstance(value, dict) and isinstance(template[key], dict):
            _reject_unknown(value, template[key], prefix=f"{prefix}{key}.")
