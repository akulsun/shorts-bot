"""Unit tests for caption chunking and ASS formatting (no ffmpeg, no video)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from shortsbot.captions import build_ass, build_events
from shortsbot.config import CaptionsConfig, RenderConfig
from shortsbot.models import Clip, Segment, Transcript, Word


def make_cfg(**overrides) -> SimpleNamespace:
    values = dict(
        enabled=True,
        font="Arial",
        font_size=72,
        max_words_per_line=4,
        margin_v=380,
        outline=4,
        bold=True,
    )
    values.update(overrides)
    return SimpleNamespace(
        captions=CaptionsConfig(**values),
        render=RenderConfig(
            width=1080,
            height=1920,
            fps=30,
            crop_mode="blur_pad",
            preset="veryfast",
            crf=21,
            audio_bitrate="128k",
        ),
    )


def make_transcript(specs, start: float = 0.0) -> Transcript:
    """Build a one-segment transcript from [(text, start_offset, end_offset), ...]."""
    words = tuple(
        Word(start + offset_start, start + offset_end, text)
        for text, offset_start, offset_end in specs
    )
    if not words:
        return Transcript("en", start, ())
    segment = Segment(
        id=0,
        start=words[0].start,
        end=words[-1].end,
        text=" ".join(word.text for word in words),
        words=words,
    )
    return Transcript("en", words[-1].end, (segment,))


def clip_for(_transcript: Transcript, start: float, end: float) -> Clip:
    return Clip(index=0, start=start, end=end, score=1.0, text="")


class ChunkTests(unittest.TestCase):
    def test_words_are_grouped_two_to_four_at_a_time(self):
        transcript = make_transcript(
            [("one", i * 0.4, i * 0.4 + 0.35) for i in range(10)]
        )
        events = build_events(transcript, clip_for(transcript, 0.0, 4.0), 4)

        self.assertTrue(events)
        for event in events:
            count = len(event.text.split())
            self.assertGreaterEqual(count, 2)
            self.assertLessEqual(count, 4)

    def test_orphan_word_is_merged_into_the_previous_chunk(self):
        transcript = make_transcript(
            [
                ("One", 0.0, 0.3),
                ("two", 0.3, 0.6),
                ("three", 0.6, 0.9),
                ("four.", 0.9, 1.2),
                ("five", 1.2, 1.5),
            ]
        )
        events = build_events(transcript, clip_for(transcript, 0.0, 2.0), 4)

        counts = [len(event.text.split()) for event in events]
        self.assertEqual(sum(counts), 5)
        self.assertTrue(all(2 <= count <= 4 for count in counts), counts)

    def test_punctuation_ends_a_chunk(self):
        transcript = make_transcript(
            [
                ("Hello", 0.0, 0.3),
                ("there.", 0.3, 0.6),
                ("How", 1.0, 1.3),
                ("are", 1.3, 1.6),
                ("you", 1.6, 1.9),
            ]
        )
        events = build_events(transcript, clip_for(transcript, 0.0, 2.0), 4)

        self.assertEqual([event.text for event in events], ["Hello there.", "How are you"])


class TimingTests(unittest.TestCase):
    def test_event_times_are_relative_to_the_clip_start(self):
        transcript = make_transcript(
            [("First", 0.5, 0.8), ("words", 0.8, 1.2), ("here", 1.2, 1.6)],
            start=100.0,
        )
        clip = clip_for(transcript, 100.0, 130.0)
        events = build_events(transcript, clip, 4)

        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].start, 0.5, places=2)
        self.assertAlmostEqual(events[0].end, 1.75, places=2)

    def test_words_outside_the_clip_are_trimmed(self):
        transcript = make_transcript(
            [("Before", 99.0, 99.8), ("inside", 100.5, 101.0), ("After", 102.0, 102.5)]
        )
        clip = clip_for(transcript, 100.0, 101.5)
        events = build_events(transcript, clip, 4)

        self.assertEqual([event.text for event in events], ["inside"])
        self.assertGreaterEqual(events[0].start, 0.0)
        self.assertLessEqual(events[0].end, clip.duration)


class AssTests(unittest.TestCase):
    def test_style_comes_from_config(self):
        transcript = make_transcript(
            [("Hello", 0.0, 0.4), ("world", 0.4, 0.8), ("again", 1.0, 1.4)]
        )
        clip = clip_for(transcript, 0.0, 2.0)
        document = build_ass(transcript, clip, make_cfg(font_size=80, margin_v=420))

        self.assertIn("PlayResX: 1080", document)
        self.assertIn("PlayResY: 1920", document)
        self.assertIn("Fontsize,", document)
        self.assertIn("Default,Arial,80,", document)
        self.assertIn(",420,1", document)
        self.assertIn("Dialogue: 0,0:00:00.00,", document)

    def test_markup_characters_are_neutralised(self):
        transcript = make_transcript(
            [("{evil}", 0.0, 0.4), ("\\Ntext", 0.4, 0.8), ("here", 0.8, 1.2)]
        )
        clip = clip_for(transcript, 0.0, 2.0)
        document = build_ass(transcript, clip, make_cfg())

        self.assertNotIn("{evil}", document)
        self.assertNotIn("\\N", document)
        self.assertIn("(evil)", document)

    def test_captions_can_be_disabled(self):
        transcript = make_transcript([("Hello", 0.0, 0.4), ("world", 0.4, 0.8)])
        clip = clip_for(transcript, 0.0, 2.0)
        document = build_ass(transcript, clip, make_cfg(enabled=False))

        self.assertIn("[Events]", document)
        self.assertNotIn("Dialogue:", document)


if __name__ == "__main__":
    unittest.main()
