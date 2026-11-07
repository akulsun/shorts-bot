"""Audio energy and volume dynamics analysis for viral moment detection.

Uses standard library `wave` and `numpy` to measure RMS audio energy,
detect shouting/laughter volume spikes, and evaluate dynamic range.
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioEnergyProfile:
    has_data: bool
    window_sec: float
    rms_values: tuple[float, ...]
    mean_rms: float
    peak_rms: float

    def score_window(self, start: float, end: float) -> tuple[float, float, float]:
        """Return (peak_ratio, avg_ratio, normalized_score) for a [start, end] window.

        normalized_score is in [0.0, 1.0], representing both the presence of
        an emotional volume surge (e.g. laughter/scream) and overall lively energy.
        """
        if not self.has_data or not self.rms_values or self.mean_rms <= 1e-6:
            return 1.0, 1.0, 0.5

        start_idx = max(0, int(start / self.window_sec))
        end_idx = min(len(self.rms_values), int(np.ceil(end / self.window_sec)))

        if start_idx >= end_idx:
            return 1.0, 1.0, 0.5

        window_rms = self.rms_values[start_idx:end_idx]
        if not window_rms:
            return 1.0, 1.0, 0.5

        max_in_window = max(window_rms)
        avg_in_window = sum(window_rms) / len(window_rms)

        peak_ratio = max_in_window / self.mean_rms
        avg_ratio = avg_in_window / self.mean_rms

        # A peak of 2.0x - 4.5x average volume indicates high excitement/laughter/shout.
        # Clamp peak contribution to [0, 1] at 3.5x.
        peak_score = min(1.0, max(0.0, (peak_ratio - 1.0) / 2.5))
        # An average of 1.4x indicates consistently energetic talking.
        avg_score = min(1.0, max(0.0, (avg_ratio - 0.7) / 1.0))

        # Dynamic contrast: difference between peak and window minimum indicates a climactic punch
        min_in_window = min(window_rms)
        contrast = (max_in_window - min_in_window) / (max_in_window + 1e-6)

        composite = 0.50 * peak_score + 0.30 * avg_score + 0.20 * contrast
        return (
            round(float(peak_ratio), 4),
            round(float(avg_ratio), 4),
            round(float(min(1.0, max(0.0, composite))), 4),
        )


def analyze_audio_energy(
    audio_path: Path | str | None, window_sec: float = 0.5
) -> AudioEnergyProfile:
    """Read a 16 kHz mono WAV file and return its RMS energy profile."""
    empty = AudioEnergyProfile(
        has_data=False,
        window_sec=window_sec,
        rms_values=(),
        mean_rms=0.0,
        peak_rms=0.0,
    )
    if not audio_path:
        return empty

    path = Path(audio_path)
    if not path.exists() or path.stat().st_size < 1024:
        return empty

    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            if n_frames <= 0 or sample_rate <= 0:
                return empty
            raw_bytes = wf.readframes(n_frames)

        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)

        samples_per_window = max(1, int(sample_rate * window_sec))
        num_windows = len(samples) // samples_per_window
        if num_windows <= 0:
            return empty

        reshaped = samples[: num_windows * samples_per_window].reshape(
            num_windows, samples_per_window
        )
        rms = np.sqrt(np.mean(reshaped**2, axis=1))

        mean_rms = float(np.mean(rms))
        peak_rms = float(np.max(rms)) if len(rms) > 0 else 0.0

        return AudioEnergyProfile(
            has_data=True,
            window_sec=window_sec,
            rms_values=tuple(float(v) for v in rms),
            mean_rms=mean_rms,
            peak_rms=peak_rms,
        )
    except Exception as exc:
        log.warning("could not analyze audio energy for %s: %s", path.name, exc)
        return empty
