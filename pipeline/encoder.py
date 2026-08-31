"""Pick H.264 encode path: NVIDIA NVENC when it actually works, else libx264.

``ffmpeg -encoders`` listing ``h264_nvenc`` is not enough. Distro builds
often compile NVENC in without a GPU (this fails with missing libcuda /
nvcuda). Probe the encoder list, then a one-frame smoke encode. Cache the
result for the process. If a later NVENC encode fails, fall back to HQ
libx264 (slow, CRF 17) and keep using software for the rest of the run.
Never fall back to a tiny/fast encode.
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

# Visually lossless talking-head. Quality first; file size second.
HQ_X264_CRF = 17
HQ_NVENC_CQ = 17
HQ_X264_PRESET = "slow"
HQ_NVENC_PRESET = "p6"
FAST_X264_PRESET = "veryfast"
FAST_NVENC_PRESET = "p1"
# Reject decimated / preview rates. Camera originals are 24–60 fps.
MIN_PLAYBACK_FPS = 12.0
# Floor for a capped bitrate if one is ever set. CQ/CRF use -b:v 0.
MIN_HQ_BITRATE_KBPS = 4000

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

# CQ 17 + VBR + no target bitrate ≈ x264 slow CRF 17 for 1080p talking head.
# -b:v 0 keeps CQ in charge. Do not set 2 Mbps / YouTube upload guesses.
NVENC_QUALITY_PARAMS: tuple[str, ...] = (
    "-rc",
    "vbr",
    "-cq",
    str(HQ_NVENC_CQ),
    "-b:v",
    "0",
    "-profile:v",
    "high",
    "-pix_fmt",
    "yuv420p",
)

SOFTWARE_QUALITY_PARAMS: tuple[str, ...] = (
    "-crf",
    str(HQ_X264_CRF),
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
                "preset": HQ_NVENC_PRESET,
                "threads": 0,
                "logger": None,
                "ffmpeg_params": list(NVENC_QUALITY_PARAMS),
            }
        return {
            "codec": SOFTWARE_CODEC,
            "audio_codec": "aac",
            "fps": fps,
            "preset": HQ_X264_PRESET,
            "threads": 0,
            "logger": None,
            "ffmpeg_params": list(SOFTWARE_QUALITY_PARAMS),
        }

    def ffmpeg_video_args(self, *, quality: EncodeQuality = "medium") -> list[str]:
        """``-c:v`` plus encoder flags. Caller adds audio (``-c:a aac``).

        ``veryfast`` only changes the preset. CRF/CQ stay at the HQ values so a
        trim or retry cannot emit a mushy bitrate or CRF 23/28 default.
        """
        if self.name == NVENC_CODEC:
            preset = FAST_NVENC_PRESET if quality == "veryfast" else HQ_NVENC_PRESET
            return ["-c:v", NVENC_CODEC, "-preset", preset, *NVENC_QUALITY_PARAMS]
        preset = FAST_X264_PRESET if quality == "veryfast" else HQ_X264_PRESET
        return ["-c:v", SOFTWARE_CODEC, "-preset", preset, *SOFTWARE_QUALITY_PARAMS]


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
    logger.warning(
        "NVENC encode failed%s. encoder=%s CRF %s HQ",
        extra,
        SOFTWARE_CODEC,
        HQ_X264_CRF,
    )
    print(
        f"      encoder={SOFTWARE_CODEC} CRF {HQ_X264_CRF} "
        f"(NVENC failed{extra}; HQ software, not a small/fast encode)"
    )
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
    if choice.name == SOFTWARE_CODEC:
        detail = reason or "software HQ"
        logger.info(
            "encoder=%s CRF %s (%s)",
            SOFTWARE_CODEC,
            HQ_X264_CRF,
            detail,
        )
        print(f"      encoder={SOFTWARE_CODEC} CRF {HQ_X264_CRF} ({detail})")
        return
    if reason:
        logger.info("encoder=%s CQ %s (%s)", NVENC_CODEC, HQ_NVENC_CQ, reason)
        print(f"      encoder={NVENC_CODEC} CQ {HQ_NVENC_CQ} ({reason})")
        return
    logger.info("encoder=%s CQ %s", NVENC_CODEC, HQ_NVENC_CQ)
    print(f"      encoder={NVENC_CODEC} CQ {HQ_NVENC_CQ}")


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
                HQ_NVENC_PRESET,
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


def _flag_value(args: list[str], flag: str) -> str | None:
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _parse_kbps(value: str) -> float | None:
    text = value.strip().lower()
    if not text or text == "0":
        return 0.0
    multiplier = 1.0
    if text.endswith("m"):
        multiplier = 1000.0
        text = text[:-1]
    elif text.endswith("k"):
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def picture_encode_args_are_hq(args: list[str]) -> bool:
    """True when ``args`` cannot be a low-fps or mushy-bitrate/CRF picture encode."""
    joined = " ".join(args)
    filter_complex = _flag_value(args, "-filter_complex") or ""
    if "fps=" in filter_complex:
        return False
    rate = _flag_value(args, "-r")
    if rate is not None:
        try:
            if "/" in rate:
                num_s, den_s = rate.split("/", 1)
                parsed = float(num_s) / float(den_s)
            else:
                parsed = float(rate)
        except ValueError:
            parsed = None
        if parsed is None or parsed < MIN_PLAYBACK_FPS:
            return False
    crf = _flag_value(args, "-crf")
    if crf is not None:
        try:
            if int(crf) > HQ_X264_CRF:
                return False
        except ValueError:
            return False
    cq = _flag_value(args, "-cq")
    if cq is not None:
        try:
            if int(cq) > HQ_NVENC_CQ:
                return False
        except ValueError:
            return False
    for flag in ("-b:v", "-b"):
        bitrate = _flag_value(args, flag)
        if bitrate is None:
            continue
        kbps = _parse_kbps(bitrate)
        if kbps is None:
            return False
        if kbps != 0 and kbps < MIN_HQ_BITRATE_KBPS:
            return False
    if "crf 28" in joined or "crf=28" in joined:
        return False
    return True
