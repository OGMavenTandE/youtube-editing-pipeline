"""Probe hardware decode and CUDA filters. Do not assume Gyan.FFmpeg features.

``ffmpeg -decoders`` listing ``h264_cuvid`` is not enough. Smoke-test against
the talking-head file. Cache the result for the process.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import Settings, require_ffmpeg
from pipeline.hidden_process import run_hidden

logger = logging.getLogger(__name__)

# Order: NVIDIA CUVID, then Windows D3D11, then generic CUDA hwaccel.
_DECODE_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("h264_cuvid", ("-c:v", "h264_cuvid")),
    ("d3d11va", ("-hwaccel", "d3d11va")),
    ("cuda", ("-hwaccel", "cuda")),
)


@dataclass(frozen=True)
class HwDecode:
    """Input-side hardware decode that actually produced a frame."""

    name: str
    input_args: tuple[str, ...]

    @property
    def cuda_frames(self) -> bool:
        return self.name in {"h264_cuvid", "cuda"}


_decode_choice: HwDecode | None | str = "unset"
_gpu_filters: bool | None = None
_lock = threading.Lock()


def reset_hwaccel_cache() -> None:
    """Test helper. Clears decode and GPU-filter probes."""
    global _decode_choice, _gpu_filters
    with _lock:
        _decode_choice = "unset"
        _gpu_filters = None


def select_hw_decode(
    settings: Settings | None,
    sample: Path,
    *,
    enabled: bool,
) -> HwDecode | None:
    """Return a working hw decode for ``sample``, or None.

    ``enabled`` is False when NVENC is not selected. Probe is skipped then.
    """
    global _decode_choice
    if not enabled:
        return None
    with _lock:
        if _decode_choice != "unset":
            return _decode_choice if isinstance(_decode_choice, HwDecode) else None
        choice = _probe_hw_decode(settings, sample)
        _decode_choice = choice if choice is not None else None
        if choice is None:
            logger.info("hwdecode=none")
        else:
            logger.info("hwdecode=%s", choice.name)
        return choice


def gpu_filters_available(settings: Settings | None = None) -> bool:
    """True when scale_cuda + hwdownload works on a lavfi frame."""
    global _gpu_filters
    with _lock:
        if _gpu_filters is not None:
            return _gpu_filters
        _gpu_filters = _probe_gpu_filters(settings)
        logger.info("gpu_filters=%s", _gpu_filters)
        return _gpu_filters


def _probe_hw_decode(settings: Settings | None, sample: Path) -> HwDecode | None:
    if not sample.is_file():
        return None
    try:
        ffmpeg_bin = require_ffmpeg(settings)
    except Exception:
        return None
    for name, args in _DECODE_CANDIDATES:
        if _smoke_decode(ffmpeg_bin, sample, args):
            return HwDecode(name, args)
    return None


def _smoke_decode(ffmpeg_bin: str, sample: Path, args: tuple[str, ...]) -> bool:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        "-i",
        str(sample),
        "-frames:v",
        "1",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = run_hidden(cmd, capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0


def _probe_gpu_filters(settings: Settings | None) -> bool:
    try:
        ffmpeg_bin = require_ffmpeg(settings)
    except Exception:
        return False
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=0.04",
        "-filter_complex",
        "[0:v]format=nv12,hwupload_cuda,scale_cuda=64:64,hwdownload,format=nv12",
        "-frames:v",
        "1",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = run_hidden(cmd, capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0
