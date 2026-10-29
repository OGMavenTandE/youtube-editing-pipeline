"""Edit-plan document: the cut list Booth renders and checks.

YAML is the hand-edited form. JSON is the same mapping. The schema is
`docs/edit-plan.schema.json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLAN_VERSION = 1
CAPTION_MODES = ("srt", "burn", "both")
BROLL_LAYOUTS = ("full", "pip-lower-right")


@dataclass
class NarrationLine:
    id: str
    text: str
    start: float
    end: float
    beat: int | None = None
    palette: str | None = None


@dataclass
class Segment:
    id: str
    kind: str
    source: str
    start: float
    duration: float
    in_point: float = 0.0
    out_point: float | None = None
    template: str | None = None
    template_text: str | None = None
    palette: str | None = None
    beat: int | None = None
    narration_ids: list[str] = field(default_factory=list)


@dataclass
class Broll:
    id: str
    source: str
    at: float
    duration: float
    in_point: float = 0.0
    layout: str = "full"
    note: str = ""


@dataclass
class MusicCue:
    id: str
    sfx: str
    at: float
    gain_db: float
    source: str | None = None
    duration: float | None = None
    duck_under_voice: bool = True
    note: str = ""


@dataclass
class CaptionCue:
    start: float
    end: float
    text: str


@dataclass
class Card:
    id: str
    text: str
    start: float
    duration: float
    style: str = "lower-third"


@dataclass
class Short:
    id: str
    title: str
    start: float
    end: float
    width: int = 1080
    height: int = 1920


@dataclass
class Beat:
    id: str
    title: str
    start: float | None = None


@dataclass
class PaletteSpan:
    name: str
    start: float
    end: float


@dataclass
class EditPlan:
    version: int = PLAN_VERSION
    series: str = "ai-survival"
    episode: str = ""
    title: str = ""
    width: int = 1920
    height: int = 1080
    fps: int = 30
    lead_in: float = 0.0
    voice: str | None = None
    captions_mode: str = "srt"
    audio_hook: str | None = None
    graphics_hook: str | None = None
    segments: list[Segment] = field(default_factory=list)
    broll: list[Broll] = field(default_factory=list)
    music: list[MusicCue] = field(default_factory=list)
    captions: list[CaptionCue] = field(default_factory=list)
    narration: list[NarrationLine] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    shorts: list[Short] = field(default_factory=list)
    beats: list[Beat] = field(default_factory=list)
    palettes: list[PaletteSpan] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def picture_duration(self) -> float:
        if self.segments:
            return max(segment.start + segment.duration for segment in self.segments)
        if self.narration:
            return max(line.end for line in self.narration)
        return 0.0


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def plan_to_dict(plan: EditPlan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "series": plan.series,
        "episode": plan.episode,
        "title": plan.title,
        "width": plan.width,
        "height": plan.height,
        "fps": plan.fps,
        "lead_in": plan.lead_in,
        "voice": plan.voice,
        "captions_mode": plan.captions_mode,
        "audio_hook": plan.audio_hook,
        "graphics_hook": plan.graphics_hook,
        "segments": [
            {
                "id": segment.id,
                "kind": segment.kind,
                "source": segment.source,
                "start": segment.start,
                "duration": segment.duration,
                "in": segment.in_point,
                "out": segment.out_point,
                "template": segment.template,
                "template_text": segment.template_text,
                "palette": segment.palette,
                "beat": segment.beat,
                "narration_ids": list(segment.narration_ids),
            }
            for segment in plan.segments
        ],
        "broll": [
            {
                "id": item.id,
                "source": item.source,
                "at": item.at,
                "duration": item.duration,
                "in": item.in_point,
                "layout": item.layout,
                "note": item.note,
            }
            for item in plan.broll
        ],
        "music": [
            {
                "id": cue.id,
                "sfx": cue.sfx,
                "at": cue.at,
                "gain_db": cue.gain_db,
                "source": cue.source,
                "duration": cue.duration,
                "duck_under_voice": cue.duck_under_voice,
                "note": cue.note,
            }
            for cue in plan.music
        ],
        "captions": [
            {"start": cue.start, "end": cue.end, "text": cue.text} for cue in plan.captions
        ],
        "narration": [
            {
                "id": line.id,
                "text": line.text,
                "start": line.start,
                "end": line.end,
                "beat": line.beat,
                "palette": line.palette,
            }
            for line in plan.narration
        ],
        "cards": [
            {
                "id": card.id,
                "text": card.text,
                "start": card.start,
                "duration": card.duration,
                "style": card.style,
            }
            for card in plan.cards
        ],
        "shorts": [
            {
                "id": short.id,
                "title": short.title,
                "start": short.start,
                "end": short.end,
                "width": short.width,
                "height": short.height,
            }
            for short in plan.shorts
        ],
        "beats": [{"id": beat.id, "title": beat.title, "start": beat.start} for beat in plan.beats],
        "palettes": [
            {"name": span.name, "start": span.start, "end": span.end} for span in plan.palettes
        ],
        "meta": plan.meta,
    }


def plan_from_dict(data: dict[str, Any]) -> EditPlan:
    if not isinstance(data, dict):
        raise ValueError("edit plan must be a mapping")
    return EditPlan(
        version=int(data.get("version") or PLAN_VERSION),
        series=str(data.get("series") or ""),
        episode=str(data.get("episode") or ""),
        title=str(data.get("title") or ""),
        width=int(data.get("width") or 1920),
        height=int(data.get("height") or 1080),
        fps=int(data.get("fps") or 30),
        lead_in=float(data.get("lead_in") or 0),
        voice=data.get("voice") or None,
        captions_mode=str(data.get("captions_mode") or "srt"),
        audio_hook=data.get("audio_hook") or None,
        graphics_hook=data.get("graphics_hook") or None,
        segments=[
            Segment(
                id=str(item["id"]),
                kind=str(item.get("kind") or "still"),
                source=str(item.get("source") or ""),
                start=float(item.get("start") or 0),
                duration=float(item["duration"]),
                in_point=float(item.get("in") or 0),
                out_point=_opt_float(item.get("out")),
                template=item.get("template") or None,
                template_text=item.get("template_text") or None,
                palette=item.get("palette") or None,
                beat=_opt_int(item.get("beat")),
                narration_ids=[str(value) for value in item.get("narration_ids") or []],
            )
            for item in data.get("segments") or []
        ],
        broll=[
            Broll(
                id=str(item["id"]),
                source=str(item.get("source") or ""),
                at=float(item.get("at") or 0),
                duration=float(item["duration"]),
                in_point=float(item.get("in") or 0),
                layout=str(item.get("layout") or "full"),
                note=str(item.get("note") or ""),
            )
            for item in data.get("broll") or []
        ],
        music=[
            MusicCue(
                id=str(item["id"]),
                sfx=str(item.get("sfx") or item["id"]),
                at=float(item.get("at") or 0),
                gain_db=float(item.get("gain_db") or 0),
                source=item.get("source") or None,
                duration=_opt_float(item.get("duration")),
                duck_under_voice=bool(item.get("duck_under_voice", True)),
                note=str(item.get("note") or ""),
            )
            for item in data.get("music") or []
        ],
        captions=[
            CaptionCue(
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(item.get("text") or ""),
            )
            for item in data.get("captions") or []
        ],
        narration=[
            NarrationLine(
                id=str(item["id"]),
                text=str(item.get("text") or ""),
                start=float(item.get("start") or 0),
                end=float(item["end"]),
                beat=_opt_int(item.get("beat")),
                palette=item.get("palette") or None,
            )
            for item in data.get("narration") or []
        ],
        cards=[
            Card(
                id=str(item["id"]),
                text=str(item.get("text") or ""),
                start=float(item.get("start") or 0),
                duration=float(item["duration"]),
                style=str(item.get("style") or "lower-third"),
            )
            for item in data.get("cards") or []
        ],
        shorts=[
            Short(
                id=str(item["id"]),
                title=str(item.get("title") or ""),
                start=float(item["start"]),
                end=float(item["end"]),
                width=int(item.get("width") or 1080),
                height=int(item.get("height") or 1920),
            )
            for item in data.get("shorts") or []
        ],
        beats=[
            Beat(
                id=str(item["id"]),
                title=str(item.get("title") or ""),
                start=_opt_float(item.get("start")),
            )
            for item in data.get("beats") or []
        ],
        palettes=[
            PaletteSpan(
                name=str(item["name"]),
                start=float(item["start"]),
                end=float(item["end"]),
            )
            for item in data.get("palettes") or []
        ],
        meta=dict(data.get("meta") or {}),
    )


def load_plan(path: Path) -> EditPlan:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    return plan_from_dict(data)


def save_plan(plan: EditPlan, path: Path) -> None:
    data = plan_to_dict(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    else:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, width=4096)


def validate_plan(plan: EditPlan) -> list[str]:
    """Return human-readable problems. An empty list means the plan is usable."""
    errors: list[str] = []
    if plan.version != PLAN_VERSION:
        errors.append(f"version must be {PLAN_VERSION}, got {plan.version}")
    if not plan.series:
        errors.append("series is empty")
    if plan.captions_mode not in CAPTION_MODES:
        errors.append(f"captions_mode must be one of {', '.join(CAPTION_MODES)}")
    seen: set[str] = set()
    for segment in plan.segments:
        if segment.duration <= 0:
            errors.append(f"segment {segment.id} has no duration")
        if segment.kind not in {"still", "clip"}:
            errors.append(f"segment {segment.id} kind must be still or clip")
    for item in plan.broll:
        if item.duration <= 0:
            errors.append(f"b-roll {item.id} has no duration")
        if item.layout not in BROLL_LAYOUTS:
            errors.append(f"b-roll {item.id} layout must be full or pip-lower-right")
    for card in plan.cards:
        if card.duration <= 0:
            errors.append(f"card {card.id} has no duration")
        if card.style not in {"title", "lower-third", "end-card"}:
            errors.append(f"card {card.id} style must be title, lower-third, or end-card")
    for line in plan.narration:
        if line.end <= line.start:
            errors.append(f"narration {line.id} ends at or before it starts")
        if line.id in seen:
            errors.append(f"duplicate narration id {line.id}")
        seen.add(line.id)
    for index, cue in enumerate(plan.captions, start=1):
        if cue.end <= cue.start:
            errors.append(f"caption {index} ends at or before it starts")
    return errors
