from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from pipeline.layouts import LayoutKind


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


class MicroEventKind(str, Enum):
    PUNCH_IN = "punch_in"
    TEXT = "text"
    CUT = "cut"


class MicroEvent(BaseModel):
    """Light visual reset inside a layout hold. Not a layout swap."""

    start: float = Field(..., ge=0.0)
    end: float = Field(..., ge=0.0)
    kind: Literal["punch_in", "text", "cut"] = "punch_in"
    text: str = Field(default="", description="On-screen line when kind is text.")
    scale: float = Field(default=1.15, ge=1.0, le=1.4, description="Punch-in zoom factor.")


class GraphicCard(BaseModel):
    """Slide / lower-third copy for one scene. Reuse across adjacent scenes when the topic holds."""

    title: str = ""
    bullets: list[str] = Field(default_factory=list)
    lower_third_title: str = ""
    lower_third_subtitle: str = ""
    slide_id: str = Field(
        default="",
        description="Stable id so adjacent scenes can share one generated slide.",
    )


class Scene(TimeRange):
    """One layout beat on the trimmed timeline. A 20-minute cut has 50-80 of these."""

    layout: LayoutKind = LayoutKind.FULL_FRAME
    reason: str = Field(default="", description="Why this layout for this spoken beat.")
    graphic: GraphicCard = Field(default_factory=GraphicCard)
    micro_events: list[MicroEvent] = Field(default_factory=list)


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
    scenes: list[Scene] = Field(
        default_factory=list,
        description="Ordered layout beats covering 0..duration. Expect 50-80 on a 20-minute cut.",
    )
    talking_head_cuts: list[TalkingHeadCut] = Field(
        default_factory=list,
        description="Optional extra A-roll keep ranges after silence trim. Empty means keep all.",
    )
    lower_thirds: list[LowerThird] = Field(
        default_factory=list,
        description="Legacy parallel list. Prefer scene.graphic.lower_third_*.",
    )
    broll: list[BRollCue] = Field(default_factory=list)
    overlays: list[OverlayCallout] = Field(default_factory=list)
    metadata: YouTubeMetadata = Field(default_factory=YouTubeMetadata)

    @classmethod
    def empty(cls) -> EditScript:
        return cls()

    def collected_lower_thirds(self) -> list[LowerThird]:
        cards = list(self.lower_thirds)
        for scene in self.scenes:
            title = scene.graphic.lower_third_title
            if not title:
                continue
            cards.append(
                LowerThird(
                    start=scene.start,
                    end=scene.end,
                    title=title,
                    subtitle=scene.graphic.lower_third_subtitle,
                )
            )
        return cards

    def collected_text_overlays(self) -> list[OverlayCallout]:
        cards = list(self.overlays)
        for scene in self.scenes:
            for event in scene.micro_events:
                if event.kind != "text" or not event.text.strip():
                    continue
                cards.append(
                    OverlayCallout(
                        start=event.start,
                        end=max(event.end, event.start + 0.4),
                        text=event.text,
                        kind="takeaway",
                    )
                )
        return cards

    def collected_punch_ins(self) -> list[MicroEvent]:
        events: list[MicroEvent] = []
        for scene in self.scenes:
            events.extend(event for event in scene.micro_events if event.kind == "punch_in")
        return events


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
