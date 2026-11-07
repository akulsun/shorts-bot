"""Environment file (.env) loader.

Zero-dependency reader for .env files to keep API keys and secrets safe
from being committed to public repositories.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Parse a .env file and set any missing variables in os.environ.

    Existing os.environ variables take precedence unless they are empty.
    Returns a dictionary of the loaded variables.
    """
    if path is None:
        project_root = Path(__file__).resolve().parent.parent
        env_path = project_root / ".env"
    else:
        env_path = Path(path).expanduser().resolve()

    if not env_path.exists() or not env_path.is_file():
        return {}

    loaded: dict[str, str] = {}
    try:
        content = env_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("could not read %s: %s", env_path, exc)
        return {}

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export ") :].strip()

        key, sep, val = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        val = val.strip()

        # Handle quoted values
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        else:
            # Strip inline comments (e.g. KEY=val # comment)
            comment_match = re.search(r"\s+#.*$", val)
            if comment_match:
                val = val[: comment_match.start()].strip()

        if key:
            loaded[key] = val
            if not os.environ.get(key):
                os.environ[key] = val

    if loaded:
        log.info("loaded %d environment variable(s) from %s", len(loaded), env_path.name)
    return loaded
