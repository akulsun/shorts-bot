"""Display-free security regression tests for approved security fixes:
3.1: State.load() video_id regex validation
4.1: URL scheme restrictions (http/https or local path only)
1.1: Windows explorer argument quoting with spaces and commas
4.3: Live stream and max duration rejection before download
4.4: Single video only (noplaylist / max_downloads: 1)
3.2: Strip ANSI and control characters from video titles
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from shortsbot.config import ConfigError, load_config
from shortsbot.download import (
    _read_local,
    _ydl_options,
    is_url,
    resolve_source,
)
from shortsbot.errors import DownloadError, StageError
from shortsbot.ffmpeg import MediaInfo
from shortsbot.formatting import strip_ansi
from shortsbot.gui_support import open_file, open_folder, validate_source
from shortsbot.state import State


class SecurityRegressionTests(unittest.TestCase):
    # --- 3.1: video_id validation ---

    def test_3_1_video_id_regex_validation(self):
        """State.load validates video_id against ^[A-Za-z0-9_-]+$."""
        cfg = load_config()
        # Valid video IDs
        for valid_id in ("abc", "video_123", "video-456", "A-Z_0-9", "12345"):
            with self.subTest(video_id=valid_id):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                        tf.write(f"paths:\n  temp: \"{Path(tmpdir).as_posix()}\"\n")
                        cfg_path = Path(tf.name)
                    try:
                        custom_cfg = load_config(cfg_path)
                        state = State.load(custom_cfg, valid_id, source="test")
                        self.assertEqual(state.video_id, valid_id)
                    finally:
                        cfg_path.unlink(missing_ok=True)

        # Invalid video IDs (path traversal, special characters, whitespace, empty)
        for bad_id in (
            "",
            "../../etc",
            "..\\..\\windows",
            "../secret",
            "video id",
            "id#bad",
            "id;bad",
            "id\nname",
            "id/test",
            "id\\test",
        ):
            with self.subTest(video_id=bad_id):
                with self.assertRaises(StageError) as cm:
                    State.load(cfg, bad_id)
                self.assertIn("must match ^[A-Za-z0-9_-]+$", str(cm.exception))

    # --- 4.1: URL scheme restrictions ---

    def test_4_1_unsupported_url_schemes_rejected(self):
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

    def test_4_1_supported_schemes_and_local_paths(self):
        """http:// and https:// URLs pass; local paths return False without error."""
        self.assertTrue(is_url("http://example.com/video.mp4"))
        self.assertTrue(is_url("https://youtube.com/watch?v=12345"))
        self.assertFalse(is_url("C:\\videos\\sample.mp4"))
        self.assertFalse(is_url("downloads/test.mp4"))

    def test_4_1_gui_validate_source_rejects_unsupported_schemes(self):
        """validate_source translates DownloadError for invalid schemes into ConfigError."""
        with self.assertRaises(ConfigError) as cm:
            validate_source("file:///C:/secret.mp4")
        self.assertIn("unsupported URL scheme", str(cm.exception))

    # --- 1.1: Windows explorer argument handling with spaces and commas ---

    def test_1_1_open_folder_with_spaces_and_comma(self):
        """open_folder on Windows uses argument list with properly quoted /select,"<path>"."""
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "folder, with comma" / "clip, name with space.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"data")

            with patch("subprocess.Popen") as mock_popen:
                open_folder(clip)
                mock_popen.assert_called_once_with(
                    ["explorer", f'/select,"{clip.resolve()}"']
                )

    def test_1_1_open_file_with_spaces_and_comma(self):
        """open_file on Windows delegates to os.startfile without shell."""
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "folder, with comma" / "clip, name with space.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"data")

            with patch("os.startfile", create=True) as mock_startfile:
                open_file(clip)
                mock_startfile.assert_called_once_with(str(clip.resolve()))

    # --- 4.3: Live streams and max duration rejection before download ---

    def test_4_3_live_stream_rejected_before_download(self):
        """resolve_source checks extract_info(download=False) and rejects live streams."""
        cfg = load_config()
        state = State.load(cfg, "test_live", source="https://youtube.com/watch?v=live")
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = {"is_live": True, "title": "Live Stream"}

        fake_ytdlp = MagicMock()
        fake_ytdlp.YoutubeDL.return_value = mock_ydl
        fake_ytdlp.utils.DownloadError = type("FakeDownloadError", (Exception,), {})

        with patch("shortsbot.download._load_ytdlp", return_value=fake_ytdlp):
            with self.assertRaises(DownloadError) as cm:
                resolve_source(cfg, state, force=True)
            self.assertIn("live streams are not supported", str(cm.exception))

            # Verify extract_info was called with download=False and NEVER with download=True
            self.assertEqual(mock_ydl.extract_info.call_count, 1)
            self.assertEqual(mock_ydl.extract_info.call_args[1], {"download": False})

    def test_4_3_video_exceeding_max_duration_rejected_before_download(self):
        """resolve_source rejects videos exceeding download.max_duration before downloading."""
        cfg = load_config()  # default max_duration is 7200s (2 hours)
        state = State.load(cfg, "test_long", source="https://youtube.com/watch?v=long")
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = {
            "duration": 7201.0,
            "title": "Too Long Video",
            "is_live": False,
        }

        fake_ytdlp = MagicMock()
        fake_ytdlp.YoutubeDL.return_value = mock_ydl
        fake_ytdlp.utils.DownloadError = type("FakeDownloadError", (Exception,), {})

        with patch("shortsbot.download._load_ytdlp", return_value=fake_ytdlp):
            with self.assertRaises(DownloadError) as cm:
                resolve_source(cfg, state, force=True)
            self.assertIn("exceeds the maximum allowed limit", str(cm.exception))

            # extract_info should not have been called with download=True
            self.assertEqual(mock_ydl.extract_info.call_count, 1)
            self.assertEqual(mock_ydl.extract_info.call_args[1], {"download": False})

    def test_4_3_local_file_exceeding_max_duration_rejected(self):
        """_read_local rejects local videos exceeding download.max_duration."""
        cfg = load_config()
        with tempfile.NamedTemporaryFile("w", suffix=".mp4", delete=False) as tf:
            local_path = Path(tf.name)
        try:
            state = State.load(cfg, "test_local_long", source=str(local_path))
            fake_probe = MediaInfo(
                width=1920,
                height=1080,
                duration=7205.0,
                fps=30.0,
                has_audio=True,
            )
            with patch("shortsbot.download.probe", return_value=fake_probe):
                with self.assertRaises(DownloadError) as cm:
                    _read_local(cfg, state)
                self.assertIn("exceeds the maximum allowed limit", str(cm.exception))
        finally:
            local_path.unlink(missing_ok=True)

    # --- 4.4: Single video only (noplaylist) ---

    def test_4_4_single_video_only_in_ydl_options(self):
        """_ydl_options forces noplaylist, max_downloads=1, and playlist_items='1'."""
        cfg = load_config()
        opts = _ydl_options(cfg, Path("dest"))
        self.assertTrue(opts.get("noplaylist"))
        self.assertEqual(opts.get("max_downloads"), 1)
        self.assertEqual(opts.get("playlist_items"), "1")

    # --- 3.2: Strip ANSI / control characters ---

    def test_3_2_strip_ansi_and_control_chars(self):
        """strip_ansi removes ANSI color codes, OSC window titles, and non-printable control chars."""
        # ANSI color escapes
        self.assertEqual(strip_ansi("\x1b[31;1mRed Bold\x1b[0m"), "Red Bold")
        # OSC window title escape
        self.assertEqual(
            strip_ansi("\x1b]0;Evil Window Title\x07Clean Title"), "Clean Title"
        )
        self.assertEqual(
            strip_ansi("\x1b]2;Evil Title\x1b\\Clean Title"), "Clean Title"
        )
        # Control characters (BEL, BS, NUL, ESC)
        self.assertEqual(
            strip_ansi("Title\x00with\x08control\x1bcharacters"),
            "Titlewithcontrolcharacters",
        )
        # Plain text remains unchanged
        self.assertEqual(strip_ansi("Normal Title - 123"), "Normal Title - 123")


if __name__ == "__main__":
    unittest.main()
