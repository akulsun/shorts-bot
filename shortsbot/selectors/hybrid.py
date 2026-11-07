"""Hybrid multimodal clip selector: combines audio energy, replay heatmap,
sentence boundary snapping, and linguistic viral signals.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from ..audio_analysis import AudioEnergyProfile, analyze_audio_energy
from ..boundaries import snap_clip_boundaries
from ..config import SelectionConfig
from ..formatting import format_duration
from ..heatmap import load_heatmap, score_heatmap_window
from ..models import Clip, Segment, Transcript, Word
from .base import SelectionOutcome

if TYPE_CHECKING:
    from ..state import State

log = logging.getLogger(__name__)

# Constants for scoring
_IDEAL_WORDS_PER_SECOND = 3.2
_SILENCE_TOLERANCE = 1.0  # allow natural dramatic pauses up to 1.0s
_SILENCE_BUDGET = 4.0
_KEYWORD_SATURATION = 3.0
_FILLER_SATURATION = 0.125

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

_STRONG_HOOK_PATTERNS = (
    r"\b(you (won'?t|will not) believe)\b",
    r"\b(listen to this)\b",
    r"\b(look at this)\b",
    r"\b(watch this)\b",
    r"\b(here'?s (the secret|why|how|what))\b",
    r"\b(stop doing)\b",
    r"\b(the biggest mistake)\b",
    r"\b(the (worst|best|craziest|funniest))\b",
    r"\b(did you know)\b",
    r"\b(what if i told you)\b",
    r"\b(imagine if)\b",
    r"\b(no one talks about)\b",
    r"\b(wait until you see)\b",
    r"\b(so yesterday)\b",
)

_EMOTIONAL_REACTION_WORDS = (
    "oh my god",
    "omg",
    "no way",
    "holy shit",
    "holy crap",
    "what the fuck",
    "what the hell",
    "are you kidding",
    "bro",
    "insane",
    "crazy",
    "unbelievable",
    "hilarious",
    "screaming",
    "laughing",
    "dead",
    "dude",
    "listen",
    "watch",
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
class _CandidateWindow:
    start: float
    end: float
    score: float
    components: dict[str, float]
    text: str
    reason: str
    title: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


class HybridSelector:
    """Multimodal selector evaluating acoustic energy, heatmaps, and viral hooks."""

    def select(
        self,
        transcript: Transcript,
        cfg: SelectionConfig,
        state: State | None = None,
    ) -> SelectionOutcome:
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

        # Check if Gemini API key is available for LLM semantic analysis
        from .gemini import resolve_api_key
        api_key = resolve_api_key(cfg)
        if api_key:
            log.info(
                "[select] Gemini API key detected -> using Gemini LLM (%s) for semantic viral selection",
                cfg.llm_model,
            )
            try:
                from .gemini import GeminiSelector
                return GeminiSelector().select(transcript, cfg, state=state)
            except Exception as exc:
                log.warning("Gemini LLM selection failed (%s); falling back to local multimodal engine", exc)
        else:
            log.info(
                "[select] No Gemini API key detected -> using local multimodal engine (audio energy + replay heatmap)"
            )

        # 1. Load Audio Energy Profile if enabled and available
        audio_profile = None
        if cfg.use_audio_energy and state is not None:
            audio_path = state.get("audio_path") or (state.workdir / "audio.wav")
            audio_profile = analyze_audio_energy(audio_path)
            if audio_profile.has_data:
                log.info(
                    "audio energy profile loaded (mean RMS: %.4f, peak: %.4f)",
                    audio_profile.mean_rms,
                    audio_profile.peak_rms,
                )

        # 2. Load YouTube Replay Heatmap if enabled and available
        heatmap = None
        if cfg.use_youtube_heatmap and state is not None:
            heatmap = load_heatmap(state.workdir)
            if heatmap:
                log.info("loaded %d heatmap points for replay analysis", len(heatmap))

        # 3. Generate and score candidate windows
        bounds = _legal_windows(segments, cfg)
        if not bounds:
            return SelectionOutcome(
                (),
                f"no run of whole segments lasts between {cfg.min_seconds:.0f}s "
                f"and {cfg.max_seconds:.0f}s (video is {format_duration(duration)}, "
                f"{len(segments)} segments)",
            )

        candidates = [
            _score_candidate(
                segments[start_idx : end_idx + 1],
                transcript,
                cfg,
                audio_profile,
                heatmap,
            )
            for start_idx, end_idx in bounds
        ]

        chosen = _suppress_overlaps(candidates, cfg.max_clips)

        # 4. Snap boundaries to exact sentence and thought completions
        clips: list[Clip] = []
        for index, window in enumerate(chosen):
            snapped = snap_clip_boundaries(
                transcript,
                window.start,
                window.end,
                cfg.min_seconds,
                cfg.max_seconds,
            )
            clips.append(
                Clip(
                    index=index,
                    start=snapped.start,
                    end=snapped.end,
                    score=window.score,
                    text=snapped.text or window.text,
                    reason=window.reason,
                    components=window.components,
                    title=window.title,
                )
            )

        return SelectionOutcome(tuple(clips))


# --------------------------------------------------------------------------- #
# Candidate Generation
# --------------------------------------------------------------------------- #


def _legal_windows(
    segments: tuple[Segment, ...], cfg: SelectionConfig
) -> list[tuple[int, int]]:
    """Generate all valid (start_idx, end_idx) pairs within duration limits."""
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


def _score_candidate(
    segments: list[Segment],
    transcript: Transcript,
    cfg: SelectionConfig,
    audio_profile: AudioEnergyProfile | None,
    heatmap: list[dict] | None,
) -> _CandidateWindow:
    start = segments[0].start
    end = segments[-1].end
    duration = max(1e-6, end - start)
    text = " ".join(s.text.strip() for s in segments).strip()
    words = [w for s in segments for w in s.words]
    word_count = len(words) or len(text.split())

    # Speech density / pacing score
    density = _density_score(word_count / duration)

    # Keywords score
    hits = _keyword_hits(text, cfg.keywords)
    keywords_norm = _clamp(hits / _KEYWORD_SATURATION)

    # Viral hook score (first 4 seconds / first sentence)
    hook = _viral_hook_score(text, segments)

    # Sentence & thought completeness score
    completeness = _completeness_score(segments)

    # Emotional reactions / high arousal score
    reaction_score = _emotional_reaction_score(text)

    # Filler and silence penalties
    filler = _filler_penalty(text, word_count)
    silence = _silence_penalty(words, start, end)

    # Audio energy / volume surge score
    audio_score = 0.5
    peak_ratio = 1.0
    if audio_profile and audio_profile.has_data:
        peak_ratio, _, audio_score = audio_profile.score_window(start, end)

    # Heatmap replay score
    heatmap_score = 0.5
    if heatmap:
        _, _, heatmap_score = score_heatmap_window(heatmap, start, end)

    weights = cfg.weights
    components = {
        "speech_density": round(weights.speech_density * density, 4),
        "keywords": round(weights.keywords * keywords_norm, 4),
        "hook": round(weights.hook * hook, 4),
        "completeness": round(weights.completeness * completeness, 4),
        "emotional_arousal": round(0.5 * reaction_score, 4),
        "filler": round(-(weights.filler * filler), 4),
        "silence": round(-(weights.silence * silence), 4),
        "audio_energy": round(weights.audio_energy * audio_score, 4),
        "heatmap": round(weights.heatmap * heatmap_score, 4),
    }
    score = sum(components.values())

    reason = _format_reason(components, hits, hook, audio_score, peak_ratio, heatmap_score)

    return _CandidateWindow(
        start=start,
        end=end,
        score=round(score, 4),
        components=components,
        text=text,
        reason=reason,
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


def _viral_hook_score(text: str, segments: list[Segment]) -> float:
    if not text:
        return 0.0

    first_sentence = _first_sentence(text).strip()
    lowered = first_sentence.lower()
    score = 0.0

    # 1. Regex pattern matches for high-converting viral hooks
    for pattern in _STRONG_HOOK_PATTERNS:
        if re.search(pattern, lowered):
            score += 0.50
            break

    # 2. Hook openers ("how", "why", "what", "never", "stop", etc.)
    for opener in _HOOK_OPENERS:
        if lowered == opener or (lowered.startswith(opener) and lowered[len(opener)] in " ,.:;!?-'\"("):
            score += 0.35
            break

    # 3. Question hook
    if first_sentence.endswith("?"):
        score += 0.30

    # 4. Specificity (numbers, metrics)
    if any(c.isdigit() for c in first_sentence):
        score += 0.20

    # 5. Clean sentence start
    if text[:1].isupper() or text[:1] in "\"':":
        score += 0.15
    elif text[:1].islower():
        score -= 0.30

    return _clamp(score)


def _emotional_reaction_score(text: str) -> float:
    lowered = text.lower()
    hits = sum(1 for phrase in _EMOTIONAL_REACTION_WORDS if phrase in lowered)
    return _clamp(hits / 3.0)


def _completeness_score(segments: list[Segment]) -> float:
    if not segments:
        return 0.0
    first = segments[0].text.strip()
    last = segments[-1].text.strip()
    start_ok = bool(first) and (first[0].isupper() or first[0].isdigit() or first[0] in "\"'([" )
    end_ok = bool(last) and last[-1] in ".?!…"
    return (0.5 if start_ok else 0.0) + (0.5 if end_ok else 0.0)


def _filler_penalty(text: str, word_count: int) -> float:
    if word_count <= 0:
        return 0.0
    lowered = f" {text.lower()} "
    hits = sum(lowered.count(phrase) for phrase in _FILLERS)
    return _clamp((hits / word_count) / _FILLER_SATURATION)


def _silence_penalty(words: Sequence[Word], start: float, end: float) -> float:
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


def _format_reason(
    components: dict[str, float],
    hits: int,
    hook: float,
    audio_score: float,
    peak_ratio: float,
    heatmap_score: float,
) -> str:
    strengths: list[str] = []
    if peak_ratio >= 2.5:
        strengths.append(f"audio excitement peak ({peak_ratio:.1f}x volume)")
    elif audio_score >= 0.7:
        strengths.append("high audio energy")
    if heatmap_score >= 0.75:
        strengths.append("high viewer replay peak")
    if hook >= 0.6:
        strengths.append("strong opening hook")
    if hits >= 2:
        strengths.append(f"{hits} keyword hits")
    return ", ".join(strengths) if strengths else "balanced"


# --------------------------------------------------------------------------- #
# Overlap Suppression
# --------------------------------------------------------------------------- #


def _suppress_overlaps(windows: list[_CandidateWindow], limit: int) -> list[_CandidateWindow]:
    """Greedy non-maximum suppression."""
    chosen: list[_CandidateWindow] = []
    for candidate in sorted(windows, key=lambda w: (-w.score, w.start)):
        if any(_overlaps(candidate, kept) for kept in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return chosen


def _overlaps(first: _CandidateWindow, second: _CandidateWindow) -> bool:
    return first.start < second.end and second.start < first.end


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _first_sentence(text: str) -> str:
    for idx, c in enumerate(text):
        if c in ".?!":
            return text[: idx + 1]
    return text


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
