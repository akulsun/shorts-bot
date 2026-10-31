"""Default clip selector: score segment-aligned windows, then remove overlaps.

Pure functions, no I/O -- every rule here is testable with made-up transcripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import SelectionConfig
from ..formatting import format_duration
from ..models import Clip, Segment, Transcript
from .base import SelectionOutcome

# Tuning constants. Weights live in config.yaml; these shape the 0..1 scores.
_IDEAL_WORDS_PER_SECOND = 3.2
_SILENCE_TOLERANCE = 0.8  # gap length (s) treated as normal between words
_SILENCE_BUDGET = 4.0  # dead air (s) that fully penalises a clip
_KEYWORD_SATURATION = 3.0  # keyword hits at which the keyword score hits 1.0
_FILLER_SATURATION = 0.125  # filler ratio that fully penalises a clip

_FILLERS = (
    " um ",
    " uh ",
    " erm ",
    " hmm ",
    " you know ",
    " i mean ",
    " sort of ",
    " kind of ",
)

_HOOK_OPENERS = (
    "how",
    "why",
    "what",
    "when",
    "never",
    "stop",
    "nobody",
    "everyone",
    "imagine",
    "here's",
    "the biggest",
    "the worst",
    "the best",
    "the secret",
)


@dataclass(frozen=True, slots=True)
class _Window:
    start: float
    end: float
    score: float
    components: dict[str, float]
    text: str
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


class HeuristicSelector:
    """Scores every legal window, then greedily keeps the best non-overlapping ones."""

    def select(self, transcript: Transcript, cfg: SelectionConfig) -> SelectionOutcome:
        segments = transcript.segments
        duration = transcript.duration or (segments[-1].end if segments else 0.0)

        if not segments:
            return SelectionOutcome((), "the transcript contains no speech segments")
        if duration < cfg.min_seconds:
            return SelectionOutcome(
                (),
                f"the video is {format_duration(duration)} long, "
                f"but a clip must be at least {cfg.min_seconds:.0f}s",
            )

        bounds = _legal_windows(segments, cfg)
        if not bounds:
            return SelectionOutcome(
                (),
                f"no run of whole segments lasts between {cfg.min_seconds:.0f}s "
                f"and {cfg.max_seconds:.0f}s (video is {format_duration(duration)}, "
                f"{len(segments)} segments)",
            )

        windows = [
            _score_window(segments[start:end + 1], cfg)
            for start, end in bounds
        ]
        chosen = _suppress_overlaps(windows, cfg.max_clips)

        clips = tuple(
            Clip(
                index=index,
                start=window.start,
                end=window.end,
                score=window.score,
                text=window.text,
                reason=window.reason,
                components=window.components,
            )
            for index, window in enumerate(chosen)
        )
        return SelectionOutcome(clips)


# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #


def _legal_windows(
    segments: tuple[Segment, ...], cfg: SelectionConfig
) -> list[tuple[int, int]]:
    """All (start_index, end_index) pairs whose duration is inside the limits."""
    windows: list[tuple[int, int]] = []
    for start in range(len(segments)):
        for end in range(start, len(segments)):
            duration = segments[end].end - segments[start].start
            if duration > cfg.max_seconds:
                break
            if duration >= cfg.min_seconds:
                windows.append((start, end))
    return windows


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _score_window(segments: list[Segment], cfg: SelectionConfig) -> _Window:
    start = segments[0].start
    end = segments[-1].end
    duration = max(1e-6, end - start)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    words = [word for segment in segments for word in segment.words]
    word_count = len(words) or len(text.split())

    density = _density_score(word_count / duration)
    hits = _keyword_hits(text, cfg.keywords)
    keywords = _clamp(hits / _KEYWORD_SATURATION)
    hook = _hook_score(text)
    completeness = _completeness_score(segments)
    filler = _filler_penalty(text, word_count)
    silence = _silence_penalty(words, start, end)

    weights = cfg.weights
    components = {
        "speech_density": weights.speech_density * density,
        "keywords": weights.keywords * keywords,
        "hook": weights.hook * hook,
        "completeness": weights.completeness * completeness,
        "filler": -(weights.filler * filler),
        "silence": -(weights.silence * silence),
    }
    score = sum(components.values())

    return _Window(
        start=start,
        end=end,
        score=round(score, 4),
        components={key: round(value, 4) for key, value in components.items()},
        text=text,
        reason=_reason(components, hits, hook, density),
    )


def _density_score(words_per_second: float) -> float:
    return _clamp(1.0 - abs(words_per_second - _IDEAL_WORDS_PER_SECOND) / _IDEAL_WORDS_PER_SECOND)


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    lowered = text.lower()
    hits = 0
    for keyword in keywords:
        term = keyword.strip().lower()
        if not term:
            continue
        if " " in term:
            hits += lowered.count(term)
        else:
            hits += len(re.findall(rf"\b{re.escape(term)}\b", lowered))
    return hits


def _hook_score(text: str) -> float:
    if not text:
        return 0.0
    sentence = _first_sentence(text).strip()
    lowered = sentence.lower()
    score = 0.0
    if _has_hook_opener(lowered):
        score += 0.40
    if sentence.endswith("?"):
        score += 0.35
    if any(character.isdigit() for character in sentence):
        score += 0.25
    if text[:1].isupper():
        score += 0.15
    elif text[:1].islower():
        score -= 0.30  # starts mid-sentence
    return _clamp(score)


def _has_hook_opener(lowered: str) -> bool:
    for opener in _HOOK_OPENERS:
        if lowered == opener:
            return True
        if lowered.startswith(opener) and lowered[len(opener)] in " ,.:;!?-'\"(":
            return True
    return False


def _completeness_score(segments: list[Segment]) -> float:
    first = segments[0].text.strip()
    last = segments[-1].text.strip()
    start_ok = bool(first) and (
        first[0].isupper() or first[0].isdigit() or first[0] in "\"'(["
    )
    end_ok = bool(last) and last[-1] in ".?!…"
    return (0.5 if start_ok else 0.0) + (0.5 if end_ok else 0.0)


def _filler_penalty(text: str, word_count: int) -> float:
    if word_count <= 0:
        return 0.0
    lowered = f" {text.lower()} "
    hits = sum(lowered.count(phrase) for phrase in _FILLERS)
    return _clamp((hits / word_count) / _FILLER_SATURATION)


def _silence_penalty(words, start: float, end: float) -> float:
    if not words:
        return 1.0
    dead_air = 0.0
    cursor = start
    for word in words:
        gap = word.start - cursor
        if gap > _SILENCE_TOLERANCE:
            dead_air += gap - _SILENCE_TOLERANCE
        cursor = max(cursor, word.end)
    tail = end - cursor
    if tail > _SILENCE_TOLERANCE:
        dead_air += tail - _SILENCE_TOLERANCE
    return _clamp(dead_air / _SILENCE_BUDGET)


def _reason(components: dict[str, float], hits: int, hook: float, density: float) -> str:
    strengths: list[str] = []
    if hits:
        strengths.append(f"{hits} keyword hit{'s' if hits != 1 else ''}")
    if hook >= 0.6:
        strengths.append("strong opening hook")
    if density >= 0.8:
        strengths.append("dense speech")
    return ", ".join(strengths) if strengths else "balanced"


# --------------------------------------------------------------------------- #
# Overlap suppression
# --------------------------------------------------------------------------- #


def _suppress_overlaps(windows: list[_Window], limit: int) -> list[_Window]:
    """Greedy non-maximum suppression: best score first, no shared time."""
    chosen: list[_Window] = []
    for candidate in sorted(windows, key=lambda window: (-window.score, window.start)):
        if any(_overlaps(candidate, kept) for kept in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return chosen


def _overlaps(first: _Window, second: _Window) -> bool:
    return first.start < second.end and second.start < first.end


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _first_sentence(text: str) -> str:
    for index, character in enumerate(text):
        if character in ".?!":
            return text[: index + 1]
    return text


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
