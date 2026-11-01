"""Stage 1a: acquire the source video.

Accepts a YouTube URL (via yt-dlp) or a local file path. The result is cached
in temp/<video_id>/state.json, so re-running with the same input is free.
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import CancelledError, DependencyError, DownloadError, FFmpegError
from .ffmpeg import probe
from .formatting import format_duration, strip_ansi
from .state import State

log = logging.getLogger(__name__)

SOURCE_STEM = "source"


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Resolved source video, regardless of where it came from."""

    video_id: str
    title: str
    path: Path
    duration: float
    width: int
    height: int
    is_local: bool
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "path": str(self.path),
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "is_local": self.is_local,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceInfo":
        return cls(
            video_id=str(data["video_id"]),
            title=str(data.get("title", "")),
            path=Path(data["path"]),
            duration=float(data.get("duration", 0.0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            is_local=bool(data.get("is_local", False)),
            url=str(data.get("url", "")),
        )


MAX_SOURCE_DURATION = 7200.0  # 2 hours


def is_url(source: str) -> bool:
    parsed = urllib.parse.urlsplit(source.strip())
    # Distinguish Windows drive letters (e.g. C:\...) from URL schemes
    is_win_drive = len(parsed.scheme) == 1 and parsed.scheme.isalpha()
    scheme = "" if is_win_drive else parsed.scheme.lower()
    if scheme in ("http", "https"):
        return True
    if scheme:
        raise DownloadError(
            f"unsupported URL scheme '{scheme}://'; only http://, https://, "
            f"or local video file paths are supported"
        )
    return False


def resolve_source(
    cfg: Config,
    state: State,
    *,
    force: bool = False,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SourceInfo:
    """Return the source video for this work folder, downloading if needed."""
    _check_cancel(cancel_event)
    source = state.source
    if not source:
        raise DownloadError(
            "this work folder has no recorded source. "
            "Start with: python main.py download <url-or-path>"
        )

    if not force:
        cached = _cached_source(state)
        if cached is not None:
            log.info("using cached source: %s", cached.path)
            return cached

    state.mark_running("download")
    try:
        if is_url(source):
            info = _download_url(
                cfg,
                state,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        else:
            info = _read_local(cfg, state)
    except Exception as exc:
        state.mark_failed("download", str(exc))
        raise
    _check_cancel(cancel_event)

    state.set("source_info", info.to_dict())
    state.mark_done(
        "download",
        path=str(info.path),
        title=info.title,
        duration=round(info.duration, 3),
    )
    return info


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _cached_source(state: State) -> SourceInfo | None:
    if not state.is_done("download"):
        return None
    raw = state.get("source_info")
    if not isinstance(raw, dict):
        return None
    try:
        info = SourceInfo.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        log.warning("cached source info is unreadable; fetching again")
        return None
    if not info.path.exists() or info.path.stat().st_size == 0:
        log.warning("cached video is gone from disk (%s); fetching again", info.path)
        return None
    return info


# --------------------------------------------------------------------------- #
# Local files
# --------------------------------------------------------------------------- #


def _read_local(_cfg: Config, state: State) -> SourceInfo:
    path = Path(state.source).expanduser()
    if not path.exists():
        raise DownloadError(
            f"file not found: {path}\n"
            f"Check the path, or pass a YouTube URL instead."
        )
    if not path.is_file():
        raise DownloadError(f"not a file: {path}")
    path = path.resolve()

    try:
        media = probe(path)
    except FFmpegError as exc:
        raise DownloadError(f"cannot read {path.name} as a video: {exc}") from exc

    if media.width == 0 or media.height == 0:
        raise DownloadError(f"{path.name} has no video stream; only audio was found")
    if not media.has_audio:
        raise DownloadError(
            f"{path.name} has no audio track, so there is nothing to transcribe"
        )
    if media.duration <= 0:
        raise DownloadError(f"{path.name} reports a zero duration; the file looks broken")
    max_duration = getattr(getattr(_cfg, "download", None), "max_duration", 7200.0)
    if media.duration > max_duration:
        raise DownloadError(
            f"{path.name} duration ({format_duration(media.duration)}) exceeds "
            f"the maximum allowed limit of {format_duration(max_duration)}"
        )

    log.info("using local file: %s", path)
    return SourceInfo(
        video_id=state.video_id,
        title=strip_ansi(path.stem) or path.stem,
        path=path,
        duration=media.duration,
        width=media.width,
        height=media.height,
        is_local=True,
    )


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #


def _download_url(
    cfg: Config,
    state: State,
    *,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SourceInfo:
    yt_dlp = _load_ytdlp()
    dest = state.workdir
    dest.mkdir(parents=True, exist_ok=True)

    log.info("fetching %s", state.source)
    options = _ydl_options(cfg, dest, on_progress=on_progress, cancel_event=cancel_event)
    max_duration = getattr(getattr(cfg, "download", None), "max_duration", 7200.0)
    meta_info: dict[str, Any] = {}
    video_info: dict[str, Any] = {}
    path: Path | None = None

    max_dl_cls = getattr(getattr(yt_dlp, "utils", None), "MaxDownloadsReached", ())
    if not (isinstance(max_dl_cls, type) and issubclass(max_dl_cls, BaseException)):
        max_dl_cls = ()

    try:
        _check_cancel(cancel_event)
        with yt_dlp.YoutubeDL(options) as ydl:
            # Validate stream status and duration before downloading
            meta = ydl.extract_info(state.source, download=False)
            if not isinstance(meta, dict):
                raise DownloadError("yt-dlp returned no information for this URL")
            meta_info = meta

            meta_item = (
                meta["entries"][0]
                if isinstance(meta.get("entries"), (list, tuple)) and meta["entries"]
                else meta
            )
            if not isinstance(meta_item, dict):
                meta_item = meta

            if meta_item.get("is_live") or meta_item.get("live_status") == "is_live":
                raise DownloadError("live streams are not supported")
            duration_meta = float(meta_item.get("duration") or 0.0)
            if duration_meta > max_duration:
                raise DownloadError(
                    f"video duration ({format_duration(duration_meta)}) exceeds "
                    f"the maximum allowed limit of {format_duration(max_duration)}"
                )

            _check_cancel(cancel_event)
            info = None
            try:
                info = ydl.extract_info(state.source, download=True)
            except max_dl_cls:
                # yt-dlp raises MaxDownloadsReached after completing the allowed download(s).
                # Treat as success if the downloaded file exists.
                info = meta

            if not isinstance(info, dict):
                raise DownloadError("yt-dlp returned no information for this URL")

            resolved = (
                info["entries"][0]
                if isinstance(info.get("entries"), (list, tuple)) and info["entries"]
                else info
            )
            video_info = resolved if isinstance(resolved, dict) else info
            path = _final_path(ydl, video_info, dest)
        _check_cancel(cancel_event)
    except max_dl_cls:
        # Safety net: treat as success if the file was downloaded to dest
        candidates = [
            p
            for p in dest.glob(f"{SOURCE_STEM}.*")
            if p.suffix.lower() not in {".part", ".ytdl", ".temp"}
        ]
        if candidates:
            path = max(candidates, key=lambda p: p.stat().st_mtime)
            video_info = meta_item if "meta_item" in locals() and isinstance(meta_item, dict) else meta_info
        else:
            raise DownloadError("maximum downloads reached but no video file was found on disk")
    except yt_dlp.utils.DownloadError as exc:
        raise _translate_error(str(exc)) from exc
    except OSError as exc:
        raise DownloadError(f"cannot write to {dest}: {exc}") from exc

    if path is None or not path.exists():
        raise DownloadError("download finished but no video file was found on disk")

    try:
        media = probe(path)
    except FFmpegError as exc:
        raise DownloadError(f"downloaded file is not readable: {exc}") from exc

    if not media.has_audio:
        raise DownloadError(
            f"'{video_info.get('title', path.stem)}' has no audio track, "
            f"so there is nothing to transcribe"
        )

    duration = media.duration or float(video_info.get("duration") or 0.0)
    if duration > max_duration:
        raise DownloadError(
            f"video duration ({format_duration(duration)}) exceeds "
            f"the maximum allowed limit of {format_duration(max_duration)}"
        )

    raw_title = str(video_info.get("title") or path.stem)
    title = strip_ansi(raw_title) or path.stem
    log.info("downloaded '%s' (%s)", title, format_duration(media.duration))
    return SourceInfo(
        video_id=state.video_id,
        title=title,
        path=path,
        duration=duration,
        width=media.width,
        height=media.height,
        is_local=False,
        url=state.source,
    )


def _ydl_options(
    cfg: Config,
    dest: Path,
    *,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    max_height = cfg.download.max_height
    fmt = (
        f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={max_height}][ext=mp4]/"
        f"bv*[height<={max_height}]+ba/"
        f"b[height<={max_height}]/b"
    )
    options: dict[str, Any] = {
        "format": fmt,
        "merge_output_format": "mp4",
        "paths": {"home": str(dest)},
        "outtmpl": {"default": f"{SOURCE_STEM}.%(ext)s"},
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": False,
        "no_color": True,
        "noprogress": True,
        "overwrites": False,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "progress_hooks": [_progress_logger(on_progress, cancel_event)],
        "logger": _YtdlLogger(),
    }
    if cfg.download.cookies_from_browser:
        options["cookiesfrombrowser"] = (cfg.download.cookies_from_browser,)
    return options


def _final_path(ydl: Any, info: dict[str, Any], dest: Path) -> Path:
    """Find the merged file yt-dlp actually wrote."""
    for entry in info.get("requested_downloads") or []:
        candidate = entry.get("filepath") or entry.get("_filename")
        if candidate and Path(candidate).exists():
            return Path(candidate)

    try:
        prepared = Path(ydl.prepare_filename(info))
        if prepared.exists():
            return prepared
    except Exception:
        pass

    partial_suffixes = {".part", ".ytdl", ".temp"}
    candidates = [
        path
        for path in dest.glob(f"{SOURCE_STEM}.*")
        if path.suffix.lower() not in partial_suffixes
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    raise DownloadError("download finished but no video file was found on disk")


def _load_ytdlp() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise DependencyError(
            "yt-dlp is not installed. Run: "
            "venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        ) from exc
    return yt_dlp


class _YtdlLogger:
    """Route yt-dlp messages through our logger instead of stdout."""

    def debug(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        log.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)


def _progress_logger(
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
):
    marks = {"next": 25}

    def hook(status: dict[str, Any]) -> None:
        _check_cancel(cancel_event)
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        downloaded = status.get("downloaded_bytes") or 0
        if not total:
            return
        fraction = min(1.0, max(0.0, downloaded / total))
        if on_progress is not None:
            on_progress(fraction)
        percent = fraction * 100.0
        if percent >= marks["next"]:
            log.info("downloading... %d%%", min(100, int(percent)))
            marks["next"] = int(percent) + 25

    return hook


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("download cancelled")


# --------------------------------------------------------------------------- #
# Error messages
# --------------------------------------------------------------------------- #

_HINT_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("private video", "this video is private"),
        "the video is private",
        "ask the uploader for access, or use a different video",
    ),
    (
        ("members-only", "join this channel"),
        "the video is members-only",
        "it requires a channel membership",
    ),
    (
        ("in your country", "geo-restricted", "not available in your country"),
        "the video is blocked in your region",
        "try a different network or another video",
    ),
    (
        ("sign in to confirm your age", "age-restricted", "inappropriate for some users"),
        "the video is age-restricted",
        "set download.cookies_from_browser in config.yaml (e.g. \"chrome\")",
    ),
    (
        ("sign in to confirm you", "not a bot", "cookies are no longer valid"),
        "YouTube wants a signed-in session (bot check)",
        "set download.cookies_from_browser in config.yaml, and update yt-dlp",
    ),
    (
        (
            "video unavailable",
            "video is unavailable",
            "video is not available",
            "video has been removed",
            "does not exist",
        ),
        "the video is unavailable",
        "check the URL, or try another video",
    ),
    (
        ("unsupported url",),
        "this URL is not supported by yt-dlp",
        "pass a YouTube video URL or a local file path",
    ),
    (
        (
            "getaddrinfo",
            "name resolution",
            "urlopen error",
            "network is unreachable",
            "connection refused",
            "connection reset",
            "timed out",
            "temporary failure",
            "no such host",
        ),
        "network error while downloading",
        "check your internet connection, then try again",
    ),
    (
        ("requested format is not available",),
        "no downloadable format matched your settings",
        "raise download.max_height in config.yaml",
    ),
)


def _translate_error(raw: str) -> DownloadError:
    text = raw.strip()
    if text.upper().startswith("ERROR:"):
        text = text[6:].strip()
    lowered = text.lower()

    for needles, summary, hint in _HINT_RULES:
        if any(needle in lowered for needle in needles):
            return DownloadError(f"{summary}.\nHint: {hint}.\nDetail: {_first_line(text)}")

    return DownloadError(f"download failed: {_first_line(text)}")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text
