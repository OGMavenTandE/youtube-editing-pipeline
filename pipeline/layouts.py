from __future__ import annotations

from enum import Enum


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
