"""Unit tests for YouTube replay heatmap loading and scoring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shortsbot import jsonio
from shortsbot.heatmap import load_heatmap, score_heatmap_window


class HeatmapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_heatmap_file_returns_none(self):
        self.assertIsNone(load_heatmap(self.tmp_path))
        peak, avg, score = score_heatmap_window(None, 0.0, 30.0)
        self.assertEqual(score, 0.5)

    def test_load_and_score_heatmap(self):
        heatmap_data = [
            {"start_time": 0.0, "end_time": 10.0, "value": 0.1},
            {"start_time": 10.0, "end_time": 20.0, "value": 0.9},
            {"start_time": 20.0, "end_time": 30.0, "value": 0.2},
        ]
        jsonio.write_json(self.tmp_path / "heatmap.json", heatmap_data)

        loaded = load_heatmap(self.tmp_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded), 3)

        # Window with replay peak (10s to 20s)
        _, _, peak_score = score_heatmap_window(loaded, 10.0, 20.0)
        # Window with low replay (0s to 10s)
        _, _, low_score = score_heatmap_window(loaded, 0.0, 10.0)

        self.assertGreater(peak_score, low_score)


if __name__ == "__main__":
    unittest.main()
