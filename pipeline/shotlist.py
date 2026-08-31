"""Picture-kit production contract.

The model tags body beats overlay | pip | nothing and fills template copy.
The app forces open/close bookends from the talk sheet + identity config.
No layout invention, no zoom, no Chromium slides, no generated host.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pipeline.layouts import PictureTag
from pipeline.models import (
    ASSET_KINDS,
    AssetKind,
    EditScript,
    GraphicCard,
    HostIdentity,
    PlannedScene,
    Scene,
    TalkSheet,
    TaggedBeat,
    TaggedBeatList,
)

ComposeMode = Literal["nothing", "overlay", "pip", "bookend"]


def overlay_copy_ok(graphic: GraphicCard) -> bool:
    return bool(graphic.kicker.strip() and graphic.title.strip())


def local_asset_path(ref: str | None) -> Path | None:
    """Return an existing local file for a path-like ref. URLs and misses are None."""
    if ref is None:
        return None
    raw = str(ref).strip()
    if not raw:
        return None
    if "://" in raw and not raw.startswith("file:"):
        return None
    path = Path(raw)
    if path.is_file() and path.stat().st_size > 0:
        return path.resolve()
    return None


def resolved_still_path(scene: Scene) -> Path | None:
    """Image (or leftover video) the PiP slot may use. Missing means no PiP."""
    return local_asset_path(scene.asset_ref) or local_asset_path(scene.graphic.asset_path)


def resolved_media_path(scene: Scene) -> Path | None:
    return resolved_still_path(scene)


def scene_has_visual(scene: Scene | PlannedScene) -> bool:
    if getattr(scene, "role", "body") in {"open", "close"}:
        return True
    tag = PictureTag.coerce(scene.layout, allow_bookend=True)
    if tag is PictureTag.OVERLAY:
        return overlay_copy_ok(scene.graphic)
    if tag is PictureTag.PIP:
        return resolved_still_path(scene) is not None if isinstance(scene, Scene) else bool(
            scene.graphic.still_query.strip() or scene.asset_ref
        )
    if tag is PictureTag.LOWER_THIRD:
        return True
    return False


def card_is_dense(graphic: GraphicCard) -> bool:
    """Overlay copy is kicker + headline. Bullets are not required."""
    return overlay_copy_ok(graphic)


def compose_mode(scene: Scene) -> ComposeMode:
    if scene.role in {"open", "close"} or scene.layout is PictureTag.LOWER_THIRD:
        return "bookend"
    if scene.layout is PictureTag.PIP and resolved_still_path(scene) is not None:
        return "pip"
    if scene.layout is PictureTag.OVERLAY and overlay_copy_ok(scene.graphic):
        return "overlay"
    return "nothing"


def talking_head_scene(scene: Scene) -> Scene:
    scene.asset_kind = "none"
    scene.asset_ref = None
    scene.layout = PictureTag.NOTHING
    scene.role = "body"
    scene.graphic.asset_path = ""
    return scene


def resolve_scene(scene: Scene) -> Scene:
    """Apply the kit contract to one beat."""
    kind: AssetKind = scene.asset_kind if scene.asset_kind in ASSET_KINDS else "none"
    scene.asset_kind = kind
    if scene.asset_ref is not None and not str(scene.asset_ref).strip():
        scene.asset_ref = None

    if scene.role in {"open", "close"}:
        scene.layout = PictureTag.LOWER_THIRD
        return scene

    scene.layout = PictureTag.coerce(scene.layout, allow_bookend=False)

    if scene.layout is PictureTag.PIP:
        path = resolved_still_path(scene)
        if path is None:
            if overlay_copy_ok(scene.graphic):
                from pipeline.talk_sheet import derive_kicker

                scene.layout = PictureTag.OVERLAY
                scene.asset_kind = "none"
                scene.graphic.kicker = derive_kicker(scene.graphic.title, said=scene.said)
                return scene
            return talking_head_scene(scene)
        scene.asset_ref = str(path)
        scene.graphic.asset_path = str(path)
        return scene

    if scene.layout is PictureTag.OVERLAY:
        if not overlay_copy_ok(scene.graphic):
            return talking_head_scene(scene)
        return scene

    return talking_head_scene(scene)


def resolve_edit_script(script: EditScript) -> EditScript:
    for scene in script.scenes:
        resolve_scene(scene)
    return script


def scene_shows_slide(scene: Scene) -> bool:
    """Chromium slides are retired. The kit paints chrome in process."""
    del scene
    return False


def resolve_talk_sheet(script: EditScript) -> TalkSheet:
    """Fill bookend cards from job metadata. Never borrow a body overlay."""
    sheet = script.talk_sheet.model_copy(deep=True)
    if not sheet.open_card.kicker.strip():
        titles = [title.strip() for title in script.metadata.titles if title.strip()]
        if titles:
            index = max(0, min(int(script.metadata.title_index or 0), len(titles) - 1))
            sheet.open_card.kicker = titles[index]
            sheet.title = sheet.title.strip() or titles[index]
    if not sheet.open_card.icon.strip():
        sheet.open_card.icon = sheet.open_icon.strip() or "bar_chart"
    if not sheet.close_card.kicker.strip():
        sheet.close_card.kicker = "WORK WITH ME"
    if not sheet.close_card.headline.strip():
        sheet.close_card.headline = "Independent AI T&E.\nVendor-agnostic."
    if not sheet.close_card.icon.strip():
        sheet.close_card.icon = sheet.close_icon.strip() or "share"
    sheet.close_kicker = sheet.close_card.kicker
    sheet.close_headline = sheet.close_card.headline
    sheet.close_icon = sheet.close_card.icon
    script.talk_sheet = sheet
    return sheet


def resolve_identity(script: EditScript, identity: HostIdentity | None = None) -> HostIdentity:
    if identity is not None:
        script.identity = identity
    return script.identity


def beats_from_script(script: EditScript, duration: float) -> TaggedBeatList:
    beats: list[TaggedBeat] = []
    for scene in script.scenes:
        beats.append(
            TaggedBeat(
                start=scene.start,
                end=scene.end,
                tag=scene.layout,
                role=scene.role,
                said=scene.said,
                kicker=scene.graphic.kicker,
                headline=scene.graphic.title,
                icon=scene.graphic.icon,
                quote=scene.graphic.quote,
                still_query=scene.graphic.still_query,
                still_path=scene.graphic.asset_path or (scene.asset_ref or ""),
                reason=scene.reason,
            )
        )
    return TaggedBeatList(
        duration=duration,
        identity=script.identity,
        talk_sheet=script.talk_sheet,
        beats=beats,
    )


def apply_tagged_beats(script: EditScript, payload: TaggedBeatList) -> EditScript:
    scenes: list[Scene] = []
    for beat in payload.beats:
        scenes.append(
            Scene(
                start=beat.start,
                end=beat.end,
                layout=beat.tag,
                role=beat.role,
                said=beat.said,
                reason=beat.reason,
                graphic=GraphicCard(
                    kicker=beat.kicker,
                    title=beat.headline,
                    icon=beat.icon,
                    quote=beat.quote,
                    still_query=beat.still_query,
                    asset_path=beat.still_path,
                ),
            )
        )
    script.scenes = scenes
    script.identity = payload.identity
    script.talk_sheet = payload.talk_sheet
    return resolve_edit_script(script)


def save_tagged_beats(payload: TaggedBeatList, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_tagged_beats(path: Path) -> TaggedBeatList:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TaggedBeatList.model_validate(raw)
