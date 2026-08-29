from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent


class FFmpegNotFoundError(RuntimeError):
    """Raised when FFmpeg is missing from PATH and FFMPEG_PATH is unset."""


class Settings(BaseModel):
    """Runtime settings loaded from the environment and optional .env file."""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    silence_min_duration: float = Field(default=0.7, ge=0.0)
    silence_padding: float = Field(default=0.15, ge=0.0)
    silence_threshold_db: float = Field(default=-40.0)
    output_width: int = Field(default=1920, ge=16)
    output_height: int = Field(default=1080, ge=16)
    pip_scale: float = Field(default=0.25, gt=0.0, le=0.6)
    split_top_ratio: float = Field(default=2.0 / 3.0, gt=0.3, lt=0.9)
    input_dir: Path = REPO_ROOT / "input"
    output_dir: Path = REPO_ROOT / "output"
    work_dir: Path = REPO_ROOT / "work"
    slides_dir: Path = REPO_ROOT / "work" / "slides"

    def ensure_dirs(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.slides_dir.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load dotenv, then map known env vars onto Settings."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    ffmpeg_bin = os.getenv("FFMPEG_PATH") or os.getenv("FFMPEG_BIN") or "ffmpeg"
    ffprobe_default = "ffprobe"
    if ffmpeg_bin not in {"ffmpeg", ""}:
        sibling = Path(ffmpeg_bin).with_name("ffprobe")
        if sibling.exists():
            ffprobe_default = str(sibling)
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
        or "gemini-2.5-flash",
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=os.getenv("FFPROBE_PATH") or os.getenv("FFPROBE_BIN") or ffprobe_default,
        silence_min_duration=float(os.getenv("SILENCE_MIN_DURATION", "0.7")),
        silence_padding=float(os.getenv("SILENCE_PADDING", "0.15")),
        silence_threshold_db=float(os.getenv("SILENCE_THRESHOLD_DB", "-40")),
        output_width=int(os.getenv("OUTPUT_WIDTH", "1920")),
        output_height=int(os.getenv("OUTPUT_HEIGHT", "1080")),
        pip_scale=float(os.getenv("PIP_SCALE", "0.25")),
        split_top_ratio=float(os.getenv("SPLIT_TOP_RATIO", str(2.0 / 3.0))),
        input_dir=Path(os.getenv("INPUT_DIR", str(REPO_ROOT / "input"))),
        output_dir=Path(os.getenv("OUTPUT_DIR", str(REPO_ROOT / "output"))),
        work_dir=Path(os.getenv("WORK_DIR", str(REPO_ROOT / "work"))),
        slides_dir=Path(os.getenv("SLIDES_DIR", str(REPO_ROOT / "work" / "slides"))),
    )


def which_or_path(binary: str) -> str | None:
    if not binary:
        return None
    found = shutil.which(binary)
    if found:
        return found
    candidate = Path(binary)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    return None


def require_ffmpeg(settings: Settings | None = None) -> str:
    """Return the FFmpeg executable path, or raise a setup-oriented error."""
    settings = settings or load_settings()
    path = which_or_path(settings.ffmpeg_bin)
    if path:
        return path
    raise FFmpegNotFoundError(
        "FFmpeg was not found on PATH"
        + (f" (looked for {settings.ffmpeg_bin!r})" if settings.ffmpeg_bin != "ffmpeg" else "")
        + ".\n"
        "Install FFmpeg, then retry:\n"
        "  macOS:   brew install ffmpeg\n"
        "  Ubuntu:  sudo apt-get update && sudo apt-get install -y ffmpeg\n"
        "  Windows: winget install Gyan.FFmpeg   (or choco install ffmpeg)\n"
        "Or set FFMPEG_PATH in .env to the full path of the ffmpeg binary."
    )


def require_ffprobe(settings: Settings | None = None) -> str:
    settings = settings or load_settings()
    path = which_or_path(settings.ffprobe_bin)
    if path:
        return path
    raise FFmpegNotFoundError(
        "ffprobe was not found on PATH. It ships with FFmpeg. "
        "Install FFmpeg or set FFPROBE_PATH in .env."
    )
