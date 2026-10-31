"""Stage 2: transcribe audio with faster-whisper (word-level timestamps).

The result is cached as temp/<video_id>/transcript.json using the shared
Transcript/Segment/Word models, so later stages never touch whisper.
"""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path

from . import jsonio
from .config import Config
from .errors import StageError, TranscriptionError
from .ffmpeg import probe
from .formatting import format_duration
from .models import Segment, Transcript, Word
from .state import State

log = logging.getLogger(__name__)

TRANSCRIPT_FILENAME = "transcript.json"
PROGRESS_STEP = 10

# Rough download sizes, used only for error messages.
_MODEL_MB = {
    "tiny": 75,
    "base": 145,
    "small": 460,
    "medium": 1500,
    "large-v3": 3100,
}


def transcribe(cfg: Config, state: State, *, force: bool = False) -> Transcript:
    """Return the transcript for this work folder, running whisper if needed."""
    state.require("audio")
    audio_path = _audio_path(state)
    output = state.workdir / TRANSCRIPT_FILENAME

    if not force:
        cached = _load_cached(cfg, state, output)
        if cached is not None:
            log.info("using cached transcript: %s", output)
            return cached

    state.mark_running("transcribe")
    try:
        model = _load_model(cfg)
        try:
            transcript, elapsed = _decode(model, audio_path, cfg)
        finally:
            del model
            gc.collect()
    except Exception as exc:
        state.mark_failed("transcribe", str(exc))
        raise

    jsonio.write_json(output, transcript.to_dict())
    state.set("transcript_signature", _signature(cfg))
    state.mark_done(
        "transcribe",
        path=str(output),
        model=cfg.transcription.model_size,
        language=transcript.language,
        duration=round(transcript.duration, 3),
        segments=len(transcript.segments),
        words=sum(len(segment.words) for segment in transcript.segments),
        seconds=round(elapsed, 2),
    )

    if not transcript.segments:
        log.warning("no speech was detected in %s", audio_path.name)
    return transcript


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _signature(cfg: Config) -> str:
    settings = cfg.transcription
    return (
        f"{settings.model_size}/{settings.compute_type}/"
        f"{settings.language or 'auto'}/vad={int(settings.vad_filter)}"
    )


def _load_cached(cfg: Config, state: State, output: Path) -> Transcript | None:
    if not state.is_done("transcribe"):
        return None
    if not output.exists():
        log.warning("cached transcript is gone from disk; transcribing again")
        return None

    previous = state.get("transcript_signature")
    current = _signature(cfg)
    if previous and previous != current:
        log.info(
            "transcription settings changed (%s -> %s); transcribing again",
            previous,
            current,
        )
        return None

    try:
        transcript = Transcript.from_dict(jsonio.read_json(output))
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("cached transcript is unreadable (%s); transcribing again", exc)
        return None

    if not transcript.segments:
        log.warning("cached transcript contains no segments")
    return transcript


def _audio_path(state: State) -> Path:
    raw = state.get("audio_path")
    if not raw:
        raise StageError(
            "no audio path is recorded for this video. "
            "Re-run: python main.py download <url-or-path>"
        )
    path = Path(raw)
    if not path.exists():
        raise StageError(
            f"audio file is missing: {path}\n"
            f"Re-create it with: python main.py download \"{state.source}\""
        )
    return path


# --------------------------------------------------------------------------- #
# Whisper
# --------------------------------------------------------------------------- #


def _load_model(cfg: Config):
    from faster_whisper import WhisperModel  # lazy: heavy import

    settings = cfg.transcription
    log.info(
        "loading whisper model '%s' (%s, %d threads)",
        settings.model_size,
        settings.compute_type,
        settings.cpu_threads,
    )
    started = time.perf_counter()
    try:
        model = WhisperModel(
            settings.model_size,
            device="cpu",
            compute_type=settings.compute_type,
            cpu_threads=settings.cpu_threads,
        )
    except Exception as exc:
        size_mb = _MODEL_MB.get(settings.model_size, "several hundred")
        raise TranscriptionError(
            f"could not load the whisper model '{settings.model_size}'.\n"
            f"The first run downloads about {size_mb} MB from huggingface.co, "
            f"so check your internet connection.\n"
            f"Verify the setup with: python main.py check-env --smoke-whisper\n"
            f"Detail: {exc}"
        ) from exc
    log.info("model ready in %.1fs", time.perf_counter() - started)
    return model


def _decode(model, audio_path: Path, cfg: Config) -> tuple[Transcript, float]:
    settings = cfg.transcription
    log.info("transcribing %s", audio_path.name)

    started = time.perf_counter()
    try:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=settings.language,
            beam_size=settings.beam_size,
            vad_filter=settings.vad_filter,
            word_timestamps=True,
            # Avoid repeating a hallucinated phrase across a long video.
            condition_on_previous_text=False,
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)
        if total <= 0:
            total = probe(audio_path).duration

        marks = {"next": PROGRESS_STEP}
        segments: list[Segment] = []
        for index, raw in enumerate(segments_iter):
            segments.append(_to_segment(index, raw))
            _log_progress(float(raw.end), total, marks)
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(f"transcription failed: {exc}") from exc

    elapsed = time.perf_counter() - started
    _log_speed(total, elapsed)

    return (
        Transcript(
            language=str(getattr(info, "language", "") or ""),
            duration=total,
            segments=tuple(segments),
        ),
        elapsed,
    )


def _to_segment(index: int, raw) -> Segment:
    words: list[Word] = []
    for word in raw.words or ():
        text = str(word.word).strip()
        if not text:
            continue
        words.append(
            Word(
                start=float(word.start),
                end=float(word.end),
                text=text,
                probability=float(getattr(word, "probability", 1.0) or 1.0),
            )
        )

    text = str(raw.text).strip()
    if not text:
        text = " ".join(word.text for word in words)

    return Segment(
        id=index,
        start=float(raw.start),
        end=float(raw.end),
        text=text,
        words=tuple(words),
    )


def _log_progress(position: float, total: float, marks: dict[str, int]) -> None:
    if total <= 0:
        return
    percent = min(100, int(position * 100 / total))
    if percent < marks["next"]:
        return
    log.info(
        "progress %3d%%  (%s / %s)",
        percent,
        format_duration(position),
        format_duration(total),
    )
    marks["next"] = (percent // PROGRESS_STEP + 1) * PROGRESS_STEP


def _log_speed(total: float, elapsed: float) -> None:
    if total <= 0 or elapsed <= 0:
        log.info("transcribed in %.1fs", elapsed)
        return
    rtf = elapsed / total
    log.info(
        "transcribed %s of audio in %.1fs (RTF %.2f, %.1fx realtime)",
        format_duration(total),
        elapsed,
        rtf,
        total / elapsed,
    )
