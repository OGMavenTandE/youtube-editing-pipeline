from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TimeRange(BaseModel):
    """Closed-open time range in seconds on the current working timeline."""

    start: float = Field(..., ge=0.0, description="Start time in seconds.")
    end: float = Field(..., ge=0.0, description="End time in seconds.")

    @field_validator("end")
    @classmethod
    def end_after_start(cls, value: float, info: object) -> float:
        start = getattr(getattr(info, "data", {}), "get", lambda *_: None)("start")
        if start is not None and value < start:
            raise ValueError("end must be >= start")
        return value

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class TalkingHeadCut(TimeRange):
    """A keep-range on the talking-head A-roll after silence trim."""

    reason: str = Field(default="", description="Why this beat stays on camera.")


class LowerThird(TimeRange):
    """Clean name/title card burned into the lower third of the frame."""

    title: str = Field(..., min_length=1, description="Primary lower-third line.")
    subtitle: str = Field(default="", description="Optional secondary line.")


class BRollCue(TimeRange):
    """B-roll or slide insert. PiP by default; full-frame when transition is cut/fade."""

    query: str = Field(
        default="",
        description="Search phrase or topic used to match a local B-roll file.",
    )
    transition: Literal["cut", "fade", "pip"] = "pip"
    asset_path: str | None = Field(
        default=None,
        description="Optional local media path. Leave null if the compositor should resolve it.",
    )


class OverlayCallout(TimeRange):
    """On-screen takeaway that is not a lower-third name card."""

    text: str = Field(..., min_length=1, description="Short on-screen takeaway.")
    kind: Literal["takeaway", "stat", "quote"] = "takeaway"


class ChapterMarker(BaseModel):
    start: float = Field(..., ge=0.0, description="Chapter start in seconds on the final cut.")
    title: str = Field(..., min_length=1)


class YouTubeMetadata(BaseModel):
    titles: list[str] = Field(
        default_factory=list,
        description="Up to five high-CTR YouTube title options.",
    )
    description: str = Field(default="", description="SEO YouTube description.")
    chapters: list[ChapterMarker] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("titles")
    @classmethod
    def cap_titles(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned[:5]


class EditScript(BaseModel):
    """Director output consumed by the compositor. Timestamps are on the trimmed cut."""

    transcript: str = Field(default="", description="Full transcript of the trimmed audio.")
    talking_head_cuts: list[TalkingHeadCut] = Field(
        default_factory=list,
        description="Optional extra A-roll keep ranges after silence trim. Empty means keep all.",
    )
    lower_thirds: list[LowerThird] = Field(default_factory=list)
    broll: list[BRollCue] = Field(default_factory=list)
    overlays: list[OverlayCallout] = Field(default_factory=list)
    metadata: YouTubeMetadata = Field(default_factory=YouTubeMetadata)

    @classmethod
    def empty(cls) -> EditScript:
        return cls()


class SilenceCutMap(BaseModel):
    """Maps original source time to the trimmed timeline."""

    kept_ranges: list[TimeRange] = Field(default_factory=list)
    removed_ranges: list[TimeRange] = Field(default_factory=list)
    original_duration: float = 0.0
    trimmed_duration: float = 0.0

    def to_trimmed(self, source_time: float) -> float | None:
        """Map a timestamp on the original file onto the trimmed file, or None if cut."""
        offset = 0.0
        for kept in self.kept_ranges:
            if kept.start <= source_time <= kept.end:
                return offset + (source_time - kept.start)
            offset += kept.duration
        return None


class SilenceTrimResult(BaseModel):
    output_path: Path
    cut_map: SilenceCutMap
    backend: str = "pydub-ffmpeg"

    @classmethod
    def passthrough(cls, path: Path, duration: float) -> SilenceTrimResult:
        cut_map = SilenceCutMap(
            kept_ranges=[TimeRange(start=0.0, end=duration)],
            removed_ranges=[],
            original_duration=duration,
            trimmed_duration=duration,
        )
        return cls(output_path=path, cut_map=cut_map, backend="passthrough")
