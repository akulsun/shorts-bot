"""Display-free tests for GUI settings persistence and queue plumbing."""

from __future__ import annotations

import logging
import queue
import tempfile
import textwrap
import unittest
from pathlib import Path

from shortsbot.config import ConfigError, load_config, save_config_values
from shortsbot.gui_support import (
    GuiSettings,
    LogEvent,
    QueueLogHandler,
    drain_queue,
    save_settings,
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


if __name__ == "__main__":
    unittest.main()
