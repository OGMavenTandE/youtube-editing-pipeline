from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


def _repo_root() -> Path:
    """Repo root for source checkouts; folder next to the exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _repo_root()


class FFmpegNotFoundError(RuntimeError):
    """Raised when FFmpeg is missing from PATH and FFMPEG_PATH is unset."""


class Settings(BaseModel):
    """Runtime settings loaded from the environment and optional .env file."""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    silence_min_duration: float = Field(default=1.0, ge=0.0)
    silence_padding: float = Field(default=0.30, ge=0.0)
    silence_threshold_db: float = Field(default=-45.0)
    output_width: int = Field(default=1920, ge=16)
    output_height: int = Field(default=1080, ge=16)
    pip_scale: float = Field(default=0.25, gt=0.0, le=0.6)
    split_top_ratio: float = Field(default=2.0 / 3.0, gt=0.3, lt=0.9)
    bookend_seconds: float = Field(default=10.0, ge=4.0, le=20.0)
    host_name: str = "Scott Mastin"
    host_title_line: str = "President, AI Eval Corp · SDVOSB · Independent T&E"
    host_affiliations: str = "Army Research Lab · Project Maven · CDAO"
    host_mission: str = "AI test and evaluation for the Department of War and the IC"
    host_find_me: str = "scottmastin.com|linkedin.com/in/scottmastin|aieval.org"
    pacing_hook_window: float = Field(default=60.0, ge=0.0)
    layout_hold_min: float = Field(default=8.0, ge=1.0)
    layout_hold_hook_max: float = Field(default=15.0, ge=1.0)
    layout_hold_hook_target: float = Field(default=12.0, ge=1.0)
    layout_hold_body_min: float = Field(default=15.0, ge=1.0)
    layout_hold_body_target: float = Field(default=20.0, ge=1.0)
    layout_hold_body_max: float = Field(default=25.0, ge=1.0)
    layout_hold_hard_ceiling: float = Field(default=40.0, ge=1.0)
    micro_reset_target: float = Field(default=6.0, ge=1.0)
    micro_reset_min: float = Field(default=5.0, ge=1.0)
    micro_reset_max: float = Field(default=7.0, ge=1.0)
    punch_in_scale: float = Field(default=1.15, ge=1.0, le=1.4)
    punch_in_duration: float = Field(default=1.6, ge=0.3)
    text_hold: float = Field(default=2.4, ge=0.4)
    max_same_layout_streak: int = Field(default=3, ge=2)
    director_chunk_seconds: float = Field(default=300.0, ge=60.0)
    director_chunk_threshold: float = Field(default=480.0, ge=60.0)
    target_lufs: float = Field(default=-14.0, le=0.0)
    input_dir: Path = REPO_ROOT / "input"
    output_dir: Path = REPO_ROOT / "output"
    work_dir: Path = REPO_ROOT / "work"
    slides_dir: Path = REPO_ROOT / "work" / "slides"
    scenes_dir: Path = REPO_ROOT / "work" / "scenes"
    encode_concurrency: int = Field(default=2, ge=1, le=8)

    def ensure_dirs(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.slides_dir.mkdir(parents=True, exist_ok=True)
        self.scenes_dir.mkdir(parents=True, exist_ok=True)


def default_env_file() -> Path:
    """`.env` next to the install, or `YOUTUBE_PIPELINE_ENV` when set.

    The desktop app sets `YOUTUBE_PIPELINE_ENV` to the user-data `.env`
    so frozen builds do not depend on a file next to the EXE.
    """
    override = os.getenv("YOUTUBE_PIPELINE_ENV", "").strip()
    if override:
        return Path(override)
    return REPO_ROOT / ".env"


def load_settings(env_file: Path | None = None) -> Settings:
    """Load dotenv, then map known env vars onto Settings."""
    load_dotenv(env_file or default_env_file())
    ffmpeg_bin = os.getenv("FFMPEG_PATH") or os.getenv("FFMPEG_BIN") or "ffmpeg"
    ffprobe_default = "ffprobe"
    if ffmpeg_bin not in {"ffmpeg", ""}:
        sibling = Path(ffmpeg_bin).with_name("ffprobe")
        if sibling.exists():
            ffprobe_default = str(sibling)
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
        or "gemini-3.6-flash",
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=os.getenv("FFPROBE_PATH") or os.getenv("FFPROBE_BIN") or ffprobe_default,
        silence_min_duration=float(os.getenv("SILENCE_MIN_DURATION", "1.0")),
        silence_padding=float(os.getenv("SILENCE_PADDING", "0.30")),
        silence_threshold_db=float(os.getenv("SILENCE_THRESHOLD_DB", "-45")),
        output_width=int(os.getenv("OUTPUT_WIDTH", "1920")),
        output_height=int(os.getenv("OUTPUT_HEIGHT", "1080")),
        pip_scale=float(os.getenv("PIP_SCALE", "0.25")),
        split_top_ratio=float(os.getenv("SPLIT_TOP_RATIO", str(2.0 / 3.0))),
        bookend_seconds=float(os.getenv("BOOKEND_SECONDS", "10")),
        host_name=os.getenv("HOST_NAME", "Scott Mastin").strip() or "Scott Mastin",
        host_title_line=os.getenv(
            "HOST_TITLE_LINE",
            "President, AI Eval Corp · SDVOSB · Independent T&E",
        ).strip(),
        host_affiliations=os.getenv(
            "HOST_AFFILIATIONS",
            "Army Research Lab · Project Maven · CDAO",
        ).strip(),
        host_mission=os.getenv(
            "HOST_MISSION",
            "AI test and evaluation for the Department of War and the IC",
        ).strip(),
        host_find_me=os.getenv(
            "HOST_FIND_ME",
            "scottmastin.com|linkedin.com/in/scottmastin|aieval.org",
        ).strip(),
        pacing_hook_window=float(os.getenv("PACING_HOOK_WINDOW", "60")),
        layout_hold_min=float(os.getenv("LAYOUT_HOLD_MIN", "8")),
        layout_hold_hook_max=float(os.getenv("LAYOUT_HOLD_HOOK_MAX", "15")),
        layout_hold_hook_target=float(os.getenv("LAYOUT_HOLD_HOOK_TARGET", "12")),
        layout_hold_body_min=float(os.getenv("LAYOUT_HOLD_BODY_MIN", "15")),
        layout_hold_body_target=float(os.getenv("LAYOUT_HOLD_BODY_TARGET", "20")),
        layout_hold_body_max=float(os.getenv("LAYOUT_HOLD_BODY_MAX", "25")),
        layout_hold_hard_ceiling=float(os.getenv("LAYOUT_HOLD_HARD_CEILING", "40")),
        micro_reset_target=float(os.getenv("MICRO_RESET_TARGET", "6")),
        punch_in_scale=float(os.getenv("PUNCH_IN_SCALE", "1.15")),
        punch_in_duration=float(os.getenv("PUNCH_IN_DURATION", "1.6")),
        text_hold=float(os.getenv("TEXT_HOLD", "2.4")),
        max_same_layout_streak=int(os.getenv("MAX_SAME_LAYOUT_STREAK", "3")),
        director_chunk_seconds=float(os.getenv("DIRECTOR_CHUNK_SECONDS", "300")),
        director_chunk_threshold=float(os.getenv("DIRECTOR_CHUNK_THRESHOLD", "480")),
        target_lufs=float(os.getenv("TARGET_LUFS", "-14")),
        input_dir=Path(os.getenv("INPUT_DIR", str(REPO_ROOT / "input"))),
        output_dir=Path(os.getenv("OUTPUT_DIR", str(REPO_ROOT / "output"))),
        work_dir=Path(os.getenv("WORK_DIR", str(REPO_ROOT / "work"))),
        slides_dir=Path(os.getenv("SLIDES_DIR", str(REPO_ROOT / "work" / "slides"))),
        scenes_dir=Path(os.getenv("SCENES_DIR", str(REPO_ROOT / "work" / "scenes"))),
        encode_concurrency=int(os.getenv("ENCODE_CONCURRENCY", "2")),
    )


def identity_from_settings(settings: Settings):
    from pipeline.models import HostIdentity

    links = [part.strip() for part in settings.host_find_me.split("|") if part.strip()]
    return HostIdentity(
        name=settings.host_name,
        title_line=settings.host_title_line,
        affiliations=settings.host_affiliations,
        mission=settings.host_mission,
        find_me=links or ["scottmastin.com", "linkedin.com/in/scottmastin", "aieval.org"],
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
