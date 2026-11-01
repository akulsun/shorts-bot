"""Display-free tests for download cleanup and config compatibility."""

from __future__ import annotations

import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shortsbot.config import load_config
from shortsbot.errors import CancelledError
from shortsbot.pipeline import clean_downloads, run_pipeline
from shortsbot.state import State


class DownloadCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads_dir = self.root / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def test_clean_downloads_deletes_file_inside_downloads(self):
        target = self.downloads_dir / "downloaded_video.mp4"
        target.write_bytes(b"downloaded content")

        ok = clean_downloads(self.downloads_dir, target)
        self.assertTrue(ok)
        self.assertFalse(target.exists())

    def test_clean_downloads_refuses_outside_downloads(self):
        user_video = self.root / "user_chosen_video.mp4"
        user_video.write_bytes(b"user video")

        ok = clean_downloads(self.downloads_dir, user_video)
        self.assertFalse(ok)
        self.assertTrue(user_video.exists())

    def test_clean_downloads_refuses_output_folder(self):
        output_file = self.output_dir / "finished_short.mp4"
        output_file.write_bytes(b"short video")

        ok = clean_downloads(self.downloads_dir, output_file)
        self.assertFalse(ok)
        self.assertTrue(output_file.exists())

    def test_clean_downloads_refuses_path_traversal(self):
        outside_file = self.root / "secret.mp4"
        outside_file.write_bytes(b"secret")
        traversal = self.downloads_dir / ".." / "secret.mp4"

        ok = clean_downloads(self.downloads_dir, traversal)
        self.assertFalse(ok)
        self.assertTrue(outside_file.exists())

    def test_clean_downloads_refuses_directory_itself(self):
        ok = clean_downloads(self.downloads_dir, self.downloads_dir)
        self.assertFalse(ok)
        self.assertTrue(self.downloads_dir.is_dir())

    def test_clean_downloads_handles_permission_error_windows_file_lock(self):
        target = self.downloads_dir / "locked_file.mp4"
        target.write_bytes(b"locked data")

        with patch.object(Path, "unlink", side_effect=PermissionError("File in use")):
            ok = clean_downloads(self.downloads_dir, target)
            self.assertFalse(ok)
            self.assertTrue(target.exists())


class PipelineCleanupBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads_dir = self.root / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.root / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _make_cfg(self) -> SimpleNamespace:
        return SimpleNamespace(
            paths=SimpleNamespace(
                downloads=self.downloads_dir,
                temp=self.temp_dir,
                output=self.output_dir,
            )
        )

    def test_failed_run_keeps_file(self):
        workdir = self.temp_dir / "work123"
        workdir.mkdir(parents=True, exist_ok=True)
        downloaded = workdir / "source.mp4"
        downloaded.write_bytes(b"source data")

        state = State(video_id="work123", workdir=workdir, data={"source": "source_url"})
        state.set("source_info", {"path": str(downloaded)})

        info = SimpleNamespace(path=downloaded, title="test", duration=60.0)
        clip = SimpleNamespace(index=0, duration=20.0)
        selection = SimpleNamespace(
            outcome=SimpleNamespace(clips=(clip,)),
            selection=SimpleNamespace(),
            path=Path("clips.json"),
        )

        with (
            patch("shortsbot.pipeline.State.for_source", return_value=state),
            patch("shortsbot.pipeline.download.resolve_source", return_value=info),
            patch("shortsbot.pipeline.audio.extract_audio"),
            patch("shortsbot.pipeline.transcribe.transcribe"),
            patch("shortsbot.pipeline.selection.select_clips", return_value=selection),
            patch("shortsbot.pipeline.render.cached_render", return_value=None),
            patch(
                "shortsbot.pipeline.render.render_clip",
                side_effect=RuntimeError("render failed"),
            ),
        ):
            result = run_pipeline(self._make_cfg(), "source_url", clean=True)

        self.assertEqual(len(result.failures), 1)
        self.assertTrue(downloaded.exists())
        self.assertEqual(result.removed, ())

    def test_cancelled_run_keeps_file(self):
        workdir = self.temp_dir / "work456"
        workdir.mkdir(parents=True, exist_ok=True)
        downloaded = workdir / "source.mp4"
        downloaded.write_bytes(b"source data")

        state = State(video_id="work456", workdir=workdir, data={"source": "source_url"})
        state.set("source_info", {"path": str(downloaded)})

        cancel = threading.Event()
        cancel.set()

        with (
            patch("shortsbot.pipeline.State.for_source", return_value=state),
            patch("shortsbot.pipeline.download.resolve_source"),
        ):
            with self.assertRaises(CancelledError):
                run_pipeline(
                    self._make_cfg(),
                    "source_url",
                    clean=True,
                    cancel_event=cancel,
                )

        self.assertTrue(downloaded.exists())

    def test_successful_run_cleans_file(self):
        workdir = self.temp_dir / "work789"
        workdir.mkdir(parents=True, exist_ok=True)
        downloaded = workdir / "source.mp4"
        downloaded.write_bytes(b"source data")

        state = State(video_id="work789", workdir=workdir, data={"source": "source_url"})
        state.set("source_info", {"path": str(downloaded)})

        info = SimpleNamespace(path=downloaded, title="test", duration=60.0)
        clip = SimpleNamespace(index=0, duration=20.0)
        selection = SimpleNamespace(
            outcome=SimpleNamespace(clips=(clip,)),
            selection=SimpleNamespace(),
            path=Path("clips.json"),
        )
        render_res = SimpleNamespace(
            output=self.output_dir / "clip01.mp4",
            info=SimpleNamespace(duration=20.0),
            clip=clip,
        )

        with (
            patch("shortsbot.pipeline.State.for_source", return_value=state),
            patch("shortsbot.pipeline.download.resolve_source", return_value=info),
            patch("shortsbot.pipeline.audio.extract_audio"),
            patch("shortsbot.pipeline.transcribe.transcribe"),
            patch("shortsbot.pipeline.selection.select_clips", return_value=selection),
            patch("shortsbot.pipeline.render.cached_render", return_value=None),
            patch("shortsbot.pipeline.render.render_clip", return_value=render_res),
        ):
            result = run_pipeline(self._make_cfg(), "source_url", clean=True)

        self.assertEqual(len(result.successes), 1)
        self.assertEqual(len(result.failures), 0)
        self.assertFalse(downloaded.exists())
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0][0], downloaded.resolve())


class ConfigCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_config_with_downloads_key_loads_cleanly(self):
        config_file = self.root / "config_with_downloads.yaml"
        config_file.write_text(
            textwrap.dedent(
                """\
                paths:
                  downloads: custom_downloads
                  temp: custom_temp
                  output: custom_output
                """
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        self.assertEqual(cfg.paths.downloads.name, "custom_downloads")
        self.assertEqual(cfg.paths.temp.name, "custom_temp")
        self.assertEqual(cfg.paths.output.name, "custom_output")

    def test_config_without_downloads_key_loads_cleanly(self):
        config_file = self.root / "config_without_downloads.yaml"
        config_file.write_text(
            textwrap.dedent(
                """\
                paths:
                  temp: custom_temp
                  output: custom_output
                """
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        self.assertEqual(cfg.paths.downloads.name, "downloads")
        self.assertEqual(cfg.paths.temp.name, "custom_temp")
        self.assertEqual(cfg.paths.output.name, "custom_output")


if __name__ == "__main__":
    unittest.main()
