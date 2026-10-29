"""Turn Ep01's timeline, concat list, and score sheet into an edit plan.

The timeline is the narration cut. `lines.json` is the script before that
cut, so a line present only there (Ep01 dropped `00d_b1`) stays out of the
plan and is named in `meta.script_ids_not_in_timeline`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ytp.plan import (
    Beat,
    CaptionCue,
    EditPlan,
    MusicCue,
    NarrationLine,
    PaletteSpan,
    Segment,
    Short,
)

_FILE_RE = re.compile(r"""^file\s+(?:'(?P<sq>.*)'|"(?P<dq>.*)")\s*$""")
_DUR_RE = re.compile(r"^duration\s+(?P<dur>[0-9]+(?:\.[0-9]+)?)\s*$")
_CUE_RE = re.compile(
    r"^\|\s*(?P<time>\d+:\d{2}(?:\.\d+)?)\s*"
    r"\|\s*(?P<sfx>.*?)\s*"
    r"\|\s*(?P<db>-?\d+(?:\.\d+)?)\s*"
    r"\|\s*(?P<note>.*?)\s*\|\s*$"
)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def parse_cue_time(value: str) -> float:
    """`M:SS.ss` or `H:MM:SS.ss` as used on the score sheet."""
    parts = value.strip().split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError(f"not a cue time: {value!r}")


def parse_concat(text: str) -> list[tuple[str, float | None]]:
    """ffmpeg concat-demuxer lines. A repeated last file with no duration is dropped.

    Image concat lists repeat the final file so the last duration is honored.
    That repeat is not a second shot.
    """
    entries: list[tuple[str, float | None]] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        file_match = _FILE_RE.match(line)
        if file_match:
            if pending is not None:
                entries.append((pending, None))
            quoted = file_match.group("sq")
            pending = quoted if quoted is not None else file_match.group("dq")
            continue
        dur_match = _DUR_RE.match(line)
        if dur_match and pending is not None:
            entries.append((pending, float(dur_match.group("dur"))))
            pending = None
    if pending is not None:
        entries.append((pending, None))
    if len(entries) >= 2 and entries[-1][1] is None and entries[-1][0] == entries[-2][0]:
        entries.pop()
    return entries


def parse_score_cues(text: str) -> list[dict[str, object]]:
    cues = []
    for raw in text.splitlines():
        match = _CUE_RE.match(raw.strip())
        if not match:
            continue
        cues.append(
            {
                "at": parse_cue_time(match.group("time")),
                "sfx": match.group("sfx").strip(),
                "gain_db": float(match.group("db")),
                "note": match.group("note").strip(),
            }
        )
    return cues


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug[:48] or "cue"


def _segment_id(source: str, index: int) -> str:
    stem = Path(source).stem
    return stem or f"segment-{index:03d}"


def import_episode(
    *,
    timeline_path: Path,
    concat_path: Path,
    score_path: Path,
    lines_path: Path | None = None,
    series: str = "ai-survival",
    episode: str = "ep01",
    title: str = "How to Survive an AI Drone",
) -> EditPlan:
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    concat_entries = parse_concat(concat_path.read_text(encoding="utf-8"))
    score_cues = parse_score_cues(score_path.read_text(encoding="utf-8"))

    narration: list[NarrationLine] = []
    for clip in timeline.get("clips") or []:
        narration.append(
            NarrationLine(
                id=str(clip["id"]),
                text=str(clip.get("text") or ""),
                start=float(clip["start"]),
                end=float(clip["end"]),
                beat=int(clip["beat"]) if clip.get("beat") is not None else None,
                palette=str(clip["palette"]) if clip.get("palette") else None,
            )
        )
    kept_ids = {line.id for line in narration}
    removed: list[str] = []
    if lines_path is not None:
        lines = json.loads(lines_path.read_text(encoding="utf-8"))
        for line in lines:
            line_id = str(line.get("id") or "")
            if line_id and line_id not in kept_ids:
                removed.append(line_id)

    segments: list[Segment] = []
    cursor = 0.0
    for index, (source, duration) in enumerate(concat_entries, start=1):
        if duration is None:
            raise ValueError(f"concat entry has no duration: {source}")
        suffix = Path(source).suffix.lower()
        segments.append(
            Segment(
                id=_segment_id(source, index),
                kind="still" if suffix in _IMAGE_SUFFIXES else "clip",
                source=source,
                start=cursor,
                duration=duration,
            )
        )
        cursor += duration

    music: list[MusicCue] = []
    used_ids: set[str] = set()
    for index, cue in enumerate(score_cues, start=1):
        base = _slug(str(cue["sfx"]))
        cue_id = base
        if cue_id in used_ids:
            cue_id = f"{base}-{index:03d}"
        used_ids.add(cue_id)
        music.append(
            MusicCue(
                id=cue_id,
                sfx=str(cue["sfx"]),
                at=float(cue["at"]),
                gain_db=float(cue["gain_db"]),
                note=str(cue["note"]),
                duck_under_voice=True,
            )
        )

    shorts = []
    for key, item in (timeline.get("shorts") or {}).items():
        shorts.append(
            Short(
                id=str(key),
                title=str(item.get("title") or ""),
                start=float(item["start"]),
                end=float(item["end"]),
            )
        )

    beat_starts = timeline.get("beat_starts") or {}
    beats = [
        Beat(id=str(key), title=str(title_text), start=_maybe_float(beat_starts.get(str(key))))
        for key, title_text in (timeline.get("beat_titles") or {}).items()
    ]
    palettes = [
        PaletteSpan(name=str(item["palette"]), start=float(item["start"]), end=float(item["end"]))
        for item in timeline.get("palette_segments") or []
    ]
    captions = [CaptionCue(start=line.start, end=line.end, text=line.text) for line in narration]

    return EditPlan(
        series=series,
        episode=episode,
        title=title,
        lead_in=float(timeline.get("lead_in") or 0),
        voice=str(timeline.get("file") or "") or None,
        segments=segments,
        music=music,
        captions=captions,
        narration=narration,
        shorts=shorts,
        beats=beats,
        palettes=palettes,
        meta={
            "imported_from": {
                "timeline": timeline_path.name,
                "concat": concat_path.name,
                "score": score_path.name,
                "lines": lines_path.name if lines_path else None,
            },
            "timeline_total": timeline.get("total"),
            "tempo": timeline.get("tempo"),
            "script_ids_not_in_timeline": removed,
        },
    )


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
