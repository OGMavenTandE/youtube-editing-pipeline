"""Working-cut reuse and optional talking-head keep ranges."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.config import Settings
from pipeline.media import concat_keep_ranges, probe_duration
from pipeline.models import (
    EditScript,
    LowerThird,
    MicroEvent,
    OverlayCallout,
    Scene,
    SilenceCutMap,
    SilenceTrimResult,
    TalkingHeadCut,
    TimeRange,
    BRollCue,
)

def trimmed_path_for(input_path: Path, settings: Settings) -> Path:
    return (settings.work_dir / f"{input_path.stem}_trimmed.mp4").resolve()


def load_cut_map(path: Path) -> SilenceCutMap | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return SilenceCutMap.model_validate(payload)


def resolve_working_cut(
    input_path: Path,
    settings: Settings,
    *,
    skip_silence: bool,
    prefer_existing_trim: bool,
) -> SilenceTrimResult | None:
    """Reuse work/<stem>_trimmed.mp4 when the director already planned on it.

    Returns None when the caller should run silence trim (or passthrough raw).
    """
    trimmed = trimmed_path_for(input_path, settings)
    cut_map_path = settings.output_dir / f"{input_path.stem}_cut_map.json"
    if not (skip_silence or prefer_existing_trim):
        return None
    if not trimmed.is_file():
        return None
    duration = probe_duration(trimmed, settings)
    cut_map = load_cut_map(cut_map_path)
    if cut_map is None:
        cut_map = SilenceCutMap(
            kept_ranges=[TimeRange(start=0.0, end=duration)],
            removed_ranges=[],
            original_duration=duration,
            trimmed_duration=duration,
        )
    else:
        cut_map.trimmed_duration = duration
    return SilenceTrimResult(
        output_path=trimmed,
        cut_map=cut_map,
        backend="reused-trim",
    )


def remap_edit_script(script: EditScript, cut_map: SilenceCutMap) -> EditScript:
    """Rewrite timestamps from the pre-cut timeline onto the extra-cut file."""
    scenes: list[Scene] = []
    for scene in script.scenes:
        window = cut_map.remap_range(scene.start, scene.end)
        if window is None:
            continue
        child = scene.model_copy(deep=True)
        child.start = window.start
        child.end = window.end
        events: list[MicroEvent] = []
        for event in scene.micro_events:
            mapped = cut_map.remap_range(event.start, event.end)
            if mapped is None:
                continue
            item = event.model_copy(deep=True)
            item.start = mapped.start
            item.end = mapped.end
            events.append(item)
        child.micro_events = events
        scenes.append(child)

    def _remap_span(start: float, end: float) -> TimeRange | None:
        return cut_map.remap_range(start, end)

    cuts: list[TalkingHeadCut] = []
    lower: list[LowerThird] = []
    overlays: list[OverlayCallout] = []
    broll: list[BRollCue] = []
    for card in script.lower_thirds:
        mapped = _remap_span(card.start, card.end)
        if mapped is None:
            continue
        item = card.model_copy(deep=True)
        item.start = mapped.start
        item.end = mapped.end
        lower.append(item)
    for card in script.overlays:
        mapped = _remap_span(card.start, card.end)
        if mapped is None:
            continue
        item = card.model_copy(deep=True)
        item.start = mapped.start
        item.end = mapped.end
        overlays.append(item)
    for cue in script.broll:
        mapped = _remap_span(cue.start, cue.end)
        if mapped is None:
            continue
        item = cue.model_copy(deep=True)
        item.start = mapped.start
        item.end = mapped.end
        broll.append(item)

    return script.model_copy(
        update={
            "scenes": scenes,
            "talking_head_cuts": cuts,
            "lower_thirds": lower,
            "overlays": overlays,
            "broll": broll,
        }
    )


def apply_talking_head_cuts(
    video_path: Path,
    script: EditScript,
    settings: Settings,
) -> tuple[Path, EditScript]:
    """Apply extra keep-ranges on the trimmed timeline, then remap the script."""
    cuts = [
        TimeRange(start=cut.start, end=cut.end)
        for cut in script.talking_head_cuts
        if cut.end > cut.start
    ]
    if not cuts:
        return video_path, script
    duration = probe_duration(video_path, settings)
    clipped: list[TimeRange] = []
    for span in cuts:
        start = max(0.0, min(span.start, duration))
        end = max(start, min(span.end, duration))
        if end - start >= 0.04:
            clipped.append(TimeRange(start=start, end=end))
    if not clipped:
        return video_path, script
    dest = (settings.work_dir / f"{video_path.stem}_director_cut.mp4").resolve()
    concat_keep_ranges(
        video_path,
        [(span.start, span.end) for span in clipped],
        dest,
        settings,
    )
    cut_map = SilenceCutMap(
        kept_ranges=clipped,
        removed_ranges=[],
        original_duration=duration,
        trimmed_duration=sum(span.duration for span in clipped),
    )
    remapped = remap_edit_script(script, cut_map)
    return dest, remapped
