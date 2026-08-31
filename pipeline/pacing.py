"""Talk structure: sparse tags + forced open/close bookends. No zoom. No variety loop."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pipeline.config import Settings
from pipeline.layouts import PictureTag
from pipeline.models import (
    EditScript,
    GraphicCard,
    HostIdentity,
    Scene,
    TalkSheet,
)
from pipeline.shotlist import overlay_copy_ok, resolve_edit_script, resolve_talk_sheet


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
    """Loose band. Sparse is correct. A 20-minute cut is not 50–80 layout swaps."""
    del settings
    duration = max(0.0, duration)
    if duration <= 0:
        return 0, 0
    # Bookends plus a handful of overlay/pip cards. Most time is one nothing hold.
    low = 1 if duration < 20 else 3
    high = max(low, min(40, 3 + int(duration / 20)))
    return low, high


def max_hold_at(time: float, settings: Settings) -> float:
    del time
    return settings.layout_hold_hard_ceiling


def target_hold_at(time: float, settings: Settings) -> float:
    del time
    return settings.layout_hold_body_target


def enforce_pacing(script: EditScript, duration: float, settings: Settings) -> EditScript:
    """Cover gaps with nothing, strip invented micro-resets, force bookends."""
    duration = max(duration, 0.0)
    scenes = [scene.model_copy(deep=True) for scene in script.scenes]
    for scene in scenes:
        scene.micro_events = []
        if scene.role == "body":
            scene.layout = PictureTag.coerce(scene.layout, allow_bookend=False)
    scenes = _cover_timeline(scenes, duration, settings)
    script = script.model_copy(update={"scenes": scenes})
    resolve_talk_sheet(script)
    apply_bookends(script, duration, settings)
    resolve_edit_script(script)
    return script


def apply_bookends(
    script: EditScript,
    duration: float,
    settings: Settings,
    *,
    identity: HostIdentity | None = None,
    talk_sheet: TalkSheet | None = None,
) -> EditScript:
    """First and last bookend_seconds: overlay card + identity lower third. Never PiP."""
    duration = max(0.0, duration)
    hold = min(float(settings.bookend_seconds), duration / 2 if duration > 0 else 0.0)
    hold = max(0.0, hold)
    if duration <= 0 or hold < 0.2:
        return script

    if identity is not None:
        script.identity = identity
    sheet = talk_sheet or resolve_talk_sheet(script)

    body = [
        scene
        for scene in script.scenes
        if scene.end > scene.start and scene.role == "body"
    ]
    # Drop any model-emitted bookends; the app owns these windows.
    trimmed: list[Scene] = []
    for scene in body:
        start = max(hold, min(scene.start, duration - hold))
        end = min(duration - hold, max(scene.end, start))
        if end - start < 0.05:
            continue
        child = scene.model_copy(deep=True)
        child.start = start
        child.end = end
        child.role = "body"
        if child.layout is PictureTag.LOWER_THIRD:
            child.layout = PictureTag.NOTHING
        trimmed.append(child)

    open_card = sheet.open_card
    close_card = sheet.close_card
    open_scene = Scene(
        start=0.0,
        end=hold if duration > hold else duration,
        layout=PictureTag.LOWER_THIRD,
        role="open",
        said=open_card.headline,
        shown="open bookend",
        reason="bookend-open",
        graphic=GraphicCard(
            kicker=open_card.kicker,
            title=open_card.headline,
            icon=open_card.icon or "bar_chart",
        ),
    )
    close_start = max(0.0, duration - hold)
    close_scene = Scene(
        start=close_start,
        end=duration,
        layout=PictureTag.LOWER_THIRD,
        role="close",
        said=close_card.headline,
        shown="close bookend",
        reason="bookend-close",
        graphic=GraphicCard(
            kicker=close_card.kicker or "WORK WITH ME",
            title=close_card.headline or "Independent AI T&E.\nVendor-agnostic.",
            icon=close_card.icon or "share",
        ),
    )

    scenes: list[Scene] = [open_scene]
    for scene in trimmed:
        if scene.end <= open_scene.end or scene.start >= close_scene.start:
            continue
        scene.start = max(scene.start, open_scene.end)
        scene.end = min(scene.end, close_scene.start)
        if scene.end - scene.start >= 0.05:
            scenes.append(scene)
    if close_scene.start > open_scene.end - 1e-6 and (
        not scenes or scenes[-1].end < close_scene.end - 0.02
    ):
        if close_start > open_scene.end + 0.05 and (
            not scenes or scenes[-1].end < close_start - 0.05
        ):
            scenes.append(
                Scene(
                    start=scenes[-1].end if len(scenes) > 1 else open_scene.end,
                    end=close_start,
                    layout=PictureTag.NOTHING,
                    role="body",
                    reason="bookend-gap",
                    shown="full-frame host",
                )
            )
        scenes.append(close_scene)
    elif duration <= hold * 2:
        # Short clip: open then close, no body.
        if close_scene.start > open_scene.end:
            scenes.append(close_scene)
        else:
            open_scene.end = duration
            scenes = [open_scene]

    if scenes:
        scenes[0].start = 0.0
        scenes[-1].end = duration
    script.scenes = scenes
    return script


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

    chrome = 0
    for index, scene in enumerate(scenes):
        if scene.layout is PictureTag.LOWER_THIRD and scene.role == "body":
            warnings.append(
                f"Scene {index} is lower_third in the body. Bookends are app-forced only."
            )
        if scene.layout in {PictureTag.OVERLAY, PictureTag.PIP}:
            chrome += 1
        if any(event.kind == "punch_in" for event in scene.micro_events):
            warnings.append(f"Scene {index} still has a punch-in. The kit does not zoom.")

    if scenes and duration >= settings.bookend_seconds * 2:
        if scenes[0].role != "open" or scenes[0].layout is not PictureTag.LOWER_THIRD:
            warnings.append("Open bookend is missing.")
        if scenes[-1].role != "close" or scenes[-1].layout is not PictureTag.LOWER_THIRD:
            warnings.append("Close bookend is missing.")

    if chrome > 12 and duration <= 600:
        warnings.append(f"{chrome} chrome beats. Overlay/PiP should stay sparse.")

    micro_count = sum(len(scene.micro_events) for scene in scenes)
    return PacingReport(
        duration=duration,
        expected_min_scenes=low,
        expected_max_scenes=high,
        scene_count=count,
        micro_event_count=micro_count,
        warnings=warnings,
    )


def graphic_is_real(graphic: GraphicCard) -> bool:
    return overlay_copy_ok(graphic) or bool(
        graphic.asset_path.strip() or graphic.still_query.strip() or graphic.quote.strip()
    )


def _cover_timeline(
    scenes: list[Scene], duration: float, settings: Settings
) -> list[Scene]:
    del settings
    if duration <= 0:
        return []
    ordered = sorted(
        [scene for scene in scenes if scene.end > scene.start],
        key=lambda scene: scene.start,
    )
    if not ordered:
        return [
            Scene(
                start=0.0,
                end=duration,
                layout=PictureTag.NOTHING,
                role="body",
                reason="pacing-fill",
                shown="full-frame host",
                asset_kind="none",
            )
        ]

    filled: list[Scene] = []
    cursor = 0.0
    for scene in ordered:
        start = min(max(scene.start, 0.0), duration)
        end = min(max(scene.end, start), duration)
        if start > cursor + 0.05:
            filled.append(
                Scene(
                    start=cursor,
                    end=start,
                    layout=PictureTag.NOTHING,
                    role="body",
                    reason="pacing-fill",
                    shown="full-frame host",
                    asset_kind="none",
                )
            )
        scene.start = min(start, cursor) if filled and start < cursor else start
        scene.end = end
        if scene.end > scene.start:
            filled.append(scene)
            cursor = max(cursor, scene.end)
    if cursor < duration - 0.05:
        filled.append(
            Scene(
                start=cursor,
                end=duration,
                layout=PictureTag.NOTHING,
                role="body",
                reason="pacing-fill",
                shown="full-frame host",
                asset_kind="none",
            )
        )
    if filled:
        filled[-1].end = duration
    return filled


def _count_at(span: float, hold: float) -> int:
    if span <= 0 or hold <= 0:
        return 0
    return max(1, math.ceil(span / hold - 1e-9))
