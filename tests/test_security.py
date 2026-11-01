"""Display-free security regression tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from shortsbot.config import ConfigError, load_config
from shortsbot.download import is_url, resolve_source
from shortsbot.errors import DownloadError, StageError
from shortsbot.gui_support import open_folder, validate_source
from shortsbot.render import _slug
from shortsbot.state import State


class SecurityRegressionTests(unittest.TestCase):
    def test_unsupported_url_schemes_rejected(self):
        """Reject file://, ftp://, and unknown schemes rather than reading as local files."""
        for invalid_url in (
            "file:///etc/passwd",
            "file:///C:/Windows/win.ini",
            "ftp://example.com/video.mp4",
            "smb://server/share/video.mp4",
            "data:video/mp4;base64,AAAA",
            "gopher://example.com",
        ):
            with self.subTest(url=invalid_url):
                with self.assertRaises(DownloadError) as cm:
                    is_url(invalid_url)
                self.assertIn("unsupported URL scheme", str(cm.exception))

    def test_supported_schemes_and_local_paths(self):
        """http:// and https:// URLs pass; local paths return False without error."""
        self.assertTrue(is_url("http://example.com/video.mp4"))
        self.assertTrue(is_url("https://youtube.com/watch?v=12345"))
        self.assertFalse(is_url("C:\\videos\\sample.mp4"))
        self.assertFalse(is_url("downloads/test.mp4"))

    def test_gui_validate_source_rejects_unsupported_schemes(self):
        """validate_source translates DownloadError for invalid schemes into ConfigError."""
        with self.assertRaises(ConfigError) as cm:
            validate_source("file:///C:/secret.mp4")
        self.assertIn("unsupported URL scheme", str(cm.exception))

    def test_video_id_path_traversal_rejected(self):
        """State.load rejects traversal characters and non-hex IDs."""
        cfg = load_config()
        for bad_id in (
            "../../etc",
            "..\\..\\windows",
            "../secret",
            "CON",
            "NUL",
            "12345",  # too short
            "123456789012345",  # too long
            "not_hex_id!!",
        ):
            with self.subTest(video_id=bad_id):
                with self.assertRaises(StageError) as cm:
                    State.load(cfg, bad_id)
                self.assertIn("invalid video id", str(cm.exception))

    def test_valid_video_id_accepted(self):
        """State.load accepts exactly 10 hex characters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                tf.write(f"paths:\n  temp: \"{Path(tmpdir).as_posix()}\"\n")
                cfg_path = Path(tf.name)
            try:
                cfg = load_config(cfg_path)
                state = State.load(cfg, "a1b2c3d4e5", source="test")
                self.assertEqual(state.video_id, "a1b2c3d4e5")
                self.assertEqual(state.workdir, Path(tmpdir).resolve() / "a1b2c3d4e5")
            finally:
                cfg_path.unlink(missing_ok=True)

    def test_config_captions_font_injection_rejected(self):
        """captions.font rejects newlines, commas, and formatting characters."""
        for bad_font in (
            "Arial\n[Events]\nDialogue: 0",
            "Arial, Bold",
            "Font; rm -rf",
            "Font{override}",
        ):
            with self.subTest(font=bad_font):
                with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                    tf.write(f"captions:\n  font: \"{bad_font}\"\n")
                    path = Path(tf.name)
                try:
                    with self.assertRaises(ConfigError) as cm:
                        load_config(path)
                    self.assertIn("captions.font", str(cm.exception))
                finally:
                    path.unlink(missing_ok=True)

    def test_config_audio_bitrate_flags_rejected(self):
        """render.audio_bitrate rejects flags and command injection patterns."""
        for bad_bitrate in ("-vn", "128k; rm", "128k -f null", "fast"):
            with self.subTest(bitrate=bad_bitrate):
                with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                    tf.write(f"render:\n  audio_bitrate: \"{bad_bitrate}\"\n")
                    path = Path(tf.name)
                try:
                    with self.assertRaises(ConfigError) as cm:
                        load_config(path)
                    self.assertIn("render.audio_bitrate", str(cm.exception))
                finally:
                    path.unlink(missing_ok=True)

    def test_config_browser_cookies_whitelist(self):
        """download.cookies_from_browser must be a known browser name."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            tf.write("download:\n  cookies_from_browser: unknown_browser\n")
            path = Path(tf.name)
        try:
            with self.assertRaises(ConfigError) as cm:
                load_config(path)
            self.assertIn("cookies_from_browser", str(cm.exception))
        finally:
            path.unlink(missing_ok=True)

    def test_config_max_clips_and_seconds_upper_bounds(self):
        """selection.max_clips and max_seconds have safety ceilings."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            tf.write("selection:\n  max_clips: 9999\n")
            path = Path(tf.name)
        try:
            with self.assertRaises(ConfigError) as cm:
                load_config(path)
            self.assertIn("max_clips", str(cm.exception))
        finally:
            path.unlink(missing_ok=True)

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            tf.write("selection:\n  max_seconds: 500\n")
            path = Path(tf.name)
        try:
            with self.assertRaises(ConfigError) as cm:
                load_config(path)
            self.assertIn("max_seconds", str(cm.exception))
        finally:
            path.unlink(missing_ok=True)

    def test_slug_sanitization_for_output_filenames(self):
        """_slug sanitizes Windows reserved names, unicode, and path separators."""
        # Reserved DOS name CON -> becomes safe slug
        self.assertEqual(_slug("CON"), "con")
        self.assertEqual(_slug("PRN"), "prn")
        self.assertEqual(_slug("NUL"), "nul")

        # Path traversal characters replaced with hyphens
        self.assertEqual(_slug("../../etc/passwd"), "etc-passwd")
        self.assertEqual(_slug("..\\..\\windows\\system32"), "windows-system32")

        # Non-ASCII and emojis stripped cleanly
        self.assertEqual(_slug("🔥 Super Video 🎥!"), "super-video")
        self.assertEqual(_slug("日本語タイトル"), "")

    def test_open_folder_avoids_comma_delimiter_bug(self):
        """Paths containing commas fall back safely to os.startfile on the parent folder."""
        target = Path("C:/videos/test,part1.mp4")
        with patch("os.startfile", create=True) as mock_startfile:
            with patch("subprocess.Popen") as mock_popen:
                open_folder(target)
                mock_popen.assert_not_called()
                mock_startfile.assert_called_once_with(str(target.parent.resolve()))

    def test_live_stream_rejected(self):
        """resolve_source rejects YouTube live streams."""
        cfg = load_config()
        state = State.load(cfg, "1234567890", source="https://youtube.com/watch?v=live")
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = {"is_live": True, "title": "Live Video"}

        fake_ytdlp = MagicMock()
        fake_ytdlp.YoutubeDL.return_value = mock_ydl
        fake_ytdlp.utils.DownloadError = type("FakeDownloadError", (Exception,), {})

        with patch("shortsbot.download._load_ytdlp", return_value=fake_ytdlp):
            with self.assertRaises(DownloadError) as cm:
                resolve_source(cfg, state, force=True)
            self.assertIn("live streams are not supported", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
