"""Locked Scott Mastin picture-kit geometry. Not a style playground."""

from __future__ import annotations

from enum import Enum

# Two colors only.
WHITE = (255, 255, 255)
GOLD = (224, 180, 74)  # #E0B44A
DARK_RGB = (8, 10, 14)
DARK_ALPHA = 200  # ~188–210
GOLD_RGBA = (*GOLD, 255)
WHITE_RGBA = (*WHITE, 255)
DARK_PLATE = (*DARK_RGB, DARK_ALPHA)

# Overlay plate (Nate card). 1920x1080.
OVERLAY_X = 56
OVERLAY_Y = 48
OVERLAY_W = 620
OVERLAY_RADIUS = 22
OVERLAY_PAD = 32
OVERLAY_ICON = 72

# PiP host window. Entire 16:9 talking-head frame, scaled, never a face crop.
PIP_W = 560
PIP_H = 315
PIP_MARGIN = 40
PIP_RADIUS = 16
PIP_BORDER = 3

# Bookend lower third. Taller identity + find-me bar.
LT_MARGIN_X = 48
LT_MARGIN_Y = 36
LT_HEIGHT = 220
LT_RADIUS = 22
LT_RULE = 5

CANVAS_W = 1920
CANVAS_H = 1080

# Model may only emit these three.
BODY_TAGS = ("overlay", "pip", "nothing")


class PictureTag(str, Enum):
    """Chrome on one beat. lower_third is app-forced at open/close only."""

    OVERLAY = "overlay"
    PIP = "pip"
    NOTHING = "nothing"
    LOWER_THIRD = "lower_third"

    @classmethod
    def coerce(cls, value: object, *, allow_bookend: bool = True) -> "PictureTag":
        if isinstance(value, cls):
            tag = value
        else:
            raw = str(value or "").strip()
            aliases = {
                "overlay": cls.OVERLAY,
                "pip": cls.PIP,
                "nothing": cls.NOTHING,
                "lower_third": cls.LOWER_THIRD,
                "FULL_FRAME": cls.NOTHING,
                "full_frame": cls.NOTHING,
                "PIP_BOTTOM_RIGHT": cls.PIP,
                "pip_bottom_right": cls.PIP,
                "SPLIT_TOP": cls.OVERLAY,
                "split_top": cls.OVERLAY,
            }
            tag = aliases.get(raw) or aliases.get(raw.lower()) or cls.NOTHING
        if tag is cls.LOWER_THIRD and not allow_bookend:
            return cls.NOTHING
        return tag


# Older imports still resolve. Values are the locked tags, not FULL/PIP/SPLIT.
LayoutKind = PictureTag


def cover_scale(src_w: int, src_h: int, dest_w: int, dest_h: int, zoom: float = 1.0) -> float:
    """Scale-to-cover. Full-frame host only. Do not use this for the PiP window."""
    if src_w <= 0 or src_h <= 0:
        return 1.0
    return max(dest_w / src_w, dest_h / src_h) * max(zoom, 1.0)


def contain_scale(src_w: int, src_h: int, dest_w: int, dest_h: int) -> float:
    """Scale-to-fit the entire frame. PiP host uses this so the face is not cropped."""
    if src_w <= 0 or src_h <= 0:
        return 1.0
    return min(dest_w / src_w, dest_h / src_h)


def pip_rect(
    width: int = CANVAS_W,
    height: int = CANVAS_H,
    scale: float | None = None,
    margin_ratio: float | None = None,
) -> tuple[int, int, int, int]:
    """Lower-right 560x315 window, margin 40, on a 1920x1080 canvas.

    ``scale`` and ``margin_ratio`` are ignored. The kit is locked.
    Smaller canvases (tests) scale the locked rect proportionally.
    """
    del scale, margin_ratio
    sx = width / CANVAS_W
    sy = height / CANVAS_H
    box_w = max(2, int(round(PIP_W * sx)))
    box_h = max(2, int(round(PIP_H * sy)))
    margin_x = max(1, int(round(PIP_MARGIN * sx)))
    margin_y = max(1, int(round(PIP_MARGIN * sy)))
    x = max(0, width - box_w - margin_x)
    y = max(0, height - box_h - margin_y)
    return x, y, box_w, box_h


def overlay_rect(width: int = CANVAS_W, height: int = CANVAS_H) -> tuple[int, int, int, int]:
    """Top-left plate origin and width. Height is content-driven."""
    sx = width / CANVAS_W
    sy = height / CANVAS_H
    return (
        int(round(OVERLAY_X * sx)),
        int(round(OVERLAY_Y * sy)),
        int(round(OVERLAY_W * sx)),
        0,
    )


def lower_third_rect(width: int = CANVAS_W, height: int = CANVAS_H) -> tuple[int, int, int, int]:
    sx = width / CANVAS_W
    sy = height / CANVAS_H
    margin_x = int(round(LT_MARGIN_X * sx))
    margin_y = int(round(LT_MARGIN_Y * sy))
    bar_h = int(round(LT_HEIGHT * sy))
    return margin_x, height - margin_y - bar_h, width - (2 * margin_x), bar_h


def split_webcam_rect(
    width: int, height: int, top_ratio: float
) -> tuple[int, int, int, int]:
    """Retired SPLIT_TOP geometry. Kept so leftover imports do not explode."""
    box_h = max(1, int(round(height * top_ratio)))
    return 0, 0, width, min(box_h, height)
