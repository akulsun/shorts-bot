"""Unit tests for audio energy and volume dynamics analysis."""

from __future__ import annotations

import struct
import tempfile
import unittest
import wave
from pathlib import Path

from shortsbot.audio_analysis import analyze_audio_energy


class AudioAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_wav(self, name: str, amplitudes: list[float], sample_rate: int = 16000) -> Path:
        """Create a mono 16-bit PCM WAV where each amplitude is held for 1 second."""
        wav_path = self.tmp_path / name
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            for amp in amplitudes:
                int_val = int(amp * 32767)
                frame = struct.pack("<h", int_val)
                wf.writeframes(frame * sample_rate)
        return wav_path

    def test_missing_or_empty_file_returns_safe_empty_profile(self):
        profile = analyze_audio_energy(self.tmp_path / "does_not_exist.wav")
        self.assertFalse(profile.has_data)
        _, _, score = profile.score_window(0.0, 10.0)
        self.assertEqual(score, 0.5)

    def test_audio_energy_detects_volume_surge(self):
        # 10s audio: 8s quiet (0.05 amplitude), 2s loud shout (0.80 amplitude) at seconds 5-7
        amps = [0.05, 0.05, 0.05, 0.05, 0.05, 0.80, 0.80, 0.05, 0.05, 0.05]
        wav = self._create_wav("test_spike.wav", amps)

        profile = analyze_audio_energy(wav, window_sec=0.5)
        self.assertTrue(profile.has_data)
        self.assertGreater(profile.peak_rms, profile.mean_rms)

        # Window with the spike (5.0s to 7.0s)
        _, _, spike_score = profile.score_window(5.0, 7.0)
        # Window during quiet part (0.0s to 3.0s)
        _, _, quiet_score = profile.score_window(0.0, 3.0)

        self.assertGreater(spike_score, quiet_score)
        self.assertGreaterEqual(spike_score, 0.6)


if __name__ == "__main__":
    unittest.main()
