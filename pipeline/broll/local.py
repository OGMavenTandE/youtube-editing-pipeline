"""Match local video files against B-roll / graphic queries. No stock APIs."""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.broll.base import BrollAsset, BrollKind, BrollSpec
from pipeline.models import EditScript, Scene
from pipeline.shotlist import local_asset_path

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_TOKEN = re.compile(r"[a-z0-9]+")


def query_tokens(query: str) -> set[str]:
    return {token for token in _TOKEN.findall(query.casefold()) if len(token) > 2}


def filename_tokens(path: Path) -> set[str]:
    stem = path.stem.replace("_", " ").replace("-", " ")
    return query_tokens(stem)


def score_filename(path: Path, tokens: set[str]) -> int:
    if not tokens:
        return 0
    return len(tokens & filename_tokens(path))


def list_local_videos(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )


def match_local_broll(query: str, directory: Path) -> Path | None:
    """Best filename/keyword hit for a query. None if nothing overlaps."""
    tokens = query_tokens(query)
    if not tokens:
        return None
    best: Path | None = None
    best_score = 0
    for path in list_local_videos(directory):
        score = score_filename(path, tokens)
        if score > best_score:
            best = path
            best_score = score
    return best if best_score > 0 else None


def scene_broll_query(scene: Scene, script: EditScript) -> str:
    parts = [scene.graphic.title, *scene.graphic.bullets, scene.graphic.slide_id]
    for cue in script.broll:
        if cue.end > scene.start and cue.start < scene.end and cue.query.strip():
            parts.append(cue.query)
    return " ".join(part.strip() for part in parts if part and part.strip())


class LocalVideoProvider:
    """BrollProvider that resolves a local folder. No network, no API keys."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def render(self, spec: BrollSpec) -> BrollAsset:
        if spec.asset_path is not None and Path(spec.asset_path).is_file():
            path = Path(spec.asset_path)
            return BrollAsset(kind=BrollKind.VIDEO, path=path, duration=spec.duration)
        query = spec.query or spec.title
        matched = match_local_broll(query, self.directory)
        if matched is None:
            raise FileNotFoundError(f"No local B-roll match for {query!r} in {self.directory}")
        return BrollAsset(kind=BrollKind.VIDEO, path=matched, duration=spec.duration)


def apply_local_broll(script: EditScript, directory: Path | None) -> EditScript:
    """Stamp matching local videos onto overlapping cues and scene graphics."""
    if directory is None:
        return script
    folder = Path(directory)
    if not folder.is_dir():
        return script

    for cue in script.broll:
        if cue.asset_path and Path(cue.asset_path).is_file():
            continue
        matched = match_local_broll(cue.query, folder)
        if matched is not None:
            cue.asset_path = str(matched.resolve())

    for scene in script.scenes:
        if scene.asset_kind not in {"broll", "site"}:
            continue
        existing = local_asset_path(scene.asset_ref) or local_asset_path(scene.graphic.asset_path)
        if existing is not None:
            scene.asset_ref = str(existing)
            scene.graphic.asset_path = str(existing)
            continue
        if scene.asset_kind == "site":
            continue
        query = scene.asset_ref or scene.shown or scene_broll_query(scene, script)
        matched = match_local_broll(query, folder)
        if matched is None:
            continue
        scene.asset_ref = str(matched.resolve())
        scene.graphic.asset_path = scene.asset_ref
    return script
