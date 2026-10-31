"""Exception hierarchy for expected, user-facing failures.

Anything deriving from ShortsBotError is reported as a clean one-line message
instead of a traceback. Unexpected exceptions still surface a traceback.
"""


class ShortsBotError(Exception):
    """Base class for errors the CLI can report without a traceback."""


class ConfigError(ShortsBotError):
    """config.yaml is missing, malformed or has invalid values."""


class DependencyError(ShortsBotError):
    """A required external tool (ffmpeg, ffprobe, yt-dlp) is unavailable."""


class FFmpegError(ShortsBotError):
    """An ffmpeg/ffprobe invocation failed."""


class StageError(ShortsBotError):
    """A pipeline stage cannot run yet, or its cached output is unusable."""
