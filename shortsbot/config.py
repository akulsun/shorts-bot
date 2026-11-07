"""Load config.yaml into validated, frozen dataclasses.

Rules:
  * every tunable lives in config.yaml -- no paths are hardcoded in modules;
  * relative paths resolve against the directory holding config.yaml;
  * missing keys fall back to DEFAULTS; unknown keys are an error (typos fail loudly).
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
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
        "max_duration": 7200,
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
        "selector": "hybrid",
        "min_seconds": 20,
        "max_seconds": 60,
        "max_clips": 3,
        "use_audio_energy": True,
        "use_youtube_heatmap": True,
        "api_key": None,
        "llm_model": "gemini-3.8-flash",
        "llm_api_key_env": "GEMINI_API_KEY",
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
            "audio_energy": 1.2,
            "heatmap": 1.5,
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
    max_duration: float
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
    audio_energy: float = 1.2
    heatmap: float = 1.5


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    selector: str
    min_seconds: float
    max_seconds: float
    max_clips: int
    keywords: tuple[str, ...]
    weights: SelectionWeights
    use_audio_energy: bool = True
    use_youtube_heatmap: bool = True
    api_key: str | None = None
    llm_model: str = "gemini-3.8-flash"
    llm_api_key_env: str = "GEMINI_API_KEY"


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
    from .env import load_env_file

    config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    load_env_file(config_path.parent / ".env")

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


def save_config_values(path: str | Path, values: Mapping[str, Any]) -> Config:
    """Update dotted scalar keys in a YAML config without losing comments.

    The candidate is written to a temporary file in the same folder and passed
    through `load_config()` before the real file is replaced.  Any validation
    error therefore leaves the original config completely untouched.

    Only flat `section.key` paths are accepted, e.g.
    `{"selection.max_clips": 5, "transcription.model_size": "small"}`.
    """
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        original = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc

    lines = original.splitlines()
    for dotted, value in values.items():
        section, separator, key = str(dotted).partition(".")
        if not separator or not section or not key or "." in key:
            raise ConfigError(
                f"settings key must look like section.key (got {dotted!r})"
            )
        lines = _set_yaml_scalar(lines, section, key, _scalar_text(value))

    candidate = "\n".join(lines).rstrip() + "\n"
    temp_path = config_path.with_name(f".{config_path.name}.tmp")
    try:
        temp_path.write_text(candidate, encoding="utf-8", newline="\n")
        load_config(temp_path)
    except ConfigError:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise ConfigError(f"cannot save {config_path}: {exc}") from exc

    try:
        os.replace(temp_path, config_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise ConfigError(f"cannot write {config_path}: {exc}") from exc
    return load_config(config_path)


def _set_yaml_scalar(
    lines: list[str], section: str, key: str, value: str
) -> list[str]:
    """Return a copy of `lines` with one `section.key` scalar changed."""
    result = list(lines)
    section_pattern = re.compile(
        rf"^{re.escape(section)}:\s*(?:#.*)?$"
    )
    start = next(
        (index for index, line in enumerate(result) if section_pattern.match(line)),
        None,
    )
    if start is None:
        if result and result[-1].strip():
            result.append("")
        result.extend([f"{section}:", f"  {key}: {value}"])
        return result

    end = len(result)
    for index in range(start + 1, len(result)):
        if re.match(r"^[^\s#][^:]*:\s*(?:#.*)?$", result[index]):
            end = index
            break

    key_pattern = re.compile(
        rf"^(\s*){re.escape(key)}:(\s*)(.*?)(\s+#.*)?$"
    )
    for index in range(start + 1, end):
        match = key_pattern.match(result[index])
        if match:
            indent = match.group(1) or "  "
            comment = match.group(4) or ""
            result[index] = f"{indent}{key}: {value}{comment}"
            return result

    insert_at = end
    while insert_at > start + 1 and not result[insert_at - 1].strip():
        insert_at -= 1
    result.insert(insert_at, f"  {key}: {value}")
    return result


def _scalar_text(value: Any) -> str:
    """Render one value in the flat YAML style used by config.yaml."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ConfigError("config values cannot contain line breaks")
    dumped = yaml.safe_dump(
        text, default_flow_style=True, allow_unicode=True
    ).splitlines()
    return dumped[0].strip() if dumped else "''"


def ensure_directories(cfg: Config) -> None:
    """Create the working directories declared in the config."""
    paths = getattr(cfg, "paths", None)
    if paths is None:
        return
    logging_cfg = getattr(cfg, "logging", None)
    log_file = getattr(logging_cfg, "file", None)
    log_dir = log_file.parent if isinstance(log_file, Path) else None

    for path in (
        getattr(paths, "downloads", None),
        getattr(paths, "temp", None),
        getattr(paths, "output", None),
        log_dir,
    ):
        if path is not None:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
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
    downloads = (
        _path(section, "downloads", "paths.downloads", root)
        if "downloads" in section
        else (root / "downloads")
    )
    return PathsConfig(
        downloads=downloads,
        temp=_path(section, "temp", "paths.temp", root),
        output=_path(section, "output", "paths.output", root),
    )


def _parse_download(section: dict[str, Any]) -> DownloadConfig:
    max_height = _int(section, "max_height", "download.max_height")
    if not 144 <= max_height <= 4320:
        raise ConfigError("download.max_height must be between 144 and 4320")
    max_duration = (
        _float(section, "max_duration", "download.max_duration")
        if "max_duration" in section
        else 7200.0
    )
    if max_duration <= 0:
        raise ConfigError("download.max_duration must be positive")
    return DownloadConfig(
        max_height=max_height,
        max_duration=max_duration,
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
        audio_energy=_float(weights_raw, "audio_energy", "selection.weights.audio_energy")
        if "audio_energy" in weights_raw
        else 1.2,
        heatmap=_float(weights_raw, "heatmap", "selection.weights.heatmap")
        if "heatmap" in weights_raw
        else 1.5,
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
        use_audio_energy=_bool(section, "use_audio_energy", "selection.use_audio_energy")
        if "use_audio_energy" in section
        else True,
        use_youtube_heatmap=_bool(section, "use_youtube_heatmap", "selection.use_youtube_heatmap")
        if "use_youtube_heatmap" in section
        else True,
        api_key=_opt_str(section, "api_key", "selection.api_key")
        if "api_key" in section
        else None,
        llm_model=_str(section, "llm_model", "selection.llm_model")
        if "llm_model" in section
        else "gemini-3.8-flash",
        llm_api_key_env=_str(section, "llm_api_key_env", "selection.llm_api_key_env")
        if "llm_api_key_env" in section
        else "GEMINI_API_KEY",
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
        ("audio_energy", selection.weights.audio_energy),
        ("heatmap", selection.weights.heatmap),
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
