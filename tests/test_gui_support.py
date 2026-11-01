"""Display-free tests for GUI settings persistence and queue plumbing."""

from __future__ import annotations

import logging
import queue
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from shortsbot.config import ConfigError, load_config, save_config_values
from shortsbot.gui_support import (
    GuiSettings,
    LogEvent,
    QueueLogHandler,
    build_results,
    clip_file_error,
    drain_queue,
    open_file,
    open_folder,
    save_settings,
    short_error,
    validate_source,
)


CONFIG_TEXT = textwrap.dedent(
    """\
    # top comment
    selection:
      # keep this comment
      max_clips: 3
      min_seconds: 20
      max_seconds: 60

    transcription:
      model_size: base

    render:
      crop_mode: blur_pad
    """
)


class ConfigSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.yaml"
        self.path.write_text(CONFIG_TEXT, encoding="utf-8")

    def test_save_updates_values_and_preserves_comments(self):
        cfg = save_config_values(
            self.path,
            {
                "selection.max_clips": 5,
                "selection.min_seconds": 25.0,
                "selection.max_seconds": 70.0,
                "transcription.model_size": "small",
                "render.crop_mode": "center",
            },
        )

        self.assertEqual(cfg.selection.max_clips, 5)
        self.assertEqual(cfg.selection.min_seconds, 25.0)
        self.assertEqual(cfg.selection.max_seconds, 70.0)
        self.assertEqual(cfg.transcription.model_size, "small")
        self.assertEqual(cfg.render.crop_mode, "center")

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# top comment", text)
        self.assertIn("# keep this comment", text)
        self.assertIn("max_clips: 5", text)
        self.assertIn("model_size: small", text)

    def test_invalid_save_keeps_the_original_file(self):
        original = self.path.read_text(encoding="utf-8")

        with self.assertRaises(ConfigError):
            save_config_values(
                self.path,
                {
                    "selection.min_seconds": 70.0,
                    "selection.max_seconds": 60.0,
                },
            )

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertFalse(
            (self.path.parent / ".config.yaml.tmp").exists()
        )

    def test_missing_keys_are_inserted_into_the_section(self):
        self.path.write_text("selection:\n  max_clips: 3\n", encoding="utf-8")

        cfg = save_config_values(
            self.path,
            {"selection.min_seconds": 25, "selection.max_seconds": 65},
        )

        self.assertEqual(cfg.selection.min_seconds, 25.0)
        self.assertEqual(cfg.selection.max_seconds, 65.0)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("min_seconds: 25", text)
        self.assertIn("max_seconds: 65", text)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.yaml"
        self.path.write_text(CONFIG_TEXT, encoding="utf-8")
        self.cfg = load_config(self.path)

    def test_from_config_and_apply_are_non_destructive(self):
        settings = GuiSettings.from_config(self.cfg)
        applied = settings.apply(self.cfg)

        self.assertEqual(applied.selection.max_clips, 3)
        self.assertEqual(applied.transcription.model_size, "base")
        self.assertEqual(applied.render.crop_mode, "blur_pad")
        self.assertEqual(self.cfg.selection.max_clips, 3)

    def test_form_rejects_bad_values(self):
        with self.assertRaises(ConfigError):
            GuiSettings.from_form(
                max_clips="many",
                model_size="base",
                crop_mode="blur_pad",
                min_seconds="20",
                max_seconds="60",
            )
        with self.assertRaises(ConfigError):
            GuiSettings.from_form(
                max_clips="3",
                model_size="base",
                crop_mode="blur_pad",
                min_seconds="60",
                max_seconds="20",
            )
        with self.assertRaises(ConfigError):
            GuiSettings.from_form(
                max_clips="3",
                model_size="base",
                crop_mode="blur_pad",
                min_seconds="4",
                max_seconds="60",
            )

    def test_invalid_settings_never_write_the_file(self):
        original = self.path.read_text(encoding="utf-8")
        settings = GuiSettings(
            max_clips=3,
            model_size="base",
            crop_mode="blur_pad",
            min_seconds=70.0,
            max_seconds=60.0,
        )

        with self.assertRaises(ConfigError):
            save_settings(settings, self.cfg)

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)


class SourceValidationTests(unittest.TestCase):
    def test_urls_are_trimmed_and_returned(self):
        self.assertEqual(
            validate_source("  https://youtube.com/watch?v=abc  "),
            "https://youtube.com/watch?v=abc",
        )

    def test_local_file_is_resolved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mp4"
            path.write_bytes(b"not a real video")
            self.assertEqual(validate_source(str(path)), str(path.resolve()))

    def test_missing_or_empty_source_has_a_clear_error(self):
        with self.assertRaises(ConfigError):
            validate_source("")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ConfigError):
                validate_source(str(Path(folder) / "missing.mp4"))
            with self.assertRaises(ConfigError):
                validate_source(folder)


class QueueTests(unittest.TestCase):
    def test_log_handler_enqueues_formatted_record(self):
        target: queue.Queue[LogEvent] = queue.Queue()
        handler = QueueLogHandler(target)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="watch %s",
            args=("out",),
            exc_info=None,
        )

        handler.handle(record)

        event = target.get_nowait()
        self.assertIsInstance(event, LogEvent)
        self.assertEqual(event.text, "WARNING watch out")
        self.assertEqual(event.level, "WARNING")

    def test_drain_queue_is_bounded_and_ordered(self):
        target = queue.Queue()
        for item in range(5):
            target.put(item)

        self.assertEqual(drain_queue(target, limit=2), [0, 1])
        self.assertEqual(drain_queue(target, limit=2), [2, 3])
        self.assertEqual(drain_queue(target, limit=0), [])
        self.assertEqual(drain_queue(target), [4])
        self.assertEqual(drain_queue(target), [])


@dataclass(frozen=True)
class _FakeClip:
    index: int
    duration: float


@dataclass(frozen=True)
class _FakeInfo:
    duration: float


@dataclass(frozen=True)
class _FakeRenderResult:
    output: Path
    info: _FakeInfo


@dataclass(frozen=True)
class _FakeClipRun:
    clip: _FakeClip
    result: _FakeRenderResult | None = None
    error: str = ""


class ResultsTests(unittest.TestCase):
    def test_build_results_all_succeeded(self):
        runs = (
            _FakeClipRun(
                clip=_FakeClip(index=0, duration=20.5),
                result=_FakeRenderResult(
                    output=Path("output/clip01.mp4"),
                    info=_FakeInfo(duration=20.5),
                ),
            ),
            _FakeClipRun(
                clip=_FakeClip(index=1, duration=35.0),
                result=_FakeRenderResult(
                    output=Path("output/clip02.mp4"),
                    info=_FakeInfo(duration=35.0),
                ),
            ),
        )

        view = build_results(runs)
        self.assertEqual(view.message, "Finished: 2 clip(s) rendered.")
        self.assertEqual(len(view.rows), 2)

        self.assertEqual(view.rows[0].title, "clip01.mp4")
        self.assertEqual(view.rows[0].duration, "0:20")
        self.assertEqual(view.rows[0].path, Path("output/clip01.mp4"))
        self.assertEqual(view.rows[0].error, "")

        self.assertEqual(view.rows[1].title, "clip02.mp4")
        self.assertEqual(view.rows[1].duration, "0:35")
        self.assertEqual(view.rows[1].path, Path("output/clip02.mp4"))
        self.assertEqual(view.rows[1].error, "")

    def test_build_results_with_failures(self):
        runs = (
            _FakeClipRun(
                clip=_FakeClip(index=0, duration=22.0),
                result=_FakeRenderResult(
                    output=Path("output/clip01.mp4"),
                    info=_FakeInfo(duration=22.0),
                ),
            ),
            _FakeClipRun(
                clip=_FakeClip(index=1, duration=30.0),
                result=None,
                error="ffmpeg encode failed:\n  disk full",
            ),
        )

        view = build_results(runs)
        self.assertEqual(
            view.message,
            "Finished: 1 of 2 clip(s) rendered; 1 failed.",
        )
        self.assertEqual(len(view.rows), 2)

        self.assertEqual(view.rows[0].title, "clip01.mp4")
        self.assertEqual(view.rows[0].path, Path("output/clip01.mp4"))
        self.assertEqual(view.rows[0].error, "")

        self.assertEqual(view.rows[1].title, "Clip #2")
        self.assertEqual(view.rows[1].duration, "0:30")
        self.assertIsNone(view.rows[1].path)
        self.assertEqual(
            view.rows[1].error,
            "ffmpeg encode failed: disk full",
        )

    def test_build_results_all_failed(self):
        runs = (
            _FakeClipRun(
                clip=_FakeClip(index=0, duration=15.0),
                result=None,
                error="render timed out",
            ),
        )
        view = build_results(runs)
        self.assertEqual(
            view.message,
            "Finished: 0 of 1 clip(s) rendered; 1 failed.",
        )
        self.assertEqual(len(view.rows), 1)
        self.assertEqual(view.rows[0].title, "Clip #1")
        self.assertIsNone(view.rows[0].path)
        self.assertEqual(view.rows[0].error, "render timed out")

    def test_build_results_empty_with_reason(self):
        view = build_results(
            (),
            reason="video duration (15.0s) is shorter than minimum clip length (20.0s).",
        )
        self.assertEqual(view.rows, ())
        self.assertEqual(
            view.message,
            "No clips were selected: video duration (15.0s) is shorter than minimum clip length (20.0s).",
        )

    def test_build_results_empty_without_reason(self):
        view = build_results(())
        self.assertEqual(view.rows, ())
        self.assertEqual(view.message, "No clips were selected.")

    def test_short_error_whitespace_and_limits(self):
        self.assertEqual(
            short_error("  multi\nline   error\t\tmessage  "),
            "multi line error message",
        )
        self.assertEqual(short_error(""), "unknown error")
        self.assertEqual(short_error(None), "unknown error")

        long_msg = "x" * 200
        truncated = short_error(long_msg, limit=50)
        self.assertEqual(len(truncated), 50)
        self.assertTrue(truncated.endswith("..."))

    def test_clip_file_error_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            valid_file = root / "valid.mp4"
            valid_file.write_bytes(b"data")
            missing_file = root / "missing.mp4"
            sub_dir = root / "subdir"
            sub_dir.mkdir()

            self.assertEqual(clip_file_error(valid_file), "")
            self.assertIn("The clip file no longer exists", clip_file_error(missing_file))
            self.assertIn(str(missing_file), clip_file_error(missing_file))
            self.assertIn("The clip path is not a file", clip_file_error(sub_dir))
            self.assertIn(str(sub_dir), clip_file_error(sub_dir))

    def test_open_file_and_open_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "test.mp4"
            clip.write_bytes(b"123")

            with patch("os.startfile", create=True) as mock_startfile:
                open_file(clip)
                mock_startfile.assert_called_once_with(str(clip.resolve()))

            with patch("subprocess.Popen") as mock_popen:
                open_folder(clip)
                mock_popen.assert_called_once()
                args = mock_popen.call_args[0][0]
                self.assertEqual(args[0], "explorer")
                self.assertIn(f"/select,{clip.resolve()}", args[1])


if __name__ == "__main__":
    unittest.main()
