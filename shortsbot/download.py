"""Stage 1a: acquire the source video.

Accepts a YouTube URL (via yt-dlp) or a local file path. The result is cached
in temp/<video_id>/state.json, so re-running with the same input is free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import DependencyError, DownloadError, FFmpegError
from .ffmpeg import probe
from .formatting import format_duration
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


def is_url(source: str) -> bool:
    return source.strip().lower().startswith(("http://", "https://"))


def resolve_source(cfg: Config, state: State, *, force: bool = False) -> SourceInfo:
    """Return the source video for this work folder, downloading if needed."""
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
            info = _download_url(cfg, state)
        else:
            info = _read_local(cfg, state)
    except Exception as exc:
        state.mark_failed("download", str(exc))
        raise

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

    log.info("using local file: %s", path)
    return SourceInfo(
        video_id=state.video_id,
        title=path.stem,
        path=path,
        duration=media.duration,
        width=media.width,
        height=media.height,
        is_local=True,
    )


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #


def _download_url(cfg: Config, state: State) -> SourceInfo:
    yt_dlp = _load_ytdlp()
    dest = state.workdir
    dest.mkdir(parents=True, exist_ok=True)

    log.info("fetching %s", state.source)
    options = _ydl_options(cfg, dest)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(state.source, download=True)
            if not isinstance(info, dict):
                raise DownloadError("yt-dlp returned no information for this URL")
            path = _final_path(ydl, info, dest)
    except yt_dlp.utils.DownloadError as exc:
        raise _translate_error(str(exc)) from exc
    except OSError as exc:
        raise DownloadError(f"cannot write to {dest}: {exc}") from exc

    try:
        media = probe(path)
    except FFmpegError as exc:
        raise DownloadError(f"downloaded file is not readable: {exc}") from exc

    if not media.has_audio:
        raise DownloadError(
            f"'{info.get('title', path.stem)}' has no audio track, "
            f"so there is nothing to transcribe"
        )

    title = str(info.get("title") or path.stem)
    log.info("downloaded '%s' (%s)", title, format_duration(media.duration))
    return SourceInfo(
        video_id=state.video_id,
        title=title,
        path=path,
        duration=media.duration or float(info.get("duration") or 0.0),
        width=media.width,
        height=media.height,
        is_local=False,
        url=state.source,
    )


def _ydl_options(cfg: Config, dest: Path) -> dict[str, Any]:
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
        "progress_hooks": [_progress_logger()],
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

    prepared = Path(ydl.prepare_filename(info))
    if prepared.exists():
        return prepared

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


def _progress_logger():
    marks = {"next": 25}

    def hook(status: dict[str, Any]) -> None:
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        downloaded = status.get("downloaded_bytes") or 0
        if not total:
            return
        percent = downloaded * 100.0 / total
        if percent >= marks["next"]:
            log.info("downloading... %d%%", min(100, int(percent)))
            marks["next"] = int(percent) + 25

    return hook


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
        ("video unavailable", "this video is unavailable", "has been removed", "does not exist"),
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
