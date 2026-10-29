"""Per-episode folder layout from the Booth handoff."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

# source/(vo, raw), audio/(voice, mix, score), frames, edit, renders,
# shorts, thumbs, text, qc. Replaces the flat versioned-suffix layout.
EPISODE_FOLDERS: tuple[str, ...] = (
    "source/vo",
    "source/raw",
    "audio/voice",
    "audio/mix",
    "audio/score",
    "frames",
    "edit",
    "renders",
    "shorts",
    "thumbs",
    "text",
    "qc",
)

LAYOUT_VERSION = 1


def episode_manifest(series: str) -> dict[str, object]:
    return {
        "series": series,
        "layout_version": LAYOUT_VERSION,
        "created": date.today().isoformat(),
        "folders": list(EPISODE_FOLDERS),
    }


def init_episode(episode_dir: Path, series: str = "ai-survival", *, force: bool = False) -> Path:
    """Create the standard folders. Existing files are left in place.

    `episode.yaml` is written once. Pass `force` to rewrite that manifest.
    """
    episode_dir.mkdir(parents=True, exist_ok=True)
    for folder in EPISODE_FOLDERS:
        (episode_dir / folder).mkdir(parents=True, exist_ok=True)
    manifest = episode_dir / "episode.yaml"
    if force or not manifest.exists():
        with manifest.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(
                episode_manifest(series),
                handle,
                sort_keys=False,
                allow_unicode=True,
            )
    return manifest
