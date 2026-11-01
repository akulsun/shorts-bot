"""Stage 4: cut a clip, make it vertical, burn captions -- one ffmpeg pass."""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import captions, jsonio, transcribe
from .config import Config
from .errors import FFmpegError, RenderError, StageError
from .ffmpeg import MediaInfo, probe, run_ffmpeg
from .models import Clip
from .selectors import CLIPS_FILENAME
from .state import State

log = logging.getLogger(__name__)

CAPTIONS_DIRNAME = "captions"


@dataclass(frozen=True, slots=True)
class RenderResult:
    output: Path
    info: MediaInfo
    clip: Clip
    crop_mode: str
    seconds: float
    caption_events: int
    cached: bool = False


def render_clip(
    cfg: Config,
    state: State,
    index: int,
    *,
    cancel_event: threading.Event | None = None,
) -> RenderResult:
    """Render the selected clip ranked `index` into cfg.paths.output."""
    state.require("select")
    clips, reason = _load_clips(state)

    clip = _find_clip(clips, index, reason, cfg)
    source = _source_path(state)
    transcript = transcribe.load_transcript(state)

    ass_name, caption_events = _write_captions(cfg, state, transcript, clip, index)
    graph = _video_graph(cfg, ass_name)
    output = cfg.paths.output / _output_name(cfg, state, clip)
    output.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "rendering clip #%d [%s -> %s] with %s crop%s",
        clip.index,
        _clock(clip.start),
        _clock(clip.end),
        cfg.render.crop_mode,
        f" and {caption_events} caption events" if caption_events else "",
    )

    started = time.perf_counter()
    state.mark_running("render")
    try:
        try:
            run_ffmpeg(
                _ffmpeg_args(cfg, source, output, clip, graph),
                desc=f"rendering clip #{clip.index}",
                cwd=state.workdir,
                cancel_event=cancel_event,
            )
        except FFmpegError as exc:
            raise RenderError(f"could not render clip #{clip.index}:\n{exc}") from exc
        seconds = time.perf_counter() - started
        info = _verify(output, clip, cfg)
    except Exception as exc:
        _remove_partial(output)
        state.mark_failed("render", str(exc))
        raise

    state.mark_done(
        "render",
        path=str(output),
        clip=clip.index,
        crop_mode=cfg.render.crop_mode,
        width=info.width,
        height=info.height,
        duration=round(info.duration, 3),
        seconds=round(seconds, 2),
    )
    cache = state.get("render_cache")
    if not isinstance(cache, dict):
        cache = {}
    cache[str(clip.index)] = {
        "signature": _signature(cfg, clip),
        "path": str(output),
        "duration": round(info.duration, 3),
        "seconds": round(seconds, 2),
        "caption_events": caption_events,
    }
    state.set("render_cache", cache)
    log.info(
        "rendered %s in %.1fs (%.1fx realtime)",
        output.name,
        seconds,
        clip.duration / seconds if seconds > 0 else 0.0,
    )
    return RenderResult(
        output=output,
        info=info,
        clip=clip,
        crop_mode=cfg.render.crop_mode,
        seconds=seconds,
        caption_events=caption_events,
    )


def cached_render(cfg: Config, state: State, index: int) -> RenderResult | None:
    """Return a previously verified render if settings and files are unchanged."""
    clips, reason = _load_clips(state)
    clip = _find_clip(clips, index, reason, cfg)

    cache = state.get("render_cache")
    record = cache.get(str(index)) if isinstance(cache, dict) else None
    if not isinstance(record, dict):
        return None
    if record.get("signature") != _signature(cfg, clip):
        log.info("render settings changed; rendering clip #%d again", index)
        return None

    raw = record.get("path")
    if not raw:
        return None
    output = Path(str(raw))
    if not output.exists():
        log.info("cached render is gone from disk; rendering clip #%d again", index)
        return None

    try:
        info = _verify(output, clip, cfg)
    except Exception as exc:  # noqa: BLE001 - a bad cache must fall back to render
        log.warning("cached render failed verification (%s); rendering again", exc)
        return None

    log.info("using cached render: %s", output.name)
    return RenderResult(
        output=output,
        info=info,
        clip=clip,
        crop_mode=cfg.render.crop_mode,
        seconds=float(record.get("seconds", 0.0) or 0.0),
        caption_events=int(record.get("caption_events", 0) or 0),
        cached=True,
    )


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def _load_clips(state: State) -> tuple[tuple[Clip, ...], str]:
    path = state.workdir / CLIPS_FILENAME
    if not path.exists():
        raise StageError(
            f"clip selection is missing: {path}\n"
            f"Run: python main.py select {state.video_id}"
        )

    data = jsonio.read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        raise StageError(
            f"clip selection is unreadable: {path}\n"
            f"Re-run: python main.py select {state.video_id}"
        )

    try:
        clips = tuple(Clip.from_dict(item) for item in data["clips"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StageError(
            f"clip selection is malformed: {path} ({exc})\n"
            f"Re-run: python main.py select {state.video_id}"
        ) from exc
    return clips, str(data.get("reason") or "")


def _find_clip(
    clips: tuple[Clip, ...], index: int, reason: str, cfg: Config
) -> Clip:
    if not clips:
        detail = f": {reason}" if reason else ""
        raise RenderError(
            f"no clips were selected for this video{detail}.\n"
            f"Selection needs at least {cfg.selection.min_seconds:.0f}s of speech. "
            f"Use a longer video, lower selection.min_seconds, or run "
            f"'python main.py select <video_id>' again."
        )
    for clip in clips:
        if clip.index == index:
            return clip
    available = ", ".join(str(clip.index) for clip in clips)
    raise RenderError(
        f"clip #{index} does not exist; available clips: {available} "
        f"(use --clip <index>)"
    )


def _source_path(state: State) -> Path:
    info = state.get("source_info")
    raw = info.get("path") if isinstance(info, dict) else None
    if not raw:
        raise StageError(
            "the source video path is not recorded in state.json. "
            f"Run: python main.py download \"{state.source}\""
        )
    path = Path(str(raw))
    if not path.exists():
        raise RenderError(
            f"source video is missing: {path}\n"
            f"Re-create it with: python main.py download \"{state.source}\""
        )
    return path


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #


def _write_captions(
    cfg: Config, state: State, transcript, clip: Clip, index: int
) -> tuple[str | None, int]:
    """Write a safe-named ASS file and return (path relative to workdir, events)."""
    if not cfg.captions.enabled:
        return None, 0

    text = captions.build_ass(transcript, clip, cfg)
    events = sum(1 for line in text.splitlines() if line.startswith("Dialogue: "))
    if not events:
        log.warning("clip #%d contains no words; rendering without captions", index)
        return None, 0

    path = state.workdir / CAPTIONS_DIRNAME / f"clip_{index:02d}.ass"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-8 without a BOM; libass accepts plain UTF-8.
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise StageError(f"cannot write caption file {path}: {exc}") from exc

    # Relative ASCII path: spaces/apostrophes in the project path never reach
    # the filtergraph parser, which cannot escape a single quote reliably.
    return path.relative_to(state.workdir).as_posix(), events


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #


def _video_graph(cfg: Config, ass_name: str | None) -> str:
    render = cfg.render
    width, height = render.width, render.height

    if render.crop_mode == "center":
        chain = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={render.fps}"
        )
    else:
        # Blur a quarter-size copy of the frame, then upscale it; much cheaper
        # than blurring 1080x1920 pixels directly and visually identical.
        blur_w, blur_h = max(2, width // 4), max(2, height // 4)
        chain = (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={blur_w}:{blur_h}:force_original_aspect_ratio=increase,"
            f"crop={blur_w}:{blur_h},gblur=sigma=6,"
            f"scale={width}:{height}:flags=bicubic,setsar=1[bgv];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            "setsar=1[fgv];"
            "[bgv][fgv]overlay=(W-w)/2:(H-h)/2,setsar=1,"
            f"fps={render.fps}"
        )

    if ass_name:
        chain += f",ass=filename='{ass_name}'"
    return chain + "[v]"


def _ffmpeg_args(
    cfg: Config, source: Path, output: Path, clip: Clip, graph: str
) -> list[str]:
    render = cfg.render
    return [
        "-ss",
        f"{clip.start:.3f}",
        "-t",
        f"{clip.duration:.3f}",
        "-i",
        str(source.resolve()),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        render.preset,
        "-crf",
        str(render.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        render.audio_bitrate,
        "-movflags",
        "+faststart",
        str(output.resolve()),
    ]


def _verify(output: Path, clip: Clip, cfg: Config) -> MediaInfo:
    info = probe(output)
    tolerance = max(0.5, 2.0 / cfg.render.fps)
    problems: list[str] = []

    expected_size = (cfg.render.width, cfg.render.height)
    if (info.width, info.height) != expected_size:
        problems.append(
            f"resolution is {info.width}x{info.height}, expected "
            f"{expected_size[0]}x{expected_size[1]}"
        )
    if not info.has_audio:
        problems.append("the output has no audio stream")
    if abs(info.duration - clip.duration) > tolerance:
        problems.append(
            f"duration is {info.duration:.2f}s, expected {clip.duration:.2f}s "
            f"(tolerance {tolerance:.2f}s)"
        )

    if problems:
        raise RenderError(
            f"rendered file failed verification: {output.name}\n  - "
            + "\n  - ".join(problems)
        )
    return info


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def _signature(cfg: Config, clip: Clip) -> str:
    """Settings that make an existing MP4 stale when any of them changes."""
    captions = cfg.captions
    render = cfg.render
    values = (
        f"{clip.start:.3f}",
        f"{clip.end:.3f}",
        clip.text,
        render.crop_mode,
        render.width,
        render.height,
        render.fps,
        render.preset,
        render.crf,
        render.audio_bitrate,
        int(captions.enabled),
        captions.font,
        captions.font_size,
        captions.max_words_per_line,
        captions.margin_v,
        captions.outline,
        int(captions.bold),
    )
    return "|".join(str(value) for value in values)


def _output_name(cfg: Config, state: State, clip: Clip) -> str:
    info = state.get("source_info") or {}
    title = str(info.get("title") or state.video_id)
    slug = _slug(title) or "video"
    return (
        f"{slug}_{state.video_id}_clip{clip.index + 1:02d}_"
        f"{cfg.render.crop_mode}.mp4"
    )


def _slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()[:48].rstrip("-")


def _clock(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{secs:06.3f}"


def _remove_partial(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove partial output: %s", path)
