"""Gemini LLM semantic clip selector.

Uses the official `google-genai` SDK with `gemini-3.8-flash` to evaluate the
transcript for viral hooks, narrative coherence, emotional punch, and standalone
appeal.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from ..boundaries import snap_clip_boundaries
from ..config import SelectionConfig
from ..formatting import format_duration
from ..models import Clip, Transcript
from .base import SelectionOutcome

if TYPE_CHECKING:
    from ..state import State

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an elite short-form video editor specializing in YouTube Shorts, TikTok, and Reels.
Your job is to analyze video transcripts and extract the most VIRAL, engaging, and self-contained clips.

A viral short MUST have:
1. Scroll-Stopping Hook (First 3 seconds): An immediate curiosity gap, surprising confession, question, drama, or bold premise.
2. Progressive Escalation: The tension, humor, or explanation builds without filler or trailing pauses.
3. Payoff / Punchline / Climax: A satisfying comedic punchline, revelation, or high-stakes reaction at the end.
4. Standalone Coherence: A viewer with zero background context must immediately understand what is happening.
5. Clean Boundaries: The clip MUST start at the beginning of a complete sentence and end right after the punchline/payoff sentence.

Duration Constraint: Each clip MUST last between {min_seconds:.0f} and {max_seconds:.0f} seconds.
"""


def resolve_api_key(cfg: SelectionConfig) -> str | None:
    """Resolve Gemini API key from direct config, env var, or .env file."""
    from ..env import load_env_file
    load_env_file()

    if cfg.api_key and cfg.api_key.strip():
        return cfg.api_key.strip()
    env_field = (cfg.llm_api_key_env or "").strip()
    if env_field.startswith("AIzaSy") or (len(env_field) > 30 and not env_field.isidentifier()):
        return env_field
    if env_field and env_field in os.environ:
        val = os.environ[env_field].strip()
        if val:
            return val
    if "GEMINI_API_KEY" in os.environ:
        val = os.environ["GEMINI_API_KEY"].strip()
        if val:
            return val
    return None


class GeminiSelector:
    """Selects viral clips using Gemini semantic understanding."""

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

        api_key = resolve_api_key(cfg)
        if not api_key:
            log.warning(
                "[select] Gemini API key not found (set 'api_key' in config.yaml or GEMINI_API_KEY env). "
                "Falling back to hybrid multimodal selector.",
            )
            from .hybrid import HybridSelector
            return HybridSelector().select(transcript, cfg, state=state)

        log.info("[select] Using Gemini LLM (%s) for semantic viral selection", cfg.llm_model)

        try:
            clips = self._query_gemini(transcript, cfg, state, api_key)
            if clips:
                return SelectionOutcome(tuple(clips))
            log.warning("Gemini returned no clips; falling back to hybrid selector")
        except Exception as exc:
            log.warning("Gemini selection encountered an error: %s; falling back", exc)

        from .hybrid import HybridSelector
        return HybridSelector().select(transcript, cfg, state=state)

    def _query_gemini(
        self,
        transcript: Transcript,
        cfg: SelectionConfig,
        state: State | None,
        api_key: str,
    ) -> list[Clip]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        model = cfg.llm_model or "gemini-3.8-flash"

        video_title = ""
        if state is not None:
            info = state.get("source_info") or {}
            video_title = str(info.get("title", ""))

        formatted_transcript = _format_transcript_for_llm(transcript)
        user_prompt = (
            f"Video Title: {video_title or 'Untitled Video'}\n"
            f"Total Duration: {transcript.duration:.1f}s\n"
            f"Select up to {cfg.max_clips} viral clips (each between {cfg.min_seconds:.0f}s "
            f"and {cfg.max_seconds:.0f}s).\n\n"
            f"Transcript with timestamps:\n{formatted_transcript}\n\n"
            "Return ONLY a JSON array with this schema for each selected clip:\n"
            "[\n"
            "  {\n"
            '    "start": <float seconds>,\n'
            '    "end": <float seconds>,\n'
            '    "score": <float between 1.0 and 10.0>,\n'
            '    "hook": "<short description of the opening hook>",\n'
            '    "payoff": "<short description of the payoff/punchline>",\n'
            '    "reason": "<why this clip is viral>",\n'
            '    "title": "<catchy short title under 50 characters>"\n'
            "  }\n"
            "]"
        )

        sys_prompt = SYSTEM_PROMPT.format(
            min_seconds=cfg.min_seconds, max_seconds=cfg.max_seconds
        )

        log.info("querying Gemini (%s) for viral clip selection...", model)
        response_text = ""
        try:
            # First attempt: modern interactions API
            interaction = client.interactions.create(
                model=model,
                input=f"{sys_prompt}\n\n{user_prompt}",
            )
            response_text = interaction.output_text or ""
        except Exception:
            # Fallback: standard models API
            config = types.GenerateContentConfig(
                system_instruction=sys_prompt,
                response_mime_type="application/json",
            )
            response = client.models.generate_content(
                model=model,
                contents=user_prompt,
                config=config,
            )
            response_text = response.text or ""

        parsed = _parse_llm_json(response_text)
        if not parsed:
            return []

        # Validate, snap boundaries, and suppress overlaps
        candidates: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item["start"])
                end = float(item["end"])
                score = float(item.get("score", 5.0))
                if end <= start:
                    continue
                candidates.append(
                    {
                        "start": start,
                        "end": end,
                        "score": score,
                        "title": str(item.get("title", "")).strip(),
                        "reason": str(item.get("reason", "")).strip(),
                        "hook": str(item.get("hook", "")).strip(),
                        "payoff": str(item.get("payoff", "")).strip(),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue

        # Sort descending by score
        candidates.sort(key=lambda c: -c["score"])
        chosen_clips: list[Clip] = []

        for item in candidates:
            snapped = snap_clip_boundaries(
                transcript,
                item["start"],
                item["end"],
                cfg.min_seconds,
                cfg.max_seconds,
            )
            if snapped.duration < cfg.min_seconds or snapped.duration > cfg.max_seconds:
                continue

            # Overlap check
            if any(
                snapped.start < kept.end and kept.start < snapped.end
                for kept in chosen_clips
            ):
                continue

            reason_parts = [r for r in [item["hook"], item["reason"]] if r]
            reason = " | ".join(reason_parts) if reason_parts else "selected by Gemini"

            clip = Clip(
                index=len(chosen_clips),
                start=snapped.start,
                end=snapped.end,
                score=round(item["score"], 4),
                text=snapped.text,
                reason=reason,
                components={"gemini_score": round(item["score"], 4)},
                title=item["title"],
            )
            chosen_clips.append(clip)
            if len(chosen_clips) >= cfg.max_clips:
                break

        return chosen_clips


def _format_transcript_for_llm(transcript: Transcript) -> str:
    """Format transcript segments into a clear, timestamped script for LLM analysis."""
    lines: list[str] = []
    for seg in transcript.segments:
        m, s = divmod(int(seg.start), 60)
        lines.append(f"[{m:02d}:{s:02d}] {seg.text.strip()}")
    return "\n".join(lines)


def _parse_llm_json(raw: str) -> list[dict[str, Any]]:
    """Parse JSON array from model output, handling potential markdown fences."""
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*(\[\s*\{.*?\}\s*\])\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    elif text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        # Fallback: search for first [ and last ]
        first_bracket = text.find("[")
        last_bracket = text.rfind("]")
        if first_bracket != -1 and last_bracket > first_bracket:
            try:
                data = json.loads(text[first_bracket : last_bracket + 1])
                if isinstance(data, list):
                    return data
            except Exception:
                pass
    return []
