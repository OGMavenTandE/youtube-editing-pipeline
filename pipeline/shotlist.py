"""Shot-list production contract: Nate talking-head-first.

v1 bible: full-frame host. Additive card only when asset_kind is card.
Local / DVIDS b-roll is a full-frame cutaway (covers the face, host audio
continues) when the file exists. site with no local file is none, not a
fake browser. This compositor does not draw Jack / Screen Studio chrome.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pipeline.layouts import LayoutKind
from pipeline.models import (
    ASSET_KINDS,
    AssetKind,
    EditScript,
    GraphicCard,
    PlannedScene,
    Scene,
)

# Title-only cards are the quality failure. A Nate-style card needs all three.
_MIN_CARD_FACTS = 2


def card_is_dense(graphic: GraphicCard) -> bool:
    """True when a card has kicker + headline + at least two facts."""
    kicker = graphic.kicker.strip()
    headline = graphic.title.strip()
    facts = [item.strip() for item in graphic.bullets if item and item.strip()]
    return bool(kicker and headline and len(facts) >= _MIN_CARD_FACTS)


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


def resolved_media_path(scene: Scene) -> Path | None:
    """Local file the compositor may overlay, or None."""
    return local_asset_path(scene.asset_ref) or local_asset_path(scene.graphic.asset_path)


def scene_has_visual(scene: Scene | PlannedScene) -> bool:
    """True when this scene is allowed to show something other than talking-head."""
    kind: AssetKind = scene.asset_kind
    if kind == "none" or kind not in ASSET_KINDS:
        return False
    if kind == "card":
        return card_is_dense(scene.graphic)
    path = local_asset_path(scene.asset_ref) or local_asset_path(scene.graphic.asset_path)
    return path is not None


ComposeMode = Literal["talking_head", "card", "cutaway"]


def compose_mode(scene: Scene) -> ComposeMode:
    """How the compositor should treat this scene after the contract.

    talking_head: full-frame host, no slide.
    card: additive HTML card on the existing PIP/SPLIT layouts.
    cutaway: full-frame b-roll/site file covering the face. Host audio stays.
    """
    if scene.asset_kind == "card" and card_is_dense(scene.graphic):
        return "card"
    if scene.asset_kind in {"broll", "site"}:
        if resolved_media_path(scene) is not None:
            return "cutaway"
    return "talking_head"


def talking_head_scene(scene: Scene) -> Scene:
    """Force commentary-only: full-frame webcam, no slide, no invented graphic path."""
    scene.asset_kind = "none"
    scene.asset_ref = None
    scene.layout = LayoutKind.FULL_FRAME
    scene.graphic.asset_path = ""
    return scene


def resolve_scene(scene: Scene) -> Scene:
    """Apply the production contract to one scene.

    card: keep only when kicker + headline + facts are present.
    broll: keep only when a local file exists, then force FULL_FRAME cutaway.
    site: local file is a cutaway. No file (URL only) is none, not a browser.
    none: talking-head, no overlay.
    """
    kind: AssetKind = scene.asset_kind if scene.asset_kind in ASSET_KINDS else "none"
    scene.asset_kind = kind
    if scene.asset_ref is not None and not str(scene.asset_ref).strip():
        scene.asset_ref = None

    if kind == "none":
        return talking_head_scene(scene)

    if kind in {"broll", "site"}:
        path = resolved_media_path(scene)
        if path is None:
            return talking_head_scene(scene)
        scene.asset_ref = str(path)
        scene.graphic.asset_path = str(path)
        scene.layout = LayoutKind.FULL_FRAME
        return scene

    if not card_is_dense(scene.graphic):
        return talking_head_scene(scene)
    return scene


def resolve_edit_script(script: EditScript) -> EditScript:
    """Normalize every scene. Safe to call more than once."""
    for scene in script.scenes:
        resolve_scene(scene)
    return script


def scene_shows_slide(scene: Scene) -> bool:
    """HTML slide jobs are only for dense cards. broll/site use a local file."""
    return scene.asset_kind == "card" and card_is_dense(scene.graphic)
