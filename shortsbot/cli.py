"""Command line interface.

Stage subcommands are registered as their modules are built, so the CLI never
advertises a command that does not work yet.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import audio, checks, download
from .config import Config, ensure_directories, load_config
from .errors import ShortsBotError
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


def _display_path(path: Path, cfg: Config) -> str:
    try:
        return str(path.relative_to(cfg.root))
    except ValueError:
        return str(path)
