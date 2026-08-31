"""DVIDS named-platform stills for PiP. Banana is a stub that fills the image slot only."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pipeline.broll.local import query_tokens
from pipeline.models import EditScript, Scene
from pipeline.shotlist import local_asset_path

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
_TOKEN = re.compile(r"[a-z0-9]+")

BANANA_PROMPT = """\
Photoreal 16:9 PAO documentary still of {hardware}.
Named US/allied hardware or a real named platform only.
No text, no HUD, no captions, no logos drawn on the frame.
No generated host, no faces, no sci-fi, no drones that do not exist.
Leave the left third and the lower-right quieter so type and a PiP window can sit there.
Natural light, field or hangar, Department of War public-affairs look.
"""


def banana_prompt(hardware: str) -> str:
    return BANANA_PROMPT.format(hardware=hardware.strip() or "named military platform")


def list_local_stills(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def match_local_still(query: str, directory: Path) -> Path | None:
    """Best filename/keyword hit for a named-platform query. Images only."""
    tokens = query_tokens(query)
    if not tokens:
        return None
    best: Path | None = None
    best_score = 0
    for path in list_local_stills(directory):
        stem = path.stem.replace("_", " ").replace("-", " ").casefold()
        score = len(tokens & set(_TOKEN.findall(stem)))
        if score > best_score:
            best = path
            best_score = score
    return best if best_score > 0 else None


def generate_banana_still(query: str, dest: Path) -> Path | None:
    """Stub. Prefer skip PiP over generating junk. Image slot only, never chrome or host."""
    logger.info("Banana still stub skipped for %r (dest would be %s)", query, dest)
    del dest
    return None


def apply_stills(script: EditScript, directory: Path | None) -> EditScript:
    """Stamp a DVIDS still onto pip beats. No still means the contract will drop PiP."""
    folder = Path(directory) if directory is not None else None
    for scene in script.scenes:
        _resolve_scene_still(scene, folder)
    return script


def _resolve_scene_still(scene: Scene, folder: Path | None) -> None:
    existing = local_asset_path(scene.asset_ref) or local_asset_path(scene.graphic.asset_path)
    if existing is not None and existing.suffix.lower() in IMAGE_SUFFIXES:
        scene.asset_ref = str(existing)
        scene.graphic.asset_path = str(existing)
        return
    query = scene.graphic.still_query.strip() or scene.shown.strip() or scene.graphic.title.strip()
    if folder is not None and query:
        matched = match_local_still(query, folder)
        if matched is not None:
            scene.asset_ref = str(matched.resolve())
            scene.graphic.asset_path = scene.asset_ref
            return
    if query and folder is not None:
        dest = folder / f"banana_{_safe(query)}.png"
        generated = generate_banana_still(query, dest)
        if generated is not None:
            scene.asset_ref = str(generated)
            scene.graphic.asset_path = str(generated)


def _safe(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned.strip("_")[:40] or "still"
