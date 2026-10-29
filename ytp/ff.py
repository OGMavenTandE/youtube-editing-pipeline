"""ffmpeg/ffprobe helpers that behave the same on Linux and Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class FFmpegError(RuntimeError):
    """ffmpeg or ffprobe exited non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        tail = (stderr or "").strip()[-2000:]
        super().__init__(f"command failed ({returncode}): {' '.join(cmd)}\n{tail}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


def hidden_kwargs() -> dict[str, object]:
    """Hide the extra console ffmpeg would open on Windows."""
    if sys.platform != "win32":
        return {}
    kwargs: dict[str, object] = {"creationflags": CREATE_NO_WINDOW}
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        info = startupinfo_cls()
        info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = info
    return kwargs


def tool_path(env_var: str, fallback_name: str) -> str:
    override = os.environ.get(env_var, "").strip()
    if override:
        return override
    found = shutil.which(fallback_name)
    if not found:
        raise FFmpegError(
            [fallback_name],
            127,
            f"{fallback_name} was not found on PATH. Install ffmpeg or set {env_var}.",
        )
    return found


def ffmpeg_bin() -> str:
    return tool_path("FFMPEG_PATH", "ffmpeg")


def ffprobe_bin() -> str:
    return tool_path("FFPROBE_PATH", "ffprobe")


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        **hidden_kwargs(),
    )
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr or proc.stdout or "")
    return proc


def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess[str]:
    return run_cmd([ffmpeg_bin(), "-hide_banner", "-nostdin", *args])


def run_ffprobe(args: list[str]) -> subprocess.CompletedProcess[str]:
    return run_cmd([ffprobe_bin(), "-hide_banner", *args])


def probe_duration(path: Path) -> float:
    proc = run_ffprobe(
        [
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(proc.stdout.strip())


def probe_video_size(path: Path) -> tuple[int, int]:
    proc = run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ]
    )
    text = proc.stdout.strip()
    width, height = text.split("x", 1)
    return int(width), int(height)


def has_audio(path: Path) -> bool:
    proc = run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    return bool(proc.stdout.strip())


def concat_line(path: Path) -> str:
    """One `file` line for the concat demuxer.

    Forward slashes and escaped quotes keep the line valid on Windows.
    """
    text = path.resolve().as_posix().replace("'", r"'\''")
    return f"file '{text}'"


def escape_filter_path(path: Path) -> str:
    """Escape a path for an ffmpeg filter argument such as subtitles=."""
    text = path.resolve().as_posix()
    return (
        text.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )
