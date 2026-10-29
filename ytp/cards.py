"""On-screen cards for talking-head episodes.

Boxes are the safe area outside the YouTube end-screen slots at 1920x1080:
video 944,170-1760,629 and subscribe 1242,730-1462,950. Studio's look is
still pending, so a graphics hook draws the card when one is configured.
"""

from __future__ import annotations

from ytp.plan import EditPlan, Segment
from ytp.rules import load_rules

# x1, y1, x2, y2 on a 1920x1080 frame.
_BOXES = {
    "title": (80.0, 36.0, 1760.0, 150.0),
    "lower-third": (80.0, 980.0, 1100.0, 1040.0),
    "end-card": (80.0, 200.0, 880.0, 640.0),
}
CARD_STYLES = tuple(_BOXES)
END_CARD_SECONDS = 7.0


def card_box(style: str, width: int, height: int) -> tuple[float, float, float, float]:
    """Scale a template box to the plan frame. Unknown styles use the lower third."""
    x1, y1, x2, y2 = _BOXES.get(style, _BOXES["lower-third"])
    scale_x = width / 1920.0 if width else 1.0
    scale_y = height / 1080.0 if height else 1.0
    return (x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y)


def append_talking_head_end_card(plan: EditPlan, rules_root=None) -> bool:
    """Hold the end card after the last spoken line when the series asks for one.

    The picture grows so the tail is 7 seconds, inside the 5-10 second rule.
    A tail that is already in range is left alone. Returns True when a segment
    was added.
    """
    if plan.series != "talking-head":
        return False
    if any(segment.id == "end-card" for segment in plan.segments):
        return False
    if not plan.narration:
        return False
    try:
        rules = load_rules(plan.series, rules_root)
    except FileNotFoundError:
        return False
    block = rules.lint.get("end_card") or {}
    if not block:
        return False
    last = max(line.end for line in plan.narration)
    tail = plan.picture_duration() - last
    low = float(block.get("min_seconds", 5))
    high = float(block.get("max_seconds", 10))
    if low <= tail <= high:
        return False
    target = min(high, max(low, END_CARD_SECONDS))
    extra = target - tail
    if extra <= 0.05:
        return False
    image = str(block.get("image") or "assets/end-card/option-C-clean.png")
    plan.segments.append(
        Segment(
            id="end-card",
            kind="still",
            source=image,
            start=plan.picture_duration(),
            duration=extra,
            template="end-card",
        )
    )
    return True
