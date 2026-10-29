"""Clip index: one row per source clip, with word timestamps when Whisper is installed."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ytp.ff import probe_duration
from ytp.transcribe import Transcriber

MEDIA_SUFFIXES = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
}

INDEX_JSON = Path("edit") / "clip-index.json"
INDEX_MD = Path("edit") / "clip-index.md"


def _series_of(episode_dir: Path) -> str:
    manifest = episode_dir / "episode.yaml"
    if not manifest.exists():
        return ""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    return str(data.get("series") or "")


def _existing_descriptions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for clip in data.get("clips") or []:
        found[str(clip.get("path") or "")] = str(clip.get("description") or "")
    return found


def iter_clips(episode_dir: Path, roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        scan = root if root.is_absolute() else episode_dir / root
        if not scan.exists():
            continue
        for path in scan.rglob("*"):
            if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
                found.append(path)
    return sorted(found)


def _clip_id(relative: str, used: set[str]) -> str:
    stem = Path(relative).stem
    candidate = stem
    if candidate in used:
        candidate = relative.replace("/", "-").rsplit(".", 1)[0]
    used.add(candidate)
    return candidate


def index_episode(
    episode_dir: Path,
    transcriber: Transcriber,
    *,
    roots: list[Path] | None = None,
) -> dict[str, object]:
    """Transcribe every clip under ``source/`` (or ``roots``) and write the index.

    A description already stored for that path is kept. New clips get an empty
    description for Booth to fill.
    """
    episode_dir = episode_dir.resolve()
    scan_roots = roots or [Path("source")]
    previous = _existing_descriptions(episode_dir / INDEX_JSON)
    used_ids: set[str] = set()
    clips: list[dict[str, object]] = []
    for path in iter_clips(episode_dir, scan_roots):
        relative = path.resolve().relative_to(episode_dir).as_posix()
        transcript = transcriber.transcribe(path)
        clips.append(
            {
                "id": _clip_id(relative, used_ids),
                "path": relative,
                "duration": round(probe_duration(path), 3),
                "description": previous.get(relative, ""),
                "transcript": transcript.text,
                "language": transcript.language,
                "timestamps": [
                    {
                        "word": word.text,
                        "start": round(word.start, 3),
                        "end": round(word.end, 3),
                    }
                    for word in transcript.words
                ],
            }
        )
    document: dict[str, object] = {
        "series": _series_of(episode_dir),
        "episode_dir": episode_dir.as_posix(),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "backend": transcriber.name,
        "clips": clips,
    }
    json_path = episode_dir / INDEX_JSON
    md_path = episode_dir / INDEX_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(_markdown(document), encoding="utf-8", newline="\n")
    return document


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _markdown(document: dict[str, object]) -> str:
    lines = [
        "# Clip index",
        "",
        f"Backend: {document.get('backend') or ''}",
        "",
        "| path | duration | description | transcript |",
        "|---|---:|---|---|",
    ]
    clips = document.get("clips") or []
    assert isinstance(clips, list)
    for clip in clips:
        lines.append(
            "| {path} | {duration:.3f} | {description} | {transcript} |".format(
                path=_cell(str(clip["path"])),
                duration=float(clip["duration"]),
                description=_cell(str(clip.get("description") or "")),
                transcript=_cell(str(clip.get("transcript") or "")),
            )
        )
    lines.append("")
    for clip in clips:
        lines.append(f"## {clip['path']}")
        lines.append("")
        lines.append(f"- id: {clip['id']}")
        lines.append(f"- duration: {float(clip['duration']):.3f}s")
        description = str(clip.get("description") or "")
        lines.append(f"- description: {description}" if description else "- description:")
        lines.append("")
        transcript = str(clip.get("transcript") or "")
        if transcript:
            lines.append(transcript)
            lines.append("")
        timestamps = clip.get("timestamps") or []
        if timestamps:
            lines.append("| word | start | end |")
            lines.append("|---|---:|---:|")
            for stamp in timestamps:
                word = _cell(str(stamp["word"]))
                start = float(stamp["start"])
                end = float(stamp["end"])
                lines.append(f"| {word} | {start:.3f} | {end:.3f} |")
            lines.append("")
    return "\n".join(lines) + "\n"
