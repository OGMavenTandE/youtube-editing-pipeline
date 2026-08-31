from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline.config import Settings, require_ffmpeg, require_ffprobe
from pipeline.encoder import (
    NVENC_CODEC,
    VideoEncoder,
    remember_nvenc_failure,
    select_video_encoder,
    software_encoder,
)
from pipeline.hidden_process import run_hidden


class MediaError(RuntimeError):
    """FFmpeg or probe failure."""


def probe_media(path: Path, settings: Settings) -> dict[str, Any]:
    """ffprobe JSON with Windows consoles hidden. Replaces ffmpeg.probe."""
    ffprobe = require_ffprobe(settings)
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = run_hidden(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise MediaError(f"ffprobe returned no object for {path}")
    return payload


def probe_duration(path: Path, settings: Settings) -> float:
    info = probe_media(path, settings)
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
    result = run_hidden(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(
            f"Could not extract audio from {video_path}. "
            "The file may have no audio track.\n"
            f"{result.stderr.strip()}"
        )
    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError(f"Audio extract produced an empty file: {dest}")
    return dest


def extract_compact_audio(src: Path, dest: Path, settings: Settings) -> Path:
    """16 kHz mono MP3, typically well under Gemini's inline size limit."""
    ffmpeg_bin = require_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(dest),
    ]
    result = run_hidden(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(
            f"Could not extract compact audio from {src}.\n{result.stderr.strip()}"
        )
    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError(f"Compact audio extract produced an empty file: {dest}")
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

        def build(encoder: VideoEncoder) -> list[str]:
            return [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{start:.3f}",
                "-to",
                f"{end:.3f}",
                "-i",
                str(video_path),
                *encoder.ffmpeg_video_args(quality="veryfast"),
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(dest),
            ]

        _run_encode(settings, f"trim {video_path} -> {dest}", build)
        return dest

    work = dest.parent / f"{dest.stem}_parts"
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for index, (start, end) in enumerate(ranges):
            part = work / f"part_{index:04d}.mp4"

            def build_part(
                encoder: VideoEncoder,
                *,
                _start: float = start,
                _end: float = end,
                _part: Path = part,
            ) -> list[str]:
                return [
                    ffmpeg_bin,
                    "-y",
                    "-ss",
                    f"{_start:.3f}",
                    "-to",
                    f"{_end:.3f}",
                    "-i",
                    str(video_path),
                    *encoder.ffmpeg_video_args(quality="veryfast"),
                    "-c:a",
                    "aac",
                    str(_part),
                ]

            _run_encode(settings, f"extract part {index}", build_part)
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


def extract_frame(
    video_path: Path,
    dest: Path,
    settings: Settings,
    *,
    at_seconds: float,
) -> Path:
    """Grab one video frame. Used for the Studio thumbnail webcam still."""
    ffmpeg_bin = require_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamp = max(0.0, at_seconds)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{stamp:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]
    try:
        _run(cmd, f"extract frame {video_path} @ {stamp:.2f}s")
    except MediaError:
        if stamp > 0:
            return extract_frame(video_path, dest, settings, at_seconds=0.0)
        raise
    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError(f"Frame extract produced an empty file: {dest}")
    return dest


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def concat_scene_files(
    parts: list[Path],
    dest: Path,
    settings: Settings,
    *,
    loudnorm: bool = True,
) -> Path:
    """Concat per-scene MP4s with ffmpeg. Optional YouTube ~-14 LUFS loudnorm."""
    if not parts:
        raise MediaError("No scene files to concat.")
    ffmpeg_bin = require_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.parent / f"{dest.stem}_concat"
    work.mkdir(parents=True, exist_ok=True)
    list_file = work / "concat.txt"
    list_file.write_text(
        "".join(f"file '{path.resolve()}'\n" for path in parts),
        encoding="utf-8",
    )
    target = settings.target_lufs

    def build_concat(encoder: VideoEncoder) -> list[str]:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
        ]
        if loudnorm:
            cmd.extend(
                [
                    "-af",
                    f"loudnorm=I={target:.1f}:TP=-1.5:LRA=11",
                    *encoder.ffmpeg_video_args(quality="medium"),
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                ]
            )
        else:
            cmd.extend(["-c", "copy"])
        cmd.extend(["-movflags", "+faststart", str(dest)])
        return cmd

    try:
        if loudnorm:
            _run_encode(settings, f"concat scenes -> {dest}", build_concat)
        else:
            _run(build_concat(software_encoder()), f"concat scenes -> {dest}")
    finally:
        if list_file.exists():
            list_file.unlink()
        try:
            work.rmdir()
        except OSError:
            pass
    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError(f"Concat produced no output at {dest}")
    return dest


def apply_loudnorm(video_path: Path, dest: Path, settings: Settings) -> Path:
    """Single-pass ffmpeg loudnorm toward settings.target_lufs."""
    ffmpeg_bin = require_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def build(encoder: VideoEncoder) -> list[str]:
        return [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-af",
            f"loudnorm=I={settings.target_lufs:.1f}:TP=-1.5:LRA=11",
            *encoder.ffmpeg_video_args(quality="medium"),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(dest),
        ]

    _run_encode(settings, f"loudnorm {video_path} -> {dest}", build)
    return dest


def _run_encode(
    settings: Settings,
    label: str,
    build: Callable[[VideoEncoder], list[str]],
) -> None:
    """Run an ffmpeg video encode; retry once with libx264 if NVENC fails."""
    encoder = select_video_encoder(settings)
    try:
        _run(build(encoder), label)
    except MediaError as exc:
        if encoder.name != NVENC_CODEC:
            raise
        remember_nvenc_failure(str(exc))
        _run(build(software_encoder()), f"{label} (libx264 fallback)")


def _run(cmd: list[str], label: str) -> None:
    result = run_hidden(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(f"{label} failed:\n{result.stderr.strip()}")
