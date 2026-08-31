from __future__ import annotations

from enum import Enum

PIP_MARGIN = 0.04
PIP_ASPECT = 9.0 / 16.0
DARK_RGB = (11, 16, 22)
BORDER_COLOR = (232, 241, 248, 230)


class LayoutKind(str, Enum):
    """How the talking-head webcam sits on the 1920x1080 canvas for one scene.

    FULL_FRAME: webcam fills the frame (direct address).
    PIP_BOTTOM_RIGHT: generated slide fills the frame; webcam is a rounded
    corner bubble (about 25% of frame width) in the lower right.
    SPLIT_TOP: webcam occupies the top two-thirds; the graphic occupies the
    bottom third (title, bullets, or a detail card).
    """

    FULL_FRAME = "FULL_FRAME"
    PIP_BOTTOM_RIGHT = "PIP_BOTTOM_RIGHT"
    SPLIT_TOP = "SPLIT_TOP"


def cover_scale(src_w: int, src_h: int, dest_w: int, dest_h: int, zoom: float = 1.0) -> float:
    if src_w <= 0 or src_h <= 0:
        return 1.0
    return max(dest_w / src_w, dest_h / src_h) * max(zoom, 1.0)


def pip_rect(
    width: int,
    height: int,
    scale: float,
    margin_ratio: float = PIP_MARGIN,
) -> tuple[int, int, int, int]:
    """Lower-right 16:9 bubble: x, y, w, h."""
    box_w = max(80, int(width * scale))
    box_h = max(45, int(box_w * PIP_ASPECT))
    margin_x = int(width * margin_ratio)
    margin_y = int(height * margin_ratio)
    x = max(0, width - box_w - margin_x)
    y = max(0, height - box_h - margin_y)
    return x, y, box_w, box_h


def split_webcam_rect(
    width: int, height: int, top_ratio: float
) -> tuple[int, int, int, int]:
    """Top band for the webcam on SPLIT_TOP: x, y, w, h."""
    box_h = max(1, int(round(height * top_ratio)))
    return 0, 0, width, min(box_h, height)
