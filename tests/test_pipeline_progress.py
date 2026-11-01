"""Progress and cancellation tests for the end-to-end pipeline (no media)."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shortsbot.errors import CancelledError, ShortsBotError
from shortsbot.pipeline import ProgressEvent, run_pipeline


def make_clip(index: int) -> SimpleNamespace:
    return SimpleNamespace(index=index, duration=20.0)


def make_info() -> SimpleNamespace:
    return SimpleNamespace(path=Path("source.mp4"), title="test", duration=60.0)


def make_result(clip: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        output=Path(f"clip{clip.index}.mp4"),
        info=SimpleNamespace(duration=clip.duration),
        clip=clip,
        cached=False,
    )


def make_selection(clips: tuple[SimpleNamespace, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        outcome=SimpleNamespace(clips=clips),
        selection=SimpleNamespace(),
        path=Path("clips.json"),
    )


def fake_state() -> SimpleNamespace:
    return SimpleNamespace(video_id="abc123", workdir=Path("."))


class PipelineProgressTests(unittest.TestCase):
    def run_pipeline_with_fakes(
        self,
        clips: tuple[SimpleNamespace, ...] = (),
        *,
        events: list[ProgressEvent] | None = None,
        progress=None,
        cancel_event: threading.Event | None = None,
        download_callback_fraction: float | None = 0.5,
        transcribe_callback_fraction: float | None = 0.5,
        render_side_effect=None,
    ):
        def fake_download(_cfg, _state, *, on_progress=None, cancel_event=None):
            if on_progress is not None and download_callback_fraction is not None:
                on_progress(download_callback_fraction)
            return make_info()

        def fake_transcribe(_cfg, _state, *, on_progress=None, cancel_event=None):
            if on_progress is not None and transcribe_callback_fraction is not None:
                on_progress(transcribe_callback_fraction)
            return SimpleNamespace()

        callback = progress
        if events is not None:
            callback = events.append

        with (
            patch("shortsbot.pipeline.State.for_source", return_value=fake_state()),
            patch(
                "shortsbot.pipeline.download.resolve_source",
                side_effect=fake_download,
            ),
            patch("shortsbot.pipeline.audio.extract_audio") as audio,
            patch(
                "shortsbot.pipeline.transcribe.transcribe",
                side_effect=fake_transcribe,
            ),
            patch(
                "shortsbot.pipeline.selection.select_clips",
                return_value=make_selection(clips),
            ),
            patch("shortsbot.pipeline.render.cached_render", return_value=None),
            patch(
                "shortsbot.pipeline.render.render_clip",
                side_effect=render_side_effect
                or (
                    lambda _cfg, _state, index, *, cancel_event=None: make_result(
                        clips[index]
                    )
                ),
            ) as render,
        ):
            result = run_pipeline(
                SimpleNamespace(),
                "source.mp4",
                progress=callback,
                cancel_event=cancel_event,
            )
        return result, render

    def test_progress_is_monotonic_and_covers_every_stage(self):
        clips = (make_clip(0), make_clip(1))
        events: list[ProgressEvent] = []

        result, _ = self.run_pipeline_with_fakes(clips, events=events)

        self.assertEqual(len(result.clips), 2)
        self.assertEqual(events[0].stage, "download")
        self.assertEqual(events[0].fraction, 0.0)
        self.assertEqual(events[-1].stage, "render")
        self.assertEqual(events[-1].fraction, 1.0)
        self.assertEqual(events[-1].message, "Done")

        fractions = [event.fraction for event in events]
        self.assertEqual(fractions, sorted(fractions))
        for stage in ("download", "transcribe", "select", "render"):
            self.assertIn(stage, [event.stage for event in events])

        # Download and whisper both reported a mid-stage value.
        self.assertTrue(
            any(event.stage == "download" and 0.0 < event.fraction < 0.10 for event in events)
        )
        self.assertTrue(
            any(
                event.stage == "transcribe" and 0.10 < event.fraction < 0.65
                for event in events
            )
        )
        self.assertTrue(
            any("Rendering clip #1 (2/2)" in event.message for event in events)
        )

    def test_callback_errors_do_not_break_the_run(self):
        def bad_callback(_event):
            raise RuntimeError("UI exploded")

        result, _ = self.run_pipeline_with_fakes(
            (make_clip(0),), progress=bad_callback
        )
        self.assertEqual(len(result.clips), 1)

    def test_cancel_before_start_makes_no_stage_call(self):
        event = threading.Event()
        event.set()
        with patch("shortsbot.pipeline.download.resolve_source") as download:
            with self.assertRaises(CancelledError):
                run_pipeline(SimpleNamespace(), "source.mp4", cancel_event=event)
        download.assert_not_called()
        self.assertIsInstance(CancelledError("x"), ShortsBotError)

    def test_cancel_after_download_stops_before_audio(self):
        event = threading.Event()

        def cancel_during_download(_cfg, _state, *, on_progress=None, cancel_event=None):
            event.set()
            return make_info()

        with (
            patch("shortsbot.pipeline.State.for_source", return_value=fake_state()),
            patch(
                "shortsbot.pipeline.download.resolve_source",
                side_effect=cancel_during_download,
            ),
            patch("shortsbot.pipeline.audio.extract_audio") as audio,
            patch("shortsbot.pipeline.transcribe.transcribe") as transcribe,
        ):
            with self.assertRaises(CancelledError):
                run_pipeline(SimpleNamespace(), "source.mp4", cancel_event=event)

        audio.assert_not_called()
        transcribe.assert_not_called()

    def test_cancel_during_render_is_not_recorded_as_a_clip_failure(self):
        clips = (make_clip(0), make_clip(1))
        event = threading.Event()

        def cancel_on_first_clip(_cfg, _state, index, *, cancel_event=None):
            event.set()
            raise CancelledError("render cancelled")

        with self.assertRaises(CancelledError):
            self.run_pipeline_with_fakes(
                clips,
                cancel_event=event,
                render_side_effect=cancel_on_first_clip,
            )


if __name__ == "__main__":
    unittest.main()
