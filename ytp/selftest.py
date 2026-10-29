"""Print what a Windows machine can actually run. Changes nothing."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ytp.ff import ffmpeg_bin, ffprobe_bin
from ytp.render import ffmpeg_encoders, reset_encoder_cache
from ytp.transcribe import _cuda_or_cpu, enable_nvidia_dlls


def self_test_failed(lines: list[str]) -> bool:
    """True when ffmpeg is missing or the tiny Whisper run did not succeed."""
    for line in lines:
        if line.startswith("ffmpeg: missing"):
            return True
        if line.startswith("whisper: failed"):
            return True
        if line.startswith("whisper: not installed"):
            return True
    return False


def self_test_lines() -> list[str]:
    reset_encoder_cache()
    lines = [f"python: {sys.version.split()[0]} ({sys.executable})"]
    try:
        ffmpeg = ffmpeg_bin()
        lines.append(f"ffmpeg: {ffmpeg}")
    except Exception as exc:
        lines.append(f"ffmpeg: missing ({exc})")
        ffmpeg = ""
    try:
        lines.append(f"ffprobe: {ffprobe_bin()}")
    except Exception as exc:
        lines.append(f"ffprobe: missing ({exc})")
    try:
        listed = ffmpeg_encoders()
        lines.append(f"nvenc: {'yes' if 'h264_nvenc' in listed else 'no'}")
    except Exception as exc:
        lines.append(f"nvenc: unknown ({exc})")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    enable_nvidia_dlls()
    device = _cuda_or_cpu()
    lines.append(f"cuda: {device}")
    lines.append(_whisper_line())
    return lines


def _whisper_line() -> str:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return "whisper: not installed"
    from ytp.ff import run_ffmpeg
    from ytp.transcribe import FasterWhisperTranscriber

    try:
        with tempfile.TemporaryDirectory(prefix="ytp-selftest-") as work:
            wav = Path(work) / "tone.wav"
            run_ffmpeg(
                [
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000:duration=0.4",
                    "-c:a",
                    "pcm_s16le",
                    str(wav),
                ]
            )
            device = "cuda" if _cuda_or_cpu() == "cuda" else "cpu"
            heard = FasterWhisperTranscriber("tiny", device, "en").transcribe(wav)
            return f"whisper: ok device={device} words={len(heard.words)}"
    except Exception as exc:
        return f"whisper: failed ({exc})"
