"""Tiny synthetic media for tests. Requires ffmpeg on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])


def write_png(path: Path, *, color: str = "black", size: str = "320x180") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}",
            "-frames:v",
            "1",
            str(path),
        ]
    )
    return path


def write_wav(path: Path, *, seconds: float = 1.0, frequency: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def write_clip(
    path: Path,
    *,
    color: str = "navy",
    size: str = "320x180",
    seconds: float = 0.4,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:d={seconds}:r=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=880:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    return path
