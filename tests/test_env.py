"""Unit tests for the .env loader."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from shortsbot.env import load_env_file


class EnvLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_env_file_parses_cleanly(self):
        env_content = """
        # This is a comment
        GEMINI_API_KEY=AIzaSyTestKey12345
        EXPORT_KEY=export_val
        QUOTED_DOUBLE="double quoted value"
        QUOTED_SINGLE='single quoted value'
        WITH_INLINE=some_value # inline comment
        export BASH_STYLE=bash_val
        """
        env_file = self.tmp_path / ".env"
        env_file.write_text(env_content, encoding="utf-8")

        loaded = load_env_file(env_file)

        self.assertEqual(loaded.get("GEMINI_API_KEY"), "AIzaSyTestKey12345")
        self.assertEqual(loaded.get("QUOTED_DOUBLE"), "double quoted value")
        self.assertEqual(loaded.get("QUOTED_SINGLE"), "single quoted value")
        self.assertEqual(loaded.get("WITH_INLINE"), "some_value")
        self.assertEqual(loaded.get("BASH_STYLE"), "bash_val")

        # Verify it was placed into os.environ
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), "AIzaSyTestKey12345")

        # Cleanup test env vars from os.environ
        for k in ["GEMINI_API_KEY", "EXPORT_KEY", "QUOTED_DOUBLE", "QUOTED_SINGLE", "WITH_INLINE", "BASH_STYLE"]:
            os.environ.pop(k, None)

    def test_non_existent_env_file_returns_empty(self):
        loaded = load_env_file(self.tmp_path / ".env.nonexistent")
        self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest.main()
