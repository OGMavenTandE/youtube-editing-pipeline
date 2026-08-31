from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from pipeline.layouts import PictureTag

AssetKind = Literal["none", "broll", "site", "card"]
ASSET_KINDS: tuple[AssetKind, ...] = ("none", "broll", "site", "card")
BodyTag = Literal["overlay", "pip", "nothing"]
BODY_TAGS: tuple[BodyTag, ...] = ("overlay", "pip", "nothing")
BeatRole = Literal["body", "open", "close"]
FieldSource = Literal["empty", "user", "auto"]
FIELD_SOURCES: tuple[FieldSource, ...] = ("empty", "user", "auto")
TALK_POINT_COUNT = 3
TALK_CARDS_PER_POINT = 3


def _coerce_field_source(value: object) -> FieldSource:
    raw = str(value or "empty").strip().lower()
    if raw in FIELD_SOURCES:
        return raw  # type: ignore[return-value]
    return "empty"


def field_is_locked(source: FieldSource | str, value: object) -> bool:
    """User-filled text or a user still cannot be overwritten later."""
    return str(source or "").strip().lower() == "user" and bool(str(value or "").strip())


def _pad_str_list(value: object, size: int) -> list[str]:
    items = [str(item) for item in (value or [])]
    while len(items) < size:
        items.append("")
    return items[:size]


def _pad_source_list(value: object, size: int) -> list[FieldSource]:
    items = [_coerce_field_source(item) for item in (value or [])]
    while len(items) < size:
        items.append("empty")
    return items[:size]


def _coerce_asset_kind(value: object) -> AssetKind:
    raw = str(value or "none").strip().lower()
    if raw in ASSET_KINDS:
        return raw  # type: ignore[return-value]
    return "none"


def _empty_ref_to_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    """Legacy takeaway. The locked kit does not paint these."""

    text: str = Field(..., min_length=1, description="Short on-screen takeaway.")
    kind: Literal["takeaway", "stat", "quote"] = "takeaway"


class HostIdentity(BaseModel):
    """Locked lower-third copy. Config, not model output."""

    name: str = "Scott Mastin"
    title_line: str = "President, AI Eval Corp · SDVOSB · Independent T&E"
    affiliations: str = "Army Research Lab · Project Maven · CDAO"
    mission: str = "AI test and evaluation for the Department of War and the IC"
    find_me_kicker: str = "FIND ME"
    find_me: list[str] = Field(
        default_factory=lambda: [
            "scottmastin.com",
            "linkedin.com/in/scottmastin",
            "aieval.org",
        ]
    )


class BookendCard(BaseModel):
    """Dedicated open/close plate. Not an overlay|pip|nothing body tag."""

    kicker: str = ""
    headline: str = ""
    icon: str = ""


class TalkPoint(BaseModel):
    """One of three talk points. Cards are overlay headlines. titles are gold kickers.

    image_text is the left-third gold line painted on the PiP still, not a card kicker.
    """

    platform: str = ""
    still_path: str = ""
    image_text: str = ""
    cards: list[str] = Field(default_factory=lambda: ["", "", ""])
    titles: list[str] = Field(default_factory=lambda: ["", "", ""])
    platform_source: FieldSource = "empty"
    still_source: FieldSource = "empty"
    image_text_source: FieldSource = "empty"
    card_sources: list[FieldSource] = Field(default_factory=lambda: ["empty", "empty", "empty"])
    title_sources: list[FieldSource] = Field(default_factory=lambda: ["empty", "empty", "empty"])

    @field_validator("platform_source", "still_source", "image_text_source", mode="before")
    @classmethod
    def coerce_point_source(cls, value: object) -> FieldSource:
        return _coerce_field_source(value)

    @field_validator("cards", "titles", mode="before")
    @classmethod
    def pad_cards(cls, value: object) -> list[str]:
        return _pad_str_list(value, TALK_CARDS_PER_POINT)

    @field_validator("card_sources", "title_sources", mode="before")
    @classmethod
    def pad_card_sources(cls, value: object) -> list[FieldSource]:
        return _pad_source_list(value, TALK_CARDS_PER_POINT)

    def platform_locked(self) -> bool:
        return field_is_locked(self.platform_source, self.platform)

    def still_locked(self) -> bool:
        return field_is_locked(self.still_source, self.still_path)

    def image_text_locked(self) -> bool:
        return field_is_locked(self.image_text_source, self.image_text)

    def card_locked(self, index: int) -> bool:
        if index < 0 or index >= TALK_CARDS_PER_POINT:
            return False
        return field_is_locked(self.card_sources[index], self.cards[index])

    def title_locked(self, index: int) -> bool:
        if index < 0 or index >= TALK_CARDS_PER_POINT:
            return False
        return field_is_locked(self.title_sources[index], self.titles[index])


def empty_talk_points() -> list[TalkPoint]:
    return [TalkPoint() for _ in range(TALK_POINT_COUNT)]


class TalkSheet(BaseModel):
    """Job metadata for bookend cards and Point 1–3 copy. Not invented by the tagger.

    open_card is the overview plate (title kicker + two-line thesis).
    close_card is the locked CTA. Neither is a body subpoint.
    title / exec_headline / close_* are aliases kept for older job JSON.
    points[] holds platform, still_path, and up to three overlay cards each.
    """

    title: str = ""
    exec_headline: str = ""
    exec_notes: str = Field(default="", description="Spoken exec notes. Not painted.")
    title_source: FieldSource = "empty"
    exec_headline_source: FieldSource = "empty"
    open_card: BookendCard = Field(default_factory=BookendCard)
    close_card: BookendCard = Field(
        default_factory=lambda: BookendCard(
            kicker="WORK WITH ME",
            headline="Independent AI T&E.\nVendor-agnostic.",
            icon="share",
        )
    )
    close_kicker: str = "WORK WITH ME"
    close_headline: str = "Independent AI T&E.\nVendor-agnostic."
    close_icon: str = "share"
    open_icon: str = "bar_chart"
    points: list[TalkPoint] = Field(default_factory=empty_talk_points)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_sources(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if "title_source" not in payload and str(payload.get("title") or "").strip():
            payload["title_source"] = "user"
        if "exec_headline_source" not in payload and str(payload.get("exec_headline") or "").strip():
            payload["exec_headline_source"] = "user"
        points = payload.get("points")
        if isinstance(points, list):
            inferred: list[object] = []
            for item in points:
                if not isinstance(item, dict):
                    inferred.append(item)
                    continue
                row = dict(item)
                if "platform_source" not in row and str(row.get("platform") or "").strip():
                    row["platform_source"] = "user"
                if "still_source" not in row and str(row.get("still_path") or "").strip():
                    row["still_source"] = "user"
                if "image_text_source" not in row and str(row.get("image_text") or "").strip():
                    row["image_text_source"] = "user"
                cards = _pad_str_list(row.get("cards"), TALK_CARDS_PER_POINT)
                if "card_sources" not in row:
                    row["card_sources"] = ["user" if card.strip() else "empty" for card in cards]
                titles = _pad_str_list(row.get("titles"), TALK_CARDS_PER_POINT)
                if "title_sources" not in row:
                    row["title_sources"] = ["user" if title.strip() else "empty" for title in titles]
                inferred.append(row)
            payload["points"] = inferred
        return payload

    @field_validator("title_source", "exec_headline_source", mode="before")
    @classmethod
    def coerce_sheet_source(cls, value: object) -> FieldSource:
        return _coerce_field_source(value)

    @field_validator("points", mode="before")
    @classmethod
    def pad_points(cls, value: object) -> list[object]:
        items = list(value or [])
        while len(items) < TALK_POINT_COUNT:
            items.append({})
        return items[:TALK_POINT_COUNT]

    @model_validator(mode="after")
    def sync_bookend_aliases(self) -> "TalkSheet":
        if not self.open_card.kicker.strip() and self.title.strip():
            self.open_card.kicker = self.title.strip()
        if not self.open_card.headline.strip() and self.exec_headline.strip():
            self.open_card.headline = self.exec_headline.strip()
        if not self.open_card.icon.strip() and self.open_icon.strip():
            self.open_card.icon = self.open_icon.strip()
        if not self.title.strip() and self.open_card.kicker.strip():
            self.title = self.open_card.kicker
        if not self.exec_headline.strip() and self.open_card.headline.strip():
            self.exec_headline = self.open_card.headline
        if not self.close_card.kicker.strip() and self.close_kicker.strip():
            self.close_card.kicker = self.close_kicker.strip()
        if not self.close_card.headline.strip() and self.close_headline.strip():
            self.close_card.headline = self.close_headline.strip()
        if not self.close_card.icon.strip() and self.close_icon.strip():
            self.close_card.icon = self.close_icon.strip()
        if not self.close_kicker.strip() and self.close_card.kicker.strip():
            self.close_kicker = self.close_card.kicker
        if not self.close_headline.strip() and self.close_card.headline.strip():
            self.close_headline = self.close_card.headline
        if len(self.points) < TALK_POINT_COUNT:
            self.points.extend(TalkPoint() for _ in range(TALK_POINT_COUNT - len(self.points)))
        self.points = self.points[:TALK_POINT_COUNT]
        return self

    def title_locked(self) -> bool:
        return field_is_locked(self.title_source, self.title)

    def headline_locked(self) -> bool:
        return field_is_locked(self.exec_headline_source, self.exec_headline)

    def headline_lines(self) -> tuple[str, str]:
        text = self.exec_headline.replace("\r\n", "\n")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            return first.strip(), rest.strip()
        return text.strip(), ""

    def set_headline_lines(self, line1: str, line2: str, *, source: FieldSource | None = None) -> None:
        parts = [line1.strip(), line2.strip()]
        self.exec_headline = "\n".join(part for part in parts if part)
        if source is not None:
            self.exec_headline_source = source
        if self.exec_headline.strip():
            self.open_card.headline = self.exec_headline


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
    """Copy that fills a locked kit template. No layout, font, or color fields."""

    kicker: str = Field(
        default="",
        description="Gold eyebrow (overlay) or gold number (pip).",
    )
    title: str = Field(default="", description="White headline / pip sub-line.")
    icon: str = Field(default="", description="Line-art icon name: bar_chart, shield, share, …")
    quote: str = Field(default="", description="Optional short pip quote.")
    still_query: str = Field(
        default="",
        description="Named-platform still query for a pip beat (DVIDS file first).",
    )
    bullets: list[str] = Field(default_factory=list)
    lower_third_title: str = ""
    lower_third_subtitle: str = ""
    slide_id: str = Field(
        default="",
        description="Stable id for a rendered chrome PNG.",
    )
    asset_path: str = Field(
        default="",
        description="Resolved still or rendered chrome PNG.",
    )
    lower_third_path: str = Field(
        default="",
        description="Rendered bookend PNG when the compositor stamps one.",
    )

    @model_validator(mode="before")
    @classmethod
    def alias_lower_third(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if not data.get("lower_third_title") and data.get("lower_third"):
            data = {**data, "lower_third_title": data["lower_third"]}
        return data

    @field_validator("bullets")
    @classmethod
    def cap_bullets(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned[:5]


class Scene(TimeRange):
    """One tagged beat on the trimmed timeline. Sparse. Most beats are nothing."""

    said: str = Field(default="", description="What is spoken on this beat.")
    shown: str = Field(default="", description="What is on screen, in words.")
    asset_kind: AssetKind = Field(
        default="none",
        description="Legacy none | broll | site | card. Prefer layout + still_query.",
    )
    asset_ref: str | None = Field(
        default=None,
        description="Optional local path or URL. Missing file is treated as none.",
    )
    layout: PictureTag = PictureTag.NOTHING
    role: BeatRole = "body"
    reason: str = Field(default="", description="Why this tag for this spoken beat.")
    graphic: GraphicCard = Field(default_factory=GraphicCard)
    micro_events: list[MicroEvent] = Field(default_factory=list)

    @field_validator("asset_kind", mode="before")
    @classmethod
    def coerce_asset_kind(cls, value: object) -> AssetKind:
        return _coerce_asset_kind(value)

    @field_validator("asset_ref", mode="before")
    @classmethod
    def coerce_asset_ref(cls, value: object) -> str | None:
        return _empty_ref_to_none(value)

    @field_validator("layout", mode="before")
    @classmethod
    def coerce_layout(cls, value: object) -> PictureTag:
        return PictureTag.coerce(value, allow_bookend=True)

    @property
    def tag(self) -> PictureTag:
        return self.layout


class PlannedScene(TimeRange):
    """Gemini body-beat payload. Tags only: overlay | pip | nothing."""

    said: str = ""
    shown: str = ""
    asset_kind: AssetKind = "none"
    asset_ref: str | None = None
    layout: PictureTag = PictureTag.NOTHING
    reason: str = ""
    graphic: GraphicCard = Field(default_factory=GraphicCard)

    @field_validator("asset_kind", mode="before")
    @classmethod
    def coerce_asset_kind(cls, value: object) -> AssetKind:
        return _coerce_asset_kind(value)

    @field_validator("asset_ref", mode="before")
    @classmethod
    def coerce_asset_ref(cls, value: object) -> str | None:
        return _empty_ref_to_none(value)

    @field_validator("layout", mode="before")
    @classmethod
    def coerce_body_tag(cls, value: object) -> PictureTag:
        return PictureTag.coerce(value, allow_bookend=False)

    def to_scene(self) -> Scene:
        return Scene(
            start=self.start,
            end=self.end,
            said=self.said,
            shown=self.shown,
            asset_kind=self.asset_kind,
            asset_ref=self.asset_ref,
            layout=self.layout,
            role="body",
            reason=self.reason,
            graphic=self.graphic,
            micro_events=[],
        )


class TaggedBeat(TimeRange):
    """Persisted kit beat. Written before encode so retries skip the model."""

    tag: PictureTag = PictureTag.NOTHING
    role: BeatRole = "body"
    said: str = ""
    kicker: str = ""
    headline: str = ""
    icon: str = ""
    quote: str = ""
    still_query: str = ""
    still_path: str = ""
    reason: str = ""

    @field_validator("tag", mode="before")
    @classmethod
    def coerce_tag(cls, value: object) -> PictureTag:
        return PictureTag.coerce(value, allow_bookend=True)


class TaggedBeatList(BaseModel):
    version: int = 1
    duration: float = 0.0
    identity: HostIdentity = Field(default_factory=HostIdentity)
    talk_sheet: TalkSheet = Field(default_factory=TalkSheet)
    beats: list[TaggedBeat] = Field(default_factory=list)


class TranscriptCue(TimeRange):
    text: str = ""


class TimedTranscript(BaseModel):
    duration: float = 0.0
    full_text: str = ""
    cues: list[TranscriptCue] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_text_alias(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if not str(data.get("full_text") or "").strip() and data.get("text"):
            data = {**data, "full_text": data["text"]}
        return data

    @property
    def text(self) -> str:
        if self.full_text.strip():
            return self.full_text.strip()
        return " ".join(cue.text.strip() for cue in self.cues if cue.text.strip()).strip()

    def slice(self, start: float, end: float) -> TimedTranscript:
        cues = [
            TranscriptCue(
                start=max(cue.start, start),
                end=min(cue.end, end),
                text=cue.text,
            )
            for cue in self.cues
            if cue.end > start and cue.start < end and cue.text.strip()
        ]
        return TimedTranscript(
            duration=max(0.0, end - start),
            full_text="",
            cues=cues,
        )

    def window_text(self, start: float, end: float) -> str:
        chunk = self.slice(start, end)
        if chunk.cues:
            return "\n".join(
                f"[{cue.start:.2f}-{cue.end:.2f}] {cue.text.strip()}" for cue in chunk.cues
            )
        if self.text:
            return (
                f"(No timed cues in {start:.1f}-{end:.1f}s. Full transcript follows.)\n"
                f"{self.text}"
            )
        return ""

    @classmethod
    def from_plain(cls, text: str, duration: float) -> TimedTranscript:
        cleaned = text.strip()
        cues = [TranscriptCue(start=0.0, end=duration, text=cleaned)] if cleaned else []
        return cls(duration=duration, full_text=cleaned, cues=cues)


class ChapterMarker(BaseModel):
    start: float = Field(..., ge=0.0, description="Chapter start in seconds on the final cut.")
    title: str = Field(..., min_length=1)

    @model_validator(mode="before")
    @classmethod
    def alias_start_seconds(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if data.get("start") is None and data.get("start_seconds") is not None:
            data = {**data, "start": data["start_seconds"]}
        return data


class YouTubeMetadata(BaseModel):
    titles: list[str] = Field(
        default_factory=list,
        description="Up to five high-CTR YouTube title options.",
    )
    description: str = Field(default="", description="SEO YouTube description.")
    chapters: list[ChapterMarker] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    title_index: int = Field(
        default=0,
        ge=0,
        le=4,
        description="Which titles[] entry is the paste title and thumbnail line.",
    )

    @model_validator(mode="before")
    @classmethod
    def alias_title_options(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if not data.get("titles") and data.get("title_options"):
            data = {**data, "titles": data["title_options"]}
        return data

    @field_validator("titles")
    @classmethod
    def cap_titles(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned[:5]

    @field_validator("tags")
    @classmethod
    def cap_tags(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned[:15]


class DirectorPlan(BaseModel):
    """Slim schema sent to Gemini for a timeline window: scenes plus metadata."""

    scenes: list[PlannedScene] = Field(default_factory=list)
    metadata: YouTubeMetadata = Field(default_factory=YouTubeMetadata)


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
    talk_sheet: TalkSheet = Field(default_factory=TalkSheet)
    identity: HostIdentity = Field(default_factory=HostIdentity)

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

    def to_trimmed_edge(self, source_time: float, *, edge: str = "start") -> float | None:
        """Map a range edge, snapping into the nearest keep if the instant was cut."""
        mapped = self.to_trimmed(source_time)
        if mapped is not None:
            return mapped
        offset = 0.0
        if edge == "start":
            for kept in self.kept_ranges:
                if kept.end > source_time:
                    return offset + max(0.0, source_time - kept.start)
                offset += kept.duration
            return None
        last_end: float | None = None
        for kept in self.kept_ranges:
            if kept.start < source_time:
                last_end = offset + min(kept.duration, max(0.0, source_time - kept.start))
            offset += kept.duration
        return last_end

    def remap_range(self, start: float, end: float) -> TimeRange | None:
        mapped_start = self.to_trimmed_edge(start, edge="start")
        mapped_end = self.to_trimmed_edge(end, edge="end")
        if mapped_start is None or mapped_end is None:
            return None
        if mapped_end - mapped_start < 0.04:
            return None
        return TimeRange(start=mapped_start, end=mapped_end)


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
