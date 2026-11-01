"""Entry point: python gui.py [--config path\\to\\config.yaml]"""

from __future__ import annotations

import argparse
from pathlib import Path

from shortsbot.gui import main


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gui.py",
        description="Open the Shorts Bot desktop window.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.yaml (default: next to this file)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main(_parse_args().config))
