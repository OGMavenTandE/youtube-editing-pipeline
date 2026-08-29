from __future__ import annotations

import math
from dataclasses import dataclass, field

from pipeline.config import Settings
from pipeline.layouts import LayoutKind
from pipeline.models import EditScript, GraphicCard, MicroEvent, MicroEventKind, Scene

# Non-repeating cycle. Not A-B-C-A-B-C.
_LAYOUT_CYCLE = (
    LayoutKind.FULL_FRAME,
    LayoutKind.PIP_BOTTOM_RIGHT,
    LayoutKind.SPLIT_TOP,
    LayoutKind.PIP_BOTTOM_RIGHT,
    LayoutKind.FULL_FRAME,
    LayoutKind.SPLIT_TOP,
    LayoutKind.PIP_BOTTOM_RIGHT,
    LayoutKind.FULL_FRAME,
)


@dataclass
class PacingReport:
    duration: float
    expected_min_scenes: int
    expected_max_scenes: int
    scene_count: int
    micro_event_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def in_band(self) -> bool:
        return self.expected_min_scenes <= self.scene_count <= self.expected_max_scenes


def expected_scene_range(duration: float, settings: Settings) -> tuple[int, int]:
    """Scene count band for a trimmed cut.

    First 60s uses tighter holds (8-15s). The rest uses 15-25s holds.
    A 20-minute cut lands near 50-80 scenes.
    """
    duration = max(0.0, duration)
    first = min(duration, settings.pacing_hook_window)
    rest = max(0.0, duration - settings.pacing_hook_window)
    low = _count_at(first, settings.layout_hold_hook_max) + _count_at(
        rest, settings.layout_hold_body_max
    )
    high = _count_at(first, settings.layout_hold_min) + _count_at(
        rest, settings.layout_hold_body_min
    )
    low = max(1 if duration > 0 else 0, low)
    high = max(low, high)
    return low, high


def max_hold_at(time: float, settings: Settings) -> float:
    if time < settings.pacing_hook_window:
        cap = settings.layout_hold_hook_max
    else:
        cap = settings.layout_hold_body_max
    return min(cap, settings.layout_hold_hard_ceiling)


def target_hold_at(time: float, settings: Settings) -> float:
    if time < settings.pacing_hook_window:
        return settings.layout_hold_hook_target
    return settings.layout_hold_body_target


def enforce_pacing(script: EditScript, duration: float, settings: Settings) -> EditScript:
    """Guarantee a dense scene list and micro-resets on the trimmed timeline."""
    duration = max(duration, 0.0)
    scenes = [scene.model_copy(deep=True) for scene in script.scenes]
    scenes = _cover_timeline(scenes, duration, settings)
    scenes = _split_long_holds(scenes, settings)
    scenes = _avoid_triple_layouts(scenes)
    scenes = _ensure_micro_events(scenes, settings)
    return script.model_copy(update={"scenes": scenes})


def evaluate_pacing(script: EditScript, duration: float, settings: Settings) -> PacingReport:
    low, high = expected_scene_range(duration, settings)
    warnings: list[str] = []
    scenes = script.scenes
    if not scenes:
        warnings.append("No scenes on the timeline.")
    count = len(scenes)
    if count < low:
        warnings.append(f"Scene count {count} is below the {low}-{high} band for {duration:.0f}s.")
    if count > high:
        warnings.append(f"Scene count {count} is above the {low}-{high} band for {duration:.0f}s.")

    streak = 1
    for index, scene in enumerate(scenes):
        hold = scene.duration
        cap = max_hold_at(scene.start, settings)
        if hold + 1e-6 < settings.layout_hold_min and hold < duration:
            warnings.append(
                f"Scene {index} hold {hold:.1f}s is under the {settings.layout_hold_min:.0f}s floor."
            )
        if hold > cap + 0.05:
            warnings.append(
                f"Scene {index} hold {hold:.1f}s exceeds the {cap:.0f}s ceiling at {scene.start:.1f}s."
            )
        if index > 0 and scene.layout == scenes[index - 1].layout:
            streak += 1
            if streak >= settings.max_same_layout_streak:
                warnings.append(
                    f"Layout {scene.layout.value} repeats {streak} times starting near {scene.start:.1f}s."
                )
        else:
            streak = 1

    micro_count = sum(len(scene.micro_events) for scene in scenes)
    return PacingReport(
        duration=duration,
        expected_min_scenes=low,
        expected_max_scenes=high,
        scene_count=count,
        micro_event_count=micro_count,
        warnings=warnings,
    )


def _cover_timeline(
    scenes: list[Scene], duration: float, settings: Settings
) -> list[Scene]:
    if duration <= 0:
        return []
    ordered = sorted(
        [scene for scene in scenes if scene.end > scene.start],
        key=lambda scene: scene.start,
    )
    if not ordered:
        return _synthesize_scenes(0.0, duration, settings, start_index=0)

    filled: list[Scene] = []
    cursor = 0.0
    for scene in ordered:
        start = min(max(scene.start, 0.0), duration)
        end = min(max(scene.end, start), duration)
        if start > cursor + 0.05:
            filled.extend(
                _synthesize_scenes(
                    cursor,
                    start,
                    settings,
                    start_index=len(filled),
                    inherit=_nearest_real_graphic(ordered, filled, cursor),
                )
            )
        scene.start = min(start, cursor) if filled and start < cursor else start
        scene.end = end
        if scene.end > scene.start:
            filled.append(scene)
            cursor = max(cursor, scene.end)
    if cursor < duration - 0.05:
        filled.extend(
            _synthesize_scenes(
                cursor,
                duration,
                settings,
                start_index=len(filled),
                inherit=_nearest_real_graphic(ordered, filled, cursor),
            )
        )
    if filled:
        filled[-1].end = duration
    return filled


def graphic_is_real(graphic: GraphicCard) -> bool:
    """True when a card has copy or a rendered/matched asset. Empty fills do not."""
    return bool(
        graphic.title.strip()
        or graphic.bullets
        or graphic.asset_path.strip()
        or graphic.slide_id.strip()
        or graphic.lower_third_title.strip()
    )


def _nearest_real_graphic(
    planned: list[Scene], filled: list[Scene], cursor: float
) -> GraphicCard | None:
    for scene in reversed(filled):
        if graphic_is_real(scene.graphic):
            return scene.graphic
    previous = [scene for scene in planned if scene.end <= cursor + 0.05]
    for scene in reversed(previous):
        if graphic_is_real(scene.graphic):
            return scene.graphic
    upcoming = [scene for scene in planned if scene.start >= cursor - 0.05]
    for scene in upcoming:
        if graphic_is_real(scene.graphic):
            return scene.graphic
    return None


def _fill_graphic(inherit: GraphicCard | None) -> GraphicCard:
    if inherit is not None and graphic_is_real(inherit):
        return inherit.model_copy(deep=True)
    return GraphicCard()


def _synthesize_scenes(
    start: float,
    end: float,
    settings: Settings,
    *,
    start_index: int,
    inherit: GraphicCard | None = None,
) -> list[Scene]:
    """Fill a gap. Stay FULL_FRAME. Never invent empty PIP/SPLIT cards."""
    del start_index
    scenes: list[Scene] = []
    cursor = start
    graphic = _fill_graphic(inherit)
    while cursor < end - 0.01:
        hold = min(target_hold_at(cursor, settings), end - cursor)
        if end - (cursor + hold) < settings.layout_hold_min:
            hold = end - cursor
        hold = max(hold, min(settings.layout_hold_min, end - cursor))
        scenes.append(
            Scene(
                start=cursor,
                end=cursor + hold,
                layout=LayoutKind.FULL_FRAME,
                reason="pacing-fill",
                graphic=graphic.model_copy(deep=True),
            )
        )
        cursor += hold
    return scenes


def _split_long_holds(scenes: list[Scene], settings: Settings) -> list[Scene]:
    split: list[Scene] = []
    for scene in scenes:
        cap = max_hold_at(scene.start, settings)
        if scene.duration <= cap + 0.05:
            split.append(scene)
            continue
        cursor = scene.start
        part = 0
        while cursor < scene.end - 0.01:
            hold = min(target_hold_at(cursor, settings), scene.end - cursor)
            if scene.end - (cursor + hold) < settings.layout_hold_min:
                hold = scene.end - cursor
            if part == 0:
                layout = scene.layout
            elif graphic_is_real(scene.graphic):
                layout = _next_layout(split[-1].layout if split else scene.layout)
            else:
                layout = LayoutKind.FULL_FRAME
            child = scene.model_copy(deep=True)
            child.start = cursor
            child.end = cursor + hold
            child.layout = layout
            if part:
                child.reason = (child.reason + " split").strip()
            split.append(child)
            cursor += hold
            part += 1
    return split


def _avoid_triple_layouts(scenes: list[Scene]) -> list[Scene]:
    streak = 1
    for index in range(1, len(scenes)):
        if scenes[index].layout == scenes[index - 1].layout:
            streak += 1
            if streak >= 3:
                if graphic_is_real(scenes[index].graphic):
                    scenes[index].layout = _next_layout(scenes[index].layout)
                    streak = 1
        else:
            streak = 1
    return scenes


def _ensure_micro_events(scenes: list[Scene], settings: Settings) -> list[Scene]:
    interval = settings.micro_reset_target
    punch = settings.punch_in_duration
    scale = settings.punch_in_scale
    for scene in scenes:
        events = [
            event
            for event in scene.micro_events
            if scene.start - 0.05 <= event.start < scene.end + 0.05
        ]
        cursor = scene.start + min(interval, max(scene.duration / 2, 1.0))
        kind_toggle = 0
        while cursor < scene.end - 0.4:
            if not any(abs(event.start - cursor) < interval * 0.6 for event in events):
                if kind_toggle % 2 == 0:
                    events.append(
                        MicroEvent(
                            start=cursor,
                            end=min(cursor + punch, scene.end),
                            kind=MicroEventKind.PUNCH_IN,
                            scale=scale,
                        )
                    )
                else:
                    text = _bullet_or_fallback(scene, kind_toggle)
                    events.append(
                        MicroEvent(
                            start=cursor,
                            end=min(cursor + settings.text_hold, scene.end),
                            kind=MicroEventKind.TEXT,
                            text=text,
                        )
                    )
                kind_toggle += 1
            cursor += interval
        events.append(
            MicroEvent(
                start=max(scene.start, scene.end - 0.05),
                end=scene.end,
                kind=MicroEventKind.CUT,
            )
        )
        events.sort(key=lambda event: event.start)
        scene.micro_events = events
    return scenes


def _bullet_or_fallback(scene: Scene, index: int) -> str:
    if scene.graphic.bullets:
        return scene.graphic.bullets[index % len(scene.graphic.bullets)]
    if scene.graphic.title:
        return scene.graphic.title
    return "Stay with this beat"


def _layout_for_index(index: int, previous: LayoutKind | None) -> LayoutKind:
    layout = _LAYOUT_CYCLE[index % len(_LAYOUT_CYCLE)]
    if previous is not None and layout == previous:
        return _next_layout(previous)
    return layout


def _next_layout(current: LayoutKind) -> LayoutKind:
    order = (
        LayoutKind.FULL_FRAME,
        LayoutKind.PIP_BOTTOM_RIGHT,
        LayoutKind.SPLIT_TOP,
    )
    return order[(order.index(current) + 1) % len(order)]


def _count_at(span: float, hold: float) -> int:
    if span <= 0 or hold <= 0:
        return 0
    return max(1, math.ceil(span / hold - 1e-9))
