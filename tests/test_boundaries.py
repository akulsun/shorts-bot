"""Unit tests for sentence and thought boundary snapping."""

from __future__ import annotations

import unittest

from shortsbot.boundaries import is_sentence_end, is_sentence_start, snap_clip_boundaries
from shortsbot.models import Segment, Transcript, Word


class BoundaryTests(unittest.TestCase):
    def test_sentence_start_and_end_detection(self):
        words = (
            Word(0.0, 0.4, "Hello."),
            Word(0.6, 0.9, "Why"),
            Word(1.0, 1.4, "not?"),
            Word(1.5, 1.8, "and"),
        )
        self.assertTrue(is_sentence_start(words, 0))
        self.assertTrue(is_sentence_end(words, 0))

        # "Why" follows "Hello." so it is a sentence start
        self.assertTrue(is_sentence_start(words, 1))

        # "not?" ends with punctuation
        self.assertTrue(is_sentence_end(words, 2))

        # "and" is a trailing preposition/conjunction, should NOT be allowed as sentence end
        self.assertFalse(is_sentence_end(words, 3))

    def test_snap_clip_boundaries_respects_durations_and_cleans_cutoffs(self):
        # Create a 30s transcript of 3 clear sentences
        # Sentence 1: 0.0 - 10.0: "First sentence goes here."
        # Sentence 2: 10.5 - 20.0: "Second sentence has an amazing punchline!"
        # Sentence 3: 20.5 - 28.0: "Third sentence ends the story."
        words: list[Word] = []
        words.extend([
            Word(0.0, 2.0, "First"),
            Word(2.2, 5.0, "sentence"),
            Word(5.2, 8.0, "goes"),
            Word(8.2, 10.0, "here."),
        ])
        words.extend([
            Word(10.5, 12.0, "Second"),
            Word(12.2, 14.0, "sentence"),
            Word(14.2, 16.0, "has"),
            Word(16.2, 18.0, "an"),
            Word(18.2, 19.0, "amazing"),
            Word(19.2, 20.0, "punchline!"),
        ])
        words.extend([
            Word(20.5, 22.0, "Third"),
            Word(22.2, 24.0, "sentence"),
            Word(24.2, 26.0, "ends"),
            Word(26.2, 27.5, "the"),
            Word(27.6, 28.0, "story."),
        ])
        segments = (
            Segment(0, 0.0, 10.0, "First sentence goes here.", tuple(words[:4])),
            Segment(1, 10.5, 20.0, "Second sentence has an amazing punchline!", tuple(words[4:10])),
            Segment(2, 20.5, 28.0, "Third sentence ends the story.", tuple(words[10:])),
        )
        transcript = Transcript("en", 28.5, segments)

        # Request an awkward subwindow: 1.0 to 19.0 (starts mid-sentence 1, ends before punchline)
        snapped = snap_clip_boundaries(transcript, 1.0, 19.0, min_duration=10.0, max_duration=25.0)

        # Snapped start should align to the start of Sentence 1 (0.0s)
        self.assertAlmostEqual(snapped.start, 0.0, delta=0.1)
        # Snapped end should include the punchline (20.0s)
        self.assertGreaterEqual(snapped.end, 20.0)
        self.assertTrue(snapped.text.endswith("punchline!"))


if __name__ == "__main__":
    unittest.main()
