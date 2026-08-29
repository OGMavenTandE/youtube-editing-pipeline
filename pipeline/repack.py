"""Rewrite an existing Studio folder without re-running pipeline stages."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from pipeline.config import Settings
from pipeline.models import YouTubeMetadata
from pipeline.studio import StudioPackage, write_studio_package

VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
_STEM_TAILS = (
    "_studio",
    "_final",
    "_youtube_metadata",
    "_edit_script",
    "_cut_map",
    "_transcript",
    "_trimmed",
)


class StudioRun(BaseModel):
    """Resolved artifacts for a finished run that already has a Studio folder."""

    stem: str
    studio_dir: Path
    video_path: Path
    metadata_path: Path
    webcam_path: Path
    metadata: YouTubeMetadata


def strip_run_stem(name: str) -> str:
    for tail in _STEM_TAILS:
        if name.endswith(tail):
            return name[: -len(tail)]
    return name


def list_studio_dirs(settings: Settings) -> list[Path]:
    if not settings.output_dir.is_dir():
        return []
    found = [
        path.resolve()
        for path in settings.output_dir.iterdir()
        if path.is_dir() and path.name.endswith("_studio")
    ]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def infer_stem(raw: str | Path, settings: Settings) -> tuple[str, Path]:
    """Turn a studio folder, stem, input, or final MP4 into (stem, studio_dir)."""
    text = str(raw).strip()
    if not text:
        raise FileNotFoundError("Studio path or stem is empty.")
    path = Path(text).expanduser()
    search = [path]
    if not path.is_absolute():
        search.extend(
            [
                Path.cwd() / path,
                settings.output_dir / path,
                settings.output_dir / path.name,
                settings.input_dir / path,
                settings.work_dir / path,
            ]
        )
    for candidate in search:
        if candidate.is_dir() and (
            candidate.name.endswith("_studio") or (candidate / "titles.txt").is_file()
        ):
            stem = strip_run_stem(candidate.name)
            return stem, candidate.resolve()
        if candidate.is_file():
            stem = strip_run_stem(candidate.stem)
            return stem, (settings.output_dir / f"{stem}_studio").resolve()
    stem = strip_run_stem(path.stem if path.suffix else path.name)
    return stem, (settings.output_dir / f"{stem}_studio").resolve()


def load_run_metadata(stem: str, settings: Settings) -> tuple[Path, YouTubeMetadata]:
    """JSON is the machine source of truth. Prefer *_youtube_metadata.json."""
    meta_path = settings.output_dir / f"{stem}_youtube_metadata.json"
    if meta_path.is_file():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta_path, YouTubeMetadata.model_validate(payload)
    script_path = settings.output_dir / f"{stem}_edit_script.json"
    if script_path.is_file():
        payload = json.loads(script_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise FileNotFoundError(f"Edit script is not an object: {script_path}")
        raw_meta = payload.get("metadata", payload)
        return script_path, YouTubeMetadata.model_validate(raw_meta)
    raise FileNotFoundError(
        f"Missing {meta_path.name} (and no {script_path.name}) for {stem!r}. "
        "JSON stays the source of truth; titles will not be invented."
    )


def find_final_video(stem: str, studio_dir: Path, settings: Settings) -> Path:
    candidates = [
        settings.output_dir / f"{stem}_final.mp4",
        studio_dir / f"{stem}_final.mp4",
    ]
    if studio_dir.is_dir():
        candidates.extend(
            sorted(
                path
                for path in studio_dir.iterdir()
                if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
            )
        )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(
        f"Final MP4 not found for {stem!r}. "
        f"Expected {settings.output_dir / f'{stem}_final.mp4'} "
        f"or a video inside {studio_dir}."
    )


def _resolve_hint(hint: str | Path | None, settings: Settings) -> Path | None:
    if hint is None:
        return None
    path = Path(str(hint)).expanduser()
    if path.is_file():
        return path.resolve()
    fallback = settings.input_dir / path.name
    if fallback.is_file():
        return fallback.resolve()
    return path


def find_webcam_path(
    stem: str,
    settings: Settings,
    *,
    extra: Path | None = None,
) -> Path:
    """Prefer the trimmed talking-head file. Never invent a black frame."""
    preferred: list[Path] = []
    fallback: list[Path] = []
    if settings.work_dir.is_dir():
        preferred.extend(
            settings.work_dir / f"{stem}_trimmed{suffix}" for suffix in VIDEO_SUFFIXES
        )
        for path in sorted(settings.work_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            name = path.stem.casefold()
            stem_key = stem.casefold()
            if not (name == f"{stem_key}_trimmed" or name.startswith(f"{stem_key}_")):
                continue
            if name.endswith("_trimmed") or "_cut" in name:
                preferred.append(path)
    if extra is not None:
        fallback.append(Path(extra))
    fallback.extend(settings.input_dir / f"{stem}{suffix}" for suffix in VIDEO_SUFFIXES)

    looked: list[Path] = []
    seen: set[Path] = set()
    for path in preferred + fallback:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        looked.append(resolved)
        if resolved.is_file():
            return resolved

    sample = "\n".join(f"  {path}" for path in looked[:8]) or "  (no candidate paths)"
    raise FileNotFoundError(
        f"No talking-head frame source for {stem!r}. "
        "Need the trimmed webcam cut (preferred) or the original talking-head file. "
        "A black frame will not be used.\n"
        f"Looked at:\n{sample}\n"
        "Pass --input to the webcam/trim file, or re-run silence trim so "
        f"{settings.work_dir / f'{stem}_trimmed.mp4'} exists."
    )


def resolve_studio_run(
    raw: str | Path,
    settings: Settings,
    *,
    input_hint: str | Path | None = None,
) -> StudioRun:
    stem, studio_dir = infer_stem(raw, settings)
    if not studio_dir.is_dir():
        raise FileNotFoundError(
            f"Studio folder not found: {studio_dir}. "
            "Run the pipeline first, or pass an existing output/<stem>_studio directory."
        )
    metadata_path, metadata = load_run_metadata(stem, settings)
    video_path = find_final_video(stem, studio_dir, settings)
    webcam_path = find_webcam_path(
        stem,
        settings,
        extra=_resolve_hint(input_hint, settings),
    )
    return StudioRun(
        stem=stem,
        studio_dir=studio_dir,
        video_path=video_path,
        metadata_path=metadata_path,
        webcam_path=webcam_path,
        metadata=metadata,
    )


def repack_studio(
    raw: str | Path,
    settings: Settings,
    *,
    title_index: int = 0,
    input_hint: str | Path | None = None,
    fallback_title: str = "",
) -> StudioPackage:
    """Rewrite studio text files and thumbnail. Reuses the existing MP4."""
    run = resolve_studio_run(raw, settings, input_hint=input_hint)
    label = fallback_title or run.stem.replace("_", " ").replace("-", " ").strip()
    transcript_path = settings.output_dir / f"{run.stem}_transcript.json"
    return write_studio_package(
        video_path=run.video_path,
        webcam_path=run.webcam_path,
        metadata=run.metadata,
        dest_dir=run.studio_dir,
        settings=settings,
        fallback_title=label,
        title_index=title_index,
        transcript_path=transcript_path if transcript_path.is_file() else None,
        metadata_path=run.metadata_path,
    )
