"""Unit tests for the HybridSelector (multimodal viral clip selection)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shortsbot import jsonio
from shortsbot.config import SelectionConfig, SelectionWeights
from shortsbot.models import Segment, Transcript, Word
from shortsbot.selectors import load_selector


def make_config(**overrides) -> SelectionConfig:
    values = dict(
        selector="hybrid",
        min_seconds=20.0,
        max_seconds=60.0,
        max_clips=3,
        keywords=("mistake", "secret", "never", "how to"),
        weights=SelectionWeights(
            speech_density=1.0,
            keywords=0.8,
            hook=1.2,
            completeness=0.8,
            filler=0.5,
            silence=0.5,
            audio_energy=1.2,
            heatmap=1.5,
        ),
        use_audio_energy=True,
        use_youtube_heatmap=True,
    )
    values.update(overrides)
    return SelectionConfig(**values)


def make_transcript(specs, *, gap: float = 0.0) -> Transcript:
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


class HybridSelectorTests(unittest.TestCase):
    def test_hook_and_viral_openers_boost_score(self):
        weak = make_transcript(
            [
                ("so this thing happens now today", 10),
                ("and people talk about it everyday", 10),
                ("here is the way out now", 10),
            ]
        )
        strong = make_transcript(
            [
                ("You won't believe why this mistake happened!", 10),
                ("Nobody ever talks about this secret truth.", 10),
                ("Watch this right now to find out.", 10),
            ]
        )

        selector = load_selector("hybrid")
        weak_clips = selector.select(weak, make_config()).clips
        strong_clips = selector.select(strong, make_config()).clips

        self.assertTrue(weak_clips)
        self.assertTrue(strong_clips)
        self.assertGreater(strong_clips[0].score, weak_clips[0].score)
        self.assertGreater(
            strong_clips[0].components["hook"], weak_clips[0].components["hook"]
        )

    def test_clips_respect_duration_limits(self):
        specs = [(f"This is sentence number {i} with several words.", 8.0) for i in range(10)]
        transcript = make_transcript(specs)
        cfg = make_config(min_seconds=20.0, max_seconds=40.0)
        outcome = load_selector("hybrid").select(transcript, cfg)

        self.assertTrue(outcome.clips)
        for clip in outcome.clips:
            self.assertGreaterEqual(clip.duration, 19.5)  # accounting for acoustic buffers
            self.assertLessEqual(clip.duration, 41.0)

    def test_empty_transcript_handled_gracefully(self):
        outcome = load_selector("hybrid").select(Transcript("en", 0.0, ()), make_config())
        self.assertEqual(outcome.clips, ())
        self.assertIn("no speech segments", outcome.reason)

    def test_resolve_api_key_handles_direct_and_pasted_keys(self):
        import os
        from shortsbot.selectors.gemini import resolve_api_key

        # Direct api_key
        cfg1 = make_config(api_key="AIzaSyDirectKey123")
        self.assertEqual(resolve_api_key(cfg1), "AIzaSyDirectKey123")

        # Pasted into llm_api_key_env
        cfg2 = make_config(api_key=None, llm_api_key_env="AIzaSyPastedKey456")
        self.assertEqual(resolve_api_key(cfg2), "AIzaSyPastedKey456")

        # Environment variable
        os.environ["TEST_GEMINI_KEY"] = "AIzaSyFromEnv789"
        try:
            cfg3 = make_config(api_key=None, llm_api_key_env="TEST_GEMINI_KEY")
            self.assertEqual(resolve_api_key(cfg3), "AIzaSyFromEnv789")
        finally:
            os.environ.pop("TEST_GEMINI_KEY", None)


if __name__ == "__main__":
    unittest.main()
