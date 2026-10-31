"""Environment diagnostics behind `python main.py check-env`.

Each check returns (ok, detail, hint) and must never raise: a broken check is
reported as a failed line so the matching hint stays visible.
"""

from __future__ import annotations

import gc
import importlib
import shutil
import time
from typing import Callable

from . import ffmpeg
from .config import Config

CheckFn = Callable[[Config], tuple[bool, str, str]]


def run_checks(cfg: Config, *, smoke_whisper: bool = False) -> bool:
    """Print a diagnostic report; return True when every check passed."""
    checks: list[tuple[str, CheckFn]] = [
        ("ffmpeg", _check_ffmpeg),
        ("ffprobe", _check_ffprobe),
        ("libx264 encoder", _check_encoder),
        ("ass filter", _check_ass_filter),
        ("yt-dlp", _check_ytdlp),
        ("faster-whisper", _check_faster_whisper),
        ("config", _check_config),
        ("working folders", _check_directories),
        ("filter path escaping", _check_escape),
    ]
    if smoke_whisper:
        checks.append(("whisper smoke test", _check_whisper_smoke))

    results: list[tuple[str, bool, str, str]] = []
    for label, check in checks:
        try:
            ok, detail, hint = check(cfg)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            ok, detail, hint = False, f"{type(exc).__name__}: {exc}", ""
        results.append((label, ok, detail, hint))

    width = max(len(label) for label, _, _, _ in results)
    print(f"Shorts Bot environment check ({cfg.config_path})")
    print()
    for label, ok, detail, hint in results:
        print(f"  {label:<{width}}  {'OK  ' if ok else 'FAIL'}  {detail}")
        if not ok and hint:
            print(f"  {'':<{width}}        -> {hint}")

    failed = sum(1 for _, ok, _, _ in results if not ok)
    print()
    if failed:
        print(f"{failed} check(s) failed.")
        return False
    print("All checks passed.")
    if not smoke_whisper:
        print("Run 'python main.py check-env --smoke-whisper' once to verify whisper inference.")
    return True


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #


def _check_ffmpeg(_cfg: Config) -> tuple[bool, str, str]:
    if shutil.which("ffmpeg") is None:
        return False, "not found on PATH", "install ffmpeg and add its bin folder to PATH"
    line = ffmpeg.ffmpeg_version()
    parts = line.split()
    version = parts[2] if len(parts) > 2 else line
    return True, version, ""


def _check_ffprobe(_cfg: Config) -> tuple[bool, str, str]:
    path = shutil.which("ffprobe")
    if path is None:
        return False, "not found on PATH", "ffprobe ships with ffmpeg; check PATH"
    return True, "found", ""


def _check_encoder(_cfg: Config) -> tuple[bool, str, str]:
    missing = [name for name in ("libx264", "aac") if not ffmpeg.has_encoder(name)]
    if missing:
        return (
            False,
            f"missing: {', '.join(missing)}",
            "use a full ffmpeg build (gyan.dev or BtbN)",
        )
    return True, "libx264 + aac", ""


def _check_ass_filter(_cfg: Config) -> tuple[bool, str, str]:
    if not ffmpeg.has_filter("ass"):
        return False, "missing", "install an ffmpeg build compiled with libass"
    return True, "available", ""


def _check_ytdlp(_cfg: Config) -> tuple[bool, str, str]:
    module = importlib.import_module("yt_dlp")
    version = getattr(getattr(module, "version", None), "__version__", None)
    if not version:
        version = getattr(module, "__version__", "unknown")
    return True, f"{version}", "pip install -r requirements.txt"


def _check_faster_whisper(_cfg: Config) -> tuple[bool, str, str]:
    module = importlib.import_module("faster_whisper")
    version = getattr(module, "__version__", "unknown")
    try:
        ctranslate2 = importlib.import_module("ctranslate2")
        backend = getattr(ctranslate2, "__version__", "unknown")
    except ImportError:
        backend = "missing"
    return True, f"{version} (ctranslate2 {backend})", "pip install -r requirements.txt"


def _check_config(cfg: Config) -> tuple[bool, str, str]:
    return (
        True,
        f"{cfg.config_path.name}: model={cfg.transcription.model_size}, "
        f"crop={cfg.render.crop_mode}, clips={cfg.selection.max_clips}",
        "",
    )


def _check_directories(cfg: Config) -> tuple[bool, str, str]:
    probe = ".shortsbot-write-test"
    for path in (cfg.paths.downloads, cfg.paths.temp, cfg.paths.output):
        try:
            path.mkdir(parents=True, exist_ok=True)
            marker = path / probe
            marker.write_text("ok", encoding="utf-8")
            marker.unlink()
        except OSError as exc:
            return False, f"{path}: {exc}", "check folder permissions"
    return True, "downloads/, temp/, output/ writable", ""


def _check_escape(cfg: Config) -> tuple[bool, str, str]:
    sample = cfg.paths.temp / "captions folder" / "clip 1.ass"
    escaped = ffmpeg.escape_filter_path(sample)
    if ":" in escaped.replace("\\:", ""):
        return False, escaped, "invalid escaping for the ass/subtitles filter"
    if " " not in escaped or "/" not in escaped:
        return False, escaped, "unexpected path escaping"
    return True, "Windows-safe", ""


def _check_whisper_smoke(cfg: Config) -> tuple[bool, str, str]:
    """Load the configured model and decode 3 s of generated audio.

    This is the real test that the CPU wheels (ctranslate2, onnxruntime) work
    on this interpreter, independent of any real footage.
    """
    from faster_whisper import WhisperModel  # imported lazily: slow import

    workdir = cfg.paths.temp / "_smoke"
    workdir.mkdir(parents=True, exist_ok=True)
    wav = workdir / "tone.wav"
    ffmpeg.run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
        desc="generating smoke-test audio",
    )

    started = time.perf_counter()
    model = WhisperModel(
        cfg.transcription.model_size,
        device="cpu",
        compute_type=cfg.transcription.compute_type,
        cpu_threads=cfg.transcription.cpu_threads,
    )
    load_time = time.perf_counter() - started

    started = time.perf_counter()
    segments, info = model.transcribe(
        str(wav),
        language=cfg.transcription.language,
        beam_size=1,
        vad_filter=False,
        word_timestamps=True,
    )
    list(segments)  # force decoding
    decode_time = time.perf_counter() - started

    language = getattr(info, "language", "?")
    del model
    gc.collect()
    wav.unlink(missing_ok=True)

    return (
        True,
        f"{cfg.transcription.model_size}/{cfg.transcription.compute_type}: "
        f"load {load_time:.1f}s, decode {decode_time:.2f}s (lang={language})",
        "",
    )
