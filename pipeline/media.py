from __future__ import annotations

import json
import subprocess
from pathlib import Path

import ffmpeg

from pipeline.config import Settings, require_ffmpeg, require_ffprobe


class MediaError(RuntimeError):
    """FFmpeg or probe failure."""


def probe_duration(path: Path, settings: Settings) -> float:
    ffprobe = require_ffprobe(settings)
    try:
        info = ffmpeg.probe(str(path), cmd=ffprobe)
    except ffmpeg.Error as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
        raise MediaError(f"ffprobe failed for {path}: {stderr}") from exc
    duration = info.get("format", {}).get("duration")
    if duration is None:
        for stream in info.get("streams", []):
            if stream.get("duration"):
                duration = stream["duration"]
                break
    if duration is None:
        raise MediaError(f"Could not read duration from {path}")
    return float(duration)


def extract_audio(video_path: Path, dest: Path, settings: Settings) -> Path:
    """Extract mono 16 kHz WAV suitable for Gemini and pydub."""
    ffmpeg_bin = require_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(
            f"Could not extract audio from {video_path}. "
            "The file may have no audio track.\n"
            f"{result.stderr.strip()}"
        )
    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError(f"Audio extract produced an empty file: {dest}")
    return dest


def concat_keep_ranges(
    video_path: Path,
    ranges: list[tuple[float, float]],
    dest: Path,
    settings: Settings,
) -> Path:
    """Re-encode only the keep ranges, then concat. Safer than stream-copy at cut points."""
    if not ranges:
        raise MediaError("No keep ranges to concat; the clip would be empty.")
    ffmpeg_bin = require_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(ranges) == 1:
        start, end = ranges[0]
        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(video_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        _run(cmd, f"trim {video_path} -> {dest}")
        return dest

    work = dest.parent / f"{dest.stem}_parts"
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for index, (start, end) in enumerate(ranges):
            part = work / f"part_{index:04d}.mp4"
            cmd = [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{start:.3f}",
                "-to",
                f"{end:.3f}",
                "-i",
                str(video_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                str(part),
            ]
            _run(cmd, f"extract part {index}")
            parts.append(part)
        list_file = work / "concat.txt"
        list_file.write_text(
            "".join(f"file '{part.resolve()}'\n" for part in parts),
            encoding="utf-8",
        )
        cmd = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        _run(cmd, f"concat parts -> {dest}")
    finally:
        for part in parts:
            if part.exists():
                part.unlink()
        list_path = work / "concat.txt"
        if list_path.exists():
            list_path.unlink()
        if work.exists():
            try:
                work.rmdir()
            except OSError:
                pass
    return dest


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _run(cmd: list[str], label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(f"{label} failed:\n{result.stderr.strip()}")
