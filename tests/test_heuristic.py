"""Unit tests for scoring, boundaries and overlap suppression."""

from __future__ import annotations

import unittest

from shortsbot.config import SelectionConfig, SelectionWeights
from shortsbot.errors import ConfigError
from shortsbot.models import Segment, Transcript, Word
from shortsbot.selectors import load_selector


def make_config(**overrides) -> SelectionConfig:
    values = dict(
        selector="heuristic",
        min_seconds=20.0,
        max_seconds=60.0,
        max_clips=3,
        keywords=("mistake", "secret", "never", "how to"),
        weights=SelectionWeights(
            speech_density=1.0,
            keywords=0.8,
            hook=1.0,
            completeness=0.6,
            filler=0.5,
            silence=0.5,
        ),
    )
    values.update(overrides)
    return SelectionConfig(**values)


def make_transcript(specs, *, gap: float = 0.0) -> Transcript:
    """Build a Transcript from [(text, duration_seconds), ...].

    Words are spread evenly across each segment so word rate is predictable.
    """
    segments: list[Segment] = []
    cursor = 0.0
    for index, (text, duration) in enumerate(specs):
        tokens = text.split()
        step = duration / len(tokens)
        words = tuple(
            Word(cursor + i * step, cursor + (i + 1) * step, token)
            for i, token in enumerate(tokens)
        )
        segments.append(Segment(index, words[0].start, words[-1].end, text, words))
        cursor += duration + gap
    return Transcript("en", segments[-1].end, tuple(segments))


def long_transcript(count: int = 20) -> Transcript:
    return make_transcript(
        [(f"Segment number {i} talks about things.", 8.0) for i in range(count)]
    )


def select(transcript: Transcript, cfg: SelectionConfig):
    return load_selector(cfg.selector).select(transcript, cfg)


class ScoringTests(unittest.TestCase):
    def test_hook_and_keywords_raise_the_score(self):
        weak = make_transcript(
            [
                ("so this thing happens now", 10),
                ("and people talk about it", 10),
                ("here is the way out", 10),
            ]
        )
        strong = make_transcript(
            [
                ("Why does this mistake happen", 10),
                ("and nobody talks about it", 10),
                ("here is the secret now", 10),
            ]
        )

        weak_clip = select(weak, make_config()).clips[0]
        strong_clip = select(strong, make_config()).clips[0]

        self.assertGreater(
            strong_clip.components["hook"], weak_clip.components["hook"]
        )
        self.assertGreater(
            strong_clip.components["keywords"], weak_clip.components["keywords"]
        )
        self.assertGreater(strong_clip.score, weak_clip.score)

    def test_components_add_up_to_the_score(self):
        transcript = make_transcript(
            [
                ("Why is this a mistake", 10),
                ("the secret is here", 12),
                ("and it never fails", 10),
            ]
        )
        for clip in select(transcript, make_config()).clips:
            self.assertAlmostEqual(
                sum(clip.components.values()), clip.score, places=2
            )


class BoundaryTests(unittest.TestCase):
    def test_clips_start_and_end_on_segment_boundaries(self):
        transcript = long_transcript(10)
        starts = {segment.start for segment in transcript.segments}
        ends = {segment.end for segment in transcript.segments}

        clips = select(transcript, make_config()).clips

        self.assertTrue(clips)
        for clip in clips:
            self.assertIn(clip.start, starts)
            self.assertIn(clip.end, ends)

    def test_clip_lengths_are_within_limits(self):
        clips = select(long_transcript(10), make_config()).clips

        self.assertTrue(clips)
        for clip in clips:
            self.assertGreaterEqual(clip.duration, 20.0)
            self.assertLessEqual(clip.duration, 60.0)


class OverlapTests(unittest.TestCase):
    def test_selected_clips_do_not_overlap(self):
        clips = sorted(
            select(long_transcript(20), make_config()).clips,
            key=lambda clip: clip.start,
        )

        self.assertGreaterEqual(len(clips), 2)
        for earlier, later in zip(clips, clips[1:]):
            self.assertLessEqual(earlier.end, later.start)

    def test_limit_is_respected(self):
        clips = select(long_transcript(20), make_config(max_clips=2)).clips

        self.assertEqual(len(clips), 2)


class EdgeCaseTests(unittest.TestCase):
    def test_video_shorter_than_minimum_explains_itself(self):
        outcome = select(make_transcript([("just a short clip", 12)]), make_config())

        self.assertEqual(outcome.clips, ())
        self.assertIn("at least 20s", outcome.reason)

    def test_segments_longer_than_maximum_explain_themselves(self):
        transcript = make_transcript(
            [("a very long segment", 80), ("another long one", 80)]
        )
        outcome = select(transcript, make_config())

        self.assertEqual(outcome.clips, ())
        self.assertIn("no run of whole segments", outcome.reason)

    def test_empty_transcript_explains_itself(self):
        outcome = select(Transcript("en", 0.0, ()), make_config())

        self.assertEqual(outcome.clips, ())
        self.assertIn("no speech segments", outcome.reason)

    def test_unknown_selector_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            load_selector("gpt-4")


if __name__ == "__main__":
    unittest.main()
