"""Core data model shared by all stages.

All timestamps are seconds (float) from the start of the source video.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "probability": round(self.probability, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Word":
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data["text"]),
            probability=float(data.get("probability", 1.0)),
        )


@dataclass(frozen=True, slots=True)
class Segment:
    id: int
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [word.to_dict() for word in self.words],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            id=int(data["id"]),
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data["text"]),
            words=tuple(Word.from_dict(item) for item in data.get("words", ())),
        )


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str
    duration: float
    segments: tuple[Segment, ...]

    @property
    def words(self) -> tuple[Word, ...]:
        return tuple(word for segment in self.segments for word in segment.words)

    def words_between(self, start: float, end: float) -> list[Word]:
        """Words overlapping [start, end), clamped to the window."""
        return [
            word
            for word in self.words
            if word.end > start and word.start < end
        ]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(word.text.strip() for word in self.words_between(start, end)).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": round(self.duration, 3),
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        return cls(
            language=str(data.get("language", "")),
            duration=float(data.get("duration", 0.0)),
            segments=tuple(
                Segment.from_dict(item) for item in data.get("segments", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class Clip:
    """One selected segment of the source video."""

    index: int
    start: float
    end: float
    score: float
    text: str
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "score": round(self.score, 4),
            "text": self.text,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Clip":
        return cls(
            index=int(data["index"]),
            start=float(data["start"]),
            end=float(data["end"]),
            score=float(data.get("score", 0.0)),
            text=str(data.get("text", "")),
            reason=str(data.get("reason", "")),
        )
