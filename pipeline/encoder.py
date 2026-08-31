"""Pick H.264 encode path: NVIDIA NVENC when it actually works, else libx264.

``ffmpeg -encoders`` listing ``h264_nvenc`` is not enough. Distro builds
often compile NVENC in without a GPU (this fails with missing libcuda /
nvcuda). Probe the encoder list, then a one-frame smoke encode. Cache the
result for the process. If a later NVENC encode fails, fall back to libx264
and keep using software for the rest of the run.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from pipeline.config import Settings, require_ffmpeg
from pipeline.hidden_process import run_hidden

logger = logging.getLogger(__name__)

NVENC_CODEC = "h264_nvenc"
SOFTWARE_CODEC = "libx264"

EncodeQuality = Literal["medium", "veryfast"]
EncoderName = Literal["h264_nvenc", "libx264"]

# MoviePy 2.x write_videofile kwargs (2.1.2). codec is passed through as
# -vcodec; preset is always appended. pixel_format is ignored for output
# on 2.1.2, so NVENC yuv420p goes in ffmpeg_params (avoids High 4:4:4).
MOVIEPY_WRITE_VIDEOFILE_KEYS = frozenset(
    {
        "fps",
        "codec",
        "bitrate",
        "audio",
        "audio_fps",
        "preset",
        "audio_nbytes",
        "audio_codec",
        "audio_bitrate",
        "audio_bufsize",
        "temp_audiofile",
        "temp_audiofile_path",
        "remove_temp",
        "write_logfile",
        "threads",
        "ffmpeg_params",
        "logger",
        "pixel_format",
    }
)

# CQ 19 + VBR + no target bitrate ≈ x264 medium CRF 18 for 1080p talking head.
# -b:v 0 keeps CQ in charge so files stay reasonable.
NVENC_QUALITY_PARAMS: tuple[str, ...] = (
    "-rc",
    "vbr",
    "-cq",
    "19",
    "-b:v",
    "0",
    "-profile:v",
    "high",
    "-pix_fmt",
    "yuv420p",
)


@dataclass(frozen=True)
class VideoEncoder:
    """One H.264 encoder and the ffmpeg / MoviePy flags to use it."""

    name: EncoderName

    def moviepy_write_kwargs(self, *, fps: float) -> dict[str, Any]:
        """Keyword args for MoviePy 2 ``write_videofile`` (filename is positional)."""
        if self.name == NVENC_CODEC:
            return {
                "codec": NVENC_CODEC,
                "audio_codec": "aac",
                "fps": fps,
                "preset": "p4",
                "threads": 0,
                "logger": None,
                "ffmpeg_params": list(NVENC_QUALITY_PARAMS),
            }
        return {
            "codec": SOFTWARE_CODEC,
            "audio_codec": "aac",
            "fps": fps,
            "preset": "medium",
            "threads": 0,
            "logger": None,
        }

    def ffmpeg_video_args(self, *, quality: EncodeQuality = "medium") -> list[str]:
        """``-c:v`` plus encoder flags. Caller adds audio (``-c:a aac``)."""
        if self.name == NVENC_CODEC:
            preset = "p1" if quality == "veryfast" else "p4"
            return ["-c:v", NVENC_CODEC, "-preset", preset, *NVENC_QUALITY_PARAMS]
        preset = "veryfast" if quality == "veryfast" else "medium"
        return ["-c:v", SOFTWARE_CODEC, "-preset", preset, "-crf", "18"]


SOFTWARE = VideoEncoder(SOFTWARE_CODEC)
NVENC = VideoEncoder(NVENC_CODEC)

_choice: VideoEncoder | None = None
_announced = False
_choice_lock = threading.Lock()


def software_encoder() -> VideoEncoder:
    return SOFTWARE


def nvenc_encoder() -> VideoEncoder:
    return NVENC


def reset_encoder_cache() -> None:
    """Test helper. Clears the process-wide encoder choice."""
    global _choice, _announced
    with _choice_lock:
        _choice = None
        _announced = False


def remember_nvenc_failure(reason: str = "") -> VideoEncoder:
    """Stop using NVENC for this process after a real encode fails."""
    global _choice
    with _choice_lock:
        _choice = SOFTWARE
    extra = f" ({reason})" if reason else ""
    logger.warning("NVENC encode failed%s. encoder=%s", extra, SOFTWARE_CODEC)
    print(f"      encoder={SOFTWARE_CODEC} (NVENC failed{extra})")
    return SOFTWARE


def select_video_encoder(settings: Settings | None = None) -> VideoEncoder:
    """Return the cached encoder for this run. Probes ffmpeg on first call."""
    global _choice
    with _choice_lock:
        if _choice is not None:
            return _choice
        choice, reason = _probe_encoder(settings)
        _choice = choice
    _announce(choice, reason)
    return choice


def encoder_is_listed(encoders_text: str, name: str) -> bool:
    """True when ``ffmpeg -encoders`` lists ``name`` as an encoder token."""
    for line in encoders_text.splitlines():
        tokens = line.split()
        if len(tokens) >= 2 and tokens[1] == name:
            return True
    return False


def _announce(choice: VideoEncoder, reason: str = "") -> None:
    global _announced
    with _choice_lock:
        if _announced:
            return
        _announced = True
    if reason:
        logger.info("encoder=%s (%s)", choice.name, reason)
    else:
        logger.info("encoder=%s", choice.name)
    print(f"      encoder={choice.name}")


def _probe_encoder(settings: Settings | None) -> tuple[VideoEncoder, str]:
    try:
        ffmpeg_bin = require_ffmpeg(settings)
    except Exception as exc:
        return SOFTWARE, f"ffmpeg unavailable: {exc}"

    listed, list_error = _nvenc_listed(ffmpeg_bin)
    if list_error:
        return SOFTWARE, f"encoder probe failed: {list_error}"
    if not listed:
        return SOFTWARE, "h264_nvenc not listed"
    if not _nvenc_smoke_ok(ffmpeg_bin):
        return SOFTWARE, "NVENC listed but probe encode failed"
    return NVENC, ""


def _nvenc_listed(ffmpeg_bin: str) -> tuple[bool, str | None]:
    try:
        result = run_hidden(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "nonzero exit").strip()
        return False, detail
    return encoder_is_listed(result.stdout or "", NVENC_CODEC), None


def _nvenc_smoke_ok(ffmpeg_bin: str) -> bool:
    """Confirm NVENC can open a device. Listing it is not sufficient."""
    try:
        result = run_hidden(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.04",
                "-frames:v",
                "1",
                "-c:v",
                NVENC_CODEC,
                "-preset",
                "p4",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0
