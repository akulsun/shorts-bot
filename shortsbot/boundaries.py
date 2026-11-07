"""Sentence and thought boundary snapping.

Uses word-level timestamps to ensure clips begin at natural sentence/thought openings
and conclude cleanly at punctuation or natural conversational pauses, preventing mid-word
or mid-sentence cutoffs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import Transcript, Word

_TERMINAL_PUNCTUATION = (".", "!", "?", "…")
_TRAILING_DISALLOWED = frozenset(
    {
        "and",
        "or",
        "but",
        "because",
        "so",
        "if",
        "that",
        "with",
        "is",
        "are",
        "was",
        "were",
        "the",
        "a",
        "an",
        "to",
        "in",
        "on",
        "at",
        "then",
        "than",
        "as",
        "like",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "of",
        "for",
        "who",
        "which",
    }
)

_ACOUSTIC_LEAD_IN = 0.05
_ACOUSTIC_RELEASE = 0.15


@dataclass(frozen=True, slots=True)
class BoundaryResult:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def is_sentence_start(words: Sequence[Word], index: int) -> bool:
    """Check if word at index is a natural beginning of a sentence or thought."""
    if index <= 0:
        return True
    prev = words[index - 1]
    curr = words[index]

    # Preceded by terminal punctuation
    prev_text = prev.text.strip()
    if any(prev_text.endswith(p) for p in _TERMINAL_PUNCTUATION):
        return True

    # Preceded by a natural pause in speech (>= 0.35s)
    if (curr.start - prev.end) >= 0.35:
        return True

    # Capitalized word that follows non-comma pause
    curr_text = curr.text.strip()
    if curr_text and curr_text[0].isupper() and (curr.start - prev.end) >= 0.15:
        return True

    return False


def is_sentence_end(words: Sequence[Word], index: int) -> bool:
    """Check if word at index is a natural conclusion of a sentence or thought."""
    curr = words[index]
    curr_text = curr.text.strip().lower().rstrip(".,!?;:\"'")

    # Disallow ending on dangling prepositions or conjunctions
    if curr_text in _TRAILING_DISALLOWED:
        return False

    raw_text = curr.text.strip()
    if any(raw_text.endswith(p) for p in _TERMINAL_PUNCTUATION):
        return True

    if index >= len(words) - 1:
        return True

    next_word = words[index + 1]
    # Followed by a significant conversational pause (>= 0.45s)
    if (next_word.start - curr.end) >= 0.45:
        return True

    return False


def snap_clip_boundaries(
    transcript: Transcript,
    start: float,
    end: float,
    min_duration: float,
    max_duration: float,
) -> BoundaryResult:
    """Snap [start, end] to clean word/sentence boundaries inside duration limits."""
    all_words = transcript.words
    if not all_words:
        return BoundaryResult(start=start, end=end, text="")

    # Find words overlapping or near the candidate window
    start_candidates = [
        i for i, w in enumerate(all_words)
        if abs(w.start - start) <= 4.0 or (w.start >= start and w.start <= start + 3.0)
    ]
    if not start_candidates:
        # Fall back to the closest word to start
        closest_start = min(range(len(all_words)), key=lambda i: abs(all_words[i].start - start))
        start_candidates = [closest_start]

    # Sort start candidates: prefer true sentence starts, then closest to requested start
    best_start_idx = min(
        start_candidates,
        key=lambda i: (
            0 if is_sentence_start(all_words, i) else 1,
            abs(all_words[i].start - start),
        ),
    )
    snapped_start_word = all_words[best_start_idx]

    # Now find the best end word that satisfies min_duration and max_duration
    end_candidates = [
        i for i in range(best_start_idx, len(all_words))
        if min_duration <= (all_words[i].end - snapped_start_word.start) <= max_duration
    ]

    if not end_candidates:
        # Fallback: find any end index that gets closest to min_duration / max_duration
        target_dur = max(min_duration, min(max_duration, end - start))
        target_end = snapped_start_word.start + target_dur
        best_end_idx = min(
            range(best_start_idx, len(all_words)),
            key=lambda i: abs(all_words[i].end - target_end),
        )
    else:
        # Prefer true sentence ends, closest to requested end
        best_end_idx = min(
            end_candidates,
            key=lambda i: (
                0 if is_sentence_end(all_words, i) else 1,
                abs(all_words[i].end - end),
            ),
        )

    snapped_end_word = all_words[best_end_idx]

    final_start = max(0.0, snapped_start_word.start - _ACOUSTIC_LEAD_IN)
    final_end = snapped_end_word.end + _ACOUSTIC_RELEASE

    # Extract text from the clamped word range
    selected_words = all_words[best_start_idx : best_end_idx + 1]
    text = " ".join(w.text.strip() for w in selected_words).strip()

    return BoundaryResult(
        start=round(final_start, 3),
        end=round(final_end, 3),
        text=text,
    )
