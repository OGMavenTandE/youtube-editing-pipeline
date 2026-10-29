"""Turn one camera take into an episode folder without copying the file."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ytp.index import INDEX_JSON, INDEX_MD, _clip_id, _markdown
from ytp.layout import init_episode
from ytp.transcribe import Transcriber, Transcript, Word


def episode_dir_for_take(video: Path, episode: str | None) -> Path:
    """Folder beside the take, named after the file unless ``episode`` is set."""
    name = (episode or video.stem).strip()
    if not name or name in {".", ".."}:
        raise ValueError(f"episode name is empty for {video}")
    return video.parent / name


def link_take(video: Path, episode_dir: Path) -> Path:
    """Hardlink the take into ``source/raw``. Never copy it and never modify it.

    A cross-volume link fails. Then ``edit/take.path`` stores the absolute
    path and the original file is transcribed in place.
    """
    video = video.resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    before = video.stat()
    dest_dir = episode_dir / "source" / "raw"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / video.name
    if dest.exists() or dest.is_symlink():
        _unchanged(video, before)
        return dest
    try:
        os.link(video, dest)
    except OSError:
        pointer = episode_dir / "edit" / "take.path"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(video) + "\n", encoding="utf-8", newline="\n")
        _unchanged(video, before)
        return video
    _unchanged(video, before)
    return dest


def _unchanged(video: Path, before: os.stat_result) -> None:
    after = video.stat()
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise RuntimeError(f"take was modified: {video}")


def clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes, rest = divmod(seconds, 60.0)
    return f"{int(minutes)}:{rest:05.2f}"


def _sentence_rows(words: list[Word]) -> list[tuple[float, float, str]]:
    """One row per sentence. A word ending in . ! or ? closes the sentence."""
    rows: list[tuple[float, float, str]] = []
    bucket: list[Word] = []
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        bucket.append(word)
        if text.endswith((".", "!", "?")):
            rows.append(
                (
                    bucket[0].start,
                    bucket[-1].end,
                    " ".join(item.text.strip() for item in bucket),
                )
            )
            bucket = []
    if bucket:
        rows.append(
            (bucket[0].start, bucket[-1].end, " ".join(item.text.strip() for item in bucket))
        )
    return rows


def transcript_rows(transcript: Transcript) -> list[tuple[float, float, str]]:
    """One line per Whisper segment, or per sentence when the file is one segment."""
    segments = [
        (start, end, text.strip()) for start, end, text in transcript.segments if text.strip()
    ]
    sentences = _sentence_rows(transcript.words)
    if len(segments) > 1:
        return segments
    if len(sentences) > 1:
        return sentences
    if segments:
        return segments
    if sentences:
        return sentences
    if transcript.text.strip():
        return [(0.0, 0.0, transcript.text.strip())]
    return []


def transcript_text(transcript: Transcript) -> str:
    """Plain lines with a timestamp range. One line per segment or sentence."""
    rows = transcript_rows(transcript)
    lines = [f"[{clock(start)}-{clock(end)}] {text}" for start, end, text in rows]
    return "\n".join(lines) + ("\n" if lines else "")


def write_words(path: Path, transcript: Transcript, model: str) -> None:
    payload = {
        "model": model,
        "language": transcript.language or "en",
        "words": [
            {
                "word": word.text,
                "start": round(word.start, 3),
                "end": round(word.end, 3),
                "confidence": word.confidence,
            }
            for word in transcript.words
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_take_index(episode_dir: Path, media: Path, transcript: Transcript, backend: str) -> None:
    try:
        relative = media.resolve().relative_to(episode_dir.resolve()).as_posix()
    except ValueError:
        relative = media.resolve().as_posix()
    from ytp.ff import probe_duration

    document: dict[str, object] = {
        "series": _series(episode_dir),
        "episode_dir": episode_dir.resolve().as_posix(),
        "generated_at": _generated_at(),
        "backend": backend,
        "clips": [
            {
                "id": _clip_id(relative, set()),
                "path": relative,
                "duration": round(probe_duration(media), 3),
                "description": "",
                "transcript": transcript.text,
                "language": transcript.language or "en",
                "timestamps": [
                    {
                        "word": word.text,
                        "start": round(word.start, 3),
                        "end": round(word.end, 3),
                    }
                    for word in transcript.words
                ],
            }
        ],
    }
    json_path = episode_dir / INDEX_JSON
    md_path = episode_dir / INDEX_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(_markdown(document), encoding="utf-8", newline="\n")


def _series(episode_dir: Path) -> str:
    import yaml

    manifest = episode_dir / "episode.yaml"
    if not manifest.is_file():
        return ""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    return str(data.get("series") or "")


def _generated_at() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ingest_take(
    video: Path,
    *,
    series: str,
    episode: str | None,
    transcriber: Transcriber,
) -> Path:
    """Init ``<stem>/`` beside the take, link it, and write the transcript files."""
    episode_dir = episode_dir_for_take(video, episode)
    init_episode(episode_dir, series)
    media = link_take(video, episode_dir)
    transcript = transcriber.transcribe(media)
    edit = episode_dir / "edit"
    edit.mkdir(parents=True, exist_ok=True)
    transcript_path = edit / "transcript.txt"
    transcript_path.write_text(transcript_text(transcript), encoding="utf-8", newline="\n")
    write_words(edit / "words.json", transcript, getattr(transcriber, "name", "") or "small")
    write_take_index(episode_dir, media, transcript, getattr(transcriber, "name", "") or "small")
    return episode_dir


def warn_cpu(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)
