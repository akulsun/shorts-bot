"""Command line interface.

Stage subcommands are registered as their modules are built, so the CLI never
advertises a command that does not work yet.
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
from pathlib import Path

from . import audio, checks, download, pipeline, render, selection, transcribe
from .config import Config, ensure_directories, load_config
from .errors import ConfigError, ShortsBotError
from .formatting import format_duration, format_size
from .logging_setup import setup_logging
from .state import State

log = logging.getLogger("shortsbot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Turn a long video into vertical, captioned YouTube Shorts.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.yaml (default: next to main.py)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="more detail (repeat for debug output)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    check_env = subparsers.add_parser(
        "check-env", help="verify ffmpeg, python packages and the config"
    )
    check_env.add_argument(
        "--smoke-whisper",
        action="store_true",
        help="also load whisper and transcribe 3 s of generated audio",
    )
    check_env.set_defaults(func=_cmd_check_env)

    download_cmd = subparsers.add_parser(
        "download",
        help="fetch a YouTube video or read a local file, then extract audio",
    )
    download_cmd.add_argument(
        "source", help="YouTube URL or path to a local video file"
    )
    download_cmd.add_argument(
        "--force",
        action="store_true",
        help="ignore cached results and fetch/extract again",
    )
    download_cmd.set_defaults(func=_cmd_download)

    transcribe_cmd = subparsers.add_parser(
        "transcribe",
        help="transcribe the audio with word-level timestamps",
    )
    transcribe_cmd.add_argument(
        "video_id", help="video id printed by the download stage"
    )
    transcribe_cmd.add_argument(
        "--preview",
        type=int,
        default=3,
        metavar="N",
        help="show the first N segments (default: 3)",
    )
    transcribe_cmd.add_argument(
        "--force",
        action="store_true",
        help="ignore the cached transcript and transcribe again",
    )
    transcribe_cmd.set_defaults(func=_cmd_transcribe)

    select_cmd = subparsers.add_parser(
        "select",
        help="pick the best clips from the transcript",
    )
    select_cmd.add_argument(
        "video_id", help="video id printed by the download stage"
    )
    select_cmd.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="number of clips to keep (default: selection.max_clips)",
    )
    select_cmd.set_defaults(func=_cmd_select)

    render_cmd = subparsers.add_parser(
        "render",
        help="cut a selected clip, make it vertical and burn in captions",
    )
    render_cmd.add_argument(
        "video_id", help="video id printed by the download stage"
    )
    render_cmd.add_argument(
        "--clip",
        type=int,
        default=0,
        metavar="N",
        help="clip index from 'select' (default: 0, the best clip)",
    )
    render_cmd.set_defaults(func=_cmd_render)

    run_cmd = subparsers.add_parser(
        "run",
        help="download, transcribe, select and render in one command",
    )
    run_cmd.add_argument(
        "source", help="YouTube URL or path to a local video"
    )
    run_cmd.add_argument(
        "--clips",
        type=int,
        default=None,
        metavar="N",
        help="number of clips (default: selection.max_clips)",
    )
    clean_group = run_cmd.add_mutually_exclusive_group()
    clean_group.add_argument(
        "--clean",
        dest="clean",
        action="store_true",
        help=(
            "delete the downloaded source video and audio from temp "
            "after a fully successful run"
        ),
    )
    clean_group.add_argument(
        "--keep-temp",
        dest="clean",
        action="store_false",
        help="keep source video and audio in temp (default)",
    )
    run_cmd.set_defaults(func=_cmd_run, clean=False)

    gui_cmd = subparsers.add_parser(
        "gui", help="open the desktop window"
    )
    gui_cmd.set_defaults(func=_cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    try:
        cfg = load_config(args.config)
    except ShortsBotError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.quiet:
        level = "WARNING"
    elif args.verbose:
        level = "DEBUG"
    else:
        level = cfg.logging.level
    setup_logging(level, cfg.logging.file)

    try:
        ensure_directories(cfg)
        return int(args.func(args, cfg) or 0)
    except ShortsBotError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    except Exception:  # noqa: BLE001 - last-resort guard, keeps the CLI clean
        log.exception("unexpected error")
        log.error("see the log file for details: %s", cfg.logging.file)
        return 1


def _cmd_check_env(args: argparse.Namespace, cfg: Config) -> int:
    return 0 if checks.run_checks(cfg, smoke_whisper=args.smoke_whisper) else 1


def _cmd_download(args: argparse.Namespace, cfg: Config) -> int:
    state = State.for_source(cfg, args.source)
    source = download.resolve_source(cfg, state, force=args.force)
    audio_path = audio.extract_audio(state, source, force=args.force)

    origin = "local file" if source.is_local else "YouTube"
    print()
    print(source.title)
    print(f"  video id  {state.video_id}")
    print(
        f"  source    {_display_path(source.path, cfg)} "
        f"({origin}, {source.width}x{source.height}, {format_duration(source.duration)})"
    )
    print(
        f"  audio     {_display_path(audio_path, cfg)} "
        f"({format_size(audio_path.stat().st_size)}, 16 kHz mono)"
    )
    print(f"  workdir   {_display_path(state.workdir, cfg)}")
    return 0


def _cmd_transcribe(args: argparse.Namespace, cfg: Config) -> int:
    state = State.load(cfg, args.video_id)
    transcript = transcribe.transcribe(cfg, state, force=args.force)

    info = state.get("source_info") or {}
    title = str(info.get("title") or state.video_id)
    word_count = sum(len(segment.words) for segment in transcript.segments)

    print()
    print(title)
    print(f"  language  {transcript.language or 'unknown'}")
    print(f"  duration  {format_duration(transcript.duration)}")
    print(f"  segments  {len(transcript.segments)}")
    print(f"  words     {word_count}")
    print(
        f"  file      "
        f"{_display_path(state.workdir / transcribe.TRANSCRIPT_FILENAME, cfg)}"
    )

    if args.preview <= 0:
        return 0
    if not transcript.segments:
        print()
        print("No speech was detected in the audio.")
        return 0

    shown = transcript.segments[: args.preview]
    print()
    print(f"Preview (first {len(shown)} of {len(transcript.segments)} segments):")
    for segment in shown:
        print(
            f"  #{segment.id:<3} "
            f"[{_fmt_timestamp(segment.start)} -> {_fmt_timestamp(segment.end)}]  "
            f"{segment.duration:.1f}s  {len(segment.words)} words"
        )
        print(f"        {segment.text}")
    return 0


def _cmd_select(args: argparse.Namespace, cfg: Config) -> int:
    if args.top is not None and args.top < 1:
        raise ConfigError("--top must be at least 1")

    state = State.load(cfg, args.video_id)
    result = selection.select_clips(cfg, state, max_clips=args.top)
    _print_clips(state, result.outcome, result.selection, result.path, cfg)
    return 0


def _print_clips(
    state: State,
    outcome,
    selection,
    clips_path: Path,
    cfg: Config,
) -> None:
    info = state.get("source_info") or {}
    title = str(info.get("title") or state.video_id)

    print()
    print(title)
    print(
        f"  selector  {selection.selector}  "
        f"({selection.min_seconds:.0f}-{selection.max_seconds:.0f}s, "
        f"up to {selection.max_clips})"
    )
    print(f"  file      {_display_path(clips_path, cfg)}")
    print(f"  found     {len(outcome.clips)} clip(s)")

    if not outcome.clips:
        print()
        print(f"No clips were selected: {outcome.reason}.")
        print(
            "Hint: lower selection.min_seconds in config.yaml, "
            "or use a longer video."
        )
        return

    print()
    print("  score = weighted sum of the components (best first)")
    for clip in outcome.clips:
        print()
        print(
            f"  #{clip.index}  "
            f"[{_fmt_timestamp(clip.start)} -> {_fmt_timestamp(clip.end)}]  "
            f"{clip.duration:.1f}s  score {clip.score:.2f}"
        )
        items = [f"{key} {value:+.2f}" for key, value in clip.components.items()]
        for start in range(0, len(items), 3):
            print("      " + "   ".join(items[start:start + 3]))
        if clip.reason:
            print(f"      reason: {clip.reason}")
        print(
            textwrap.fill(
                clip.text,
                width=96,
                initial_indent="      ",
                subsequent_indent="      ",
            )
        )


def _cmd_render(args: argparse.Namespace, cfg: Config) -> int:
    if args.clip < 0:
        raise ConfigError("--clip must be 0 or greater")

    state = State.load(cfg, args.video_id)
    result = render.render_clip(cfg, state, args.clip)
    clip = result.clip
    info = result.info

    source = state.get("source_info") or {}
    title = str(source.get("title") or state.video_id)
    speed = clip.duration / result.seconds if result.seconds > 0 else 0.0
    size = result.output.stat().st_size if result.output.exists() else 0

    print()
    print(title)
    print(
        f"  clip      #{clip.index}  "
        f"[{_fmt_timestamp(clip.start)} -> {_fmt_timestamp(clip.end)}]  "
        f"{clip.duration:.1f}s  score {clip.score:.2f}"
    )
    print(f"  crop      {result.crop_mode}")
    if cfg.captions.enabled:
        print(
            f"  captions  {result.caption_events} events "
            f"({cfg.captions.max_words_per_line} words max)"
        )
    else:
        print("  captions  off")
    print(
        f"  output    {_display_path(result.output, cfg)} "
        f"({format_size(size)})"
    )
    print(f"  render    {result.seconds:.1f}s ({speed:.2f}x realtime)")
    print(
        f"  verified  {info.width}x{info.height} @ {info.fps:g} fps, "
        f"audio {'yes' if info.has_audio else 'NO'}, "
        f"duration {info.duration:.2f}s (clip {clip.duration:.2f}s)"
    )
    return 0


def _cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    if args.clips is not None and args.clips < 1:
        raise ConfigError("--clips must be at least 1")

    result = pipeline.run_pipeline(
        cfg,
        args.source,
        max_clips=args.clips,
        clean=args.clean,
    )
    source = result.source
    successes = result.successes
    failures = result.failures

    print()
    print(source.title)
    print(f"  video id  {result.state.video_id}")
    print(f"  selected  {len(result.clips)} clip(s)")

    if result.clips:
        print()
        print("  Results:")
        for item in result.clips:
            if item.result is not None:
                cached = " (cached)" if item.result.cached else ""
                print(
                    f"    #{item.clip.index}  "
                    f"{_display_path(item.result.output, cfg)}  "
                    f"{format_duration(item.result.info.duration)}{cached}"
                )
            else:
                reason = item.error.splitlines()[0] if item.error else "unknown error"
                print(f"    #{item.clip.index}  FAILED  {reason}")
    else:
        print()
        print(f"  No clips were selected: {result.selection.outcome.reason}.")

    if result.removed:
        print()
        print("  Cleaned from temp:")
        for path, size in result.removed:
            print(f"    {_display_path(path, cfg)} ({format_size(size)})")
    elif args.clean and failures:
        print()
        print("  Temp kept because some clips failed to render.")
    elif args.clean and not result.clips:
        print()
        print("  Temp kept because no clips were rendered.")

    print()
    print(f"  {len(successes)} rendered, {len(failures)} failed")
    return result.return_code


def _cmd_gui(_args: argparse.Namespace, cfg: Config) -> int:
    # Imported lazily so the CLI and --help stay usable without a display.
    from .gui import run_gui

    return run_gui(cfg)


def _fmt_timestamp(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{secs:06.3f}"


def _display_path(path: Path, cfg: Config) -> str:
    try:
        return str(path.relative_to(cfg.root))
    except ValueError:
        return str(path)
