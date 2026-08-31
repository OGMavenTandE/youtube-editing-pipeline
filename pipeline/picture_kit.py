"""Locked picture-kit renderer. Inter + white/gold + dark plate. Nothing else."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pipeline.layouts import (
    CANVAS_H,
    CANVAS_W,
    DARK_PLATE,
    GOLD,
    GOLD_RGBA,
    LT_RADIUS,
    LT_RULE,
    OVERLAY_ICON,
    OVERLAY_PAD,
    OVERLAY_RADIUS,
    OVERLAY_W,
    WHITE,
    WHITE_RGBA,
    lower_third_rect,
    overlay_rect,
    pip_rect,
)

if TYPE_CHECKING:
    from pipeline.models import HostIdentity, TalkSheet

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
_REGULAR = FONTS_DIR / "Inter-Regular.ttf"
_BOLD = FONTS_DIR / "Inter-Bold.ttf"

_SYSTEM_INTER = (
    "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
    "/usr/share/fonts/truetype/inter/Inter_18pt-Regular.ttf",
    "C:\\Windows\\Fonts\\Inter-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Inter-Regular.ttf",
)
_SYSTEM_INTER_BOLD = (
    "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
    "/usr/share/fonts/truetype/inter/Inter_18pt-Bold.ttf",
    "C:\\Windows\\Fonts\\Inter-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Inter-Bold.ttf",
)

ICON_NAMES = (
    "bar_chart",
    "robot",
    "shield",
    "drone",
    "share",
    "chip",
    "lock",
    "target",
)


@dataclass(frozen=True)
class KitScale:
    width: int
    height: int

    @property
    def sx(self) -> float:
        return self.width / CANVAS_W

    @property
    def sy(self) -> float:
        return self.height / CANVAS_H

    @property
    def s(self) -> float:
        return min(self.sx, self.sy)

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.s)))

    def xx(self, value: float) -> int:
        return int(round(value * self.sx))

    def yy(self, value: float) -> int:
        return int(round(value * self.sy))


def load_inter(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load bundled Inter. Known-path fallback, then PIL default (tests still run)."""
    size = max(8, int(size))
    candidates = [_BOLD if bold else _REGULAR]
    candidates.extend(Path(p) for p in (_SYSTEM_INTER_BOLD if bold else _SYSTEM_INTER))
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _blank(size: tuple[int, int]) -> Image.Image:
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _wrap_px(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int = 2,
) -> list[str]:
    words = (text or "").replace("\n", " \n ").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    draw = ImageDraw.Draw(_blank((4, 4)))
    for word in words:
        if word == "\n":
            lines.append(current)
            current = ""
            if len(lines) >= max_lines:
                break
            continue
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    elif current and lines:
        trial = (lines[-1] + " " + current).strip()
        if draw.textlength(trial, font=font) <= max_width:
            lines[-1] = trial
        else:
            lines[-1] = lines[-1]
    return lines[:max_lines] or [""]


def _fit_headline(
    text: str,
    scale: KitScale,
    max_width: int,
    *,
    max_lines: int = 2,
    start: int = 40,
    floor: int = 26,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    text = (text or "").strip()
    if not text:
        font = load_inter(scale.px(floor), bold=True)
        return font, [], scale.px(floor)
    for size in range(start, floor - 1, -2):
        font = load_inter(scale.px(size), bold=True)
        lines = _wrap_px(text, font, max_width, max_lines=max_lines)
        draw = ImageDraw.Draw(_blank((4, 4)))
        if all(draw.textlength(line, font=font) <= max_width + 1 for line in lines if line):
            return font, [ln for ln in lines if ln], scale.px(size)
    font = load_inter(scale.px(floor), bold=True)
    return font, [ln for ln in _wrap_px(text, font, max_width, max_lines=max_lines) if ln], scale.px(floor)


def _fit_lines(
    text: str,
    scale: KitScale,
    max_width: int,
    *,
    max_lines: int = 2,
    start: int = 16,
    floor: int = 12,
    bold: bool = True,
) -> list[tuple[ImageFont.ImageFont, str, int]]:
    """Wrap first. Shrink a leftover long word rather than clip it."""
    text = (text or "").strip()
    if not text:
        return []
    draw = ImageDraw.Draw(_blank((4, 4)))
    chosen_font = load_inter(scale.px(floor), bold=bold)
    chosen_lines = _wrap_px(text, chosen_font, max_width, max_lines=max_lines)
    chosen_size = scale.px(floor)
    for size in range(start, floor - 1, -1):
        font = load_inter(scale.px(size), bold=bold)
        lines = _wrap_px(text, font, max_width, max_lines=max_lines)
        if all(draw.textlength(line, font=font) <= max_width for line in lines if line):
            chosen_font, chosen_lines, chosen_size = font, lines, scale.px(size)
            break
    fitted: list[tuple[ImageFont.ImageFont, str, int]] = []
    for line in chosen_lines:
        if not line:
            continue
        if draw.textlength(line, font=chosen_font) <= max_width:
            fitted.append((chosen_font, line, chosen_size))
            continue
        line_font = load_inter(scale.px(12), bold=bold)
        line_size = scale.px(12)
        for size in range(max(int(round(chosen_size / max(scale.s, 1e-6))), 12), 11, -1):
            font = load_inter(scale.px(size), bold=bold)
            if draw.textlength(line, font=font) <= max_width + 1:
                line_font, line_size = font, scale.px(size)
                break
        fitted.append((line_font, line, line_size))
    return fitted


def draw_icon(name: str, size: int) -> Image.Image:
    """Gold line-art in a square. No extra colors."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    stroke = max(2, size // 18)
    inset = max(2, size // 12)
    box = (inset, inset, size - inset - 1, size - inset - 1)
    draw.rounded_rectangle(box, radius=max(3, size // 10), outline=GOLD_RGBA, width=stroke)
    inner = inset + stroke + max(2, size // 16)
    x0, y0, x1, y1 = inner, inner, size - inner - 1, size - inner - 1
    w, h = x1 - x0, y1 - y0
    key = (name or "bar_chart").strip().lower().replace("-", "_")
    if key not in ICON_NAMES:
        key = "bar_chart"

    def line(a: tuple[int, int], b: tuple[int, int]) -> None:
        draw.line((a, b), fill=GOLD_RGBA, width=stroke)

    if key == "bar_chart":
        gap = max(2, w // 8)
        bw = max(2, (w - 2 * gap) // 3)
        heights = (0.45, 0.75, 1.0)
        for i, frac in enumerate(heights):
            bx = x0 + i * (bw + gap)
            by = y1 - int(h * frac)
            draw.rectangle((bx, by, bx + bw, y1), outline=GOLD_RGBA, width=stroke)
    elif key == "robot":
        draw.rounded_rectangle((x0, y0 + h // 6, x1, y1), radius=size // 12, outline=GOLD_RGBA, width=stroke)
        eye_y = y0 + h // 2
        er = max(1, stroke)
        draw.ellipse((x0 + w // 4 - er, eye_y - er, x0 + w // 4 + er, eye_y + er), fill=GOLD_RGBA)
        draw.ellipse((x1 - w // 4 - er, eye_y - er, x1 - w // 4 + er, eye_y + er), fill=GOLD_RGBA)
        line(((x0 + x1) // 2, y0), ((x0 + x1) // 2, y0 + h // 6))
    elif key == "shield":
        mid = (x0 + x1) // 2
        draw.polygon(
            [(mid, y0), (x1, y0 + h // 4), (x1 - w // 8, y1 - h // 6), (mid, y1), (x0 + w // 8, y1 - h // 6), (x0, y0 + h // 4)],
            outline=GOLD_RGBA,
        )
    elif key == "drone":
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        arm = max(4, w // 3)
        line((cx - arm, cy - arm), (cx + arm, cy + arm))
        line((cx + arm, cy - arm), (cx - arm, cy + arm))
        r = max(2, stroke + 1)
        for px, py in ((cx - arm, cy - arm), (cx + arm, cy - arm), (cx - arm, cy + arm), (cx + arm, cy + arm)):
            draw.ellipse((px - r, py - r, px + r, py + r), outline=GOLD_RGBA, width=stroke)
        draw.rectangle((cx - r, cy - r, cx + r, cy + r), outline=GOLD_RGBA, width=stroke)
    elif key == "share":
        draw.rectangle((x0, y0 + h // 3, x1 - w // 4, y1), outline=GOLD_RGBA, width=stroke)
        line((x1 - w // 3, y0 + h // 6), (x1, y0))
        line((x1, y0), (x1, y0 + h // 3))
        line((x1, y0), (x1 - w // 3, y0))
    elif key == "chip":
        draw.rectangle((x0 + w // 6, y0 + h // 6, x1 - w // 6, y1 - h // 6), outline=GOLD_RGBA, width=stroke)
        for i in range(3):
            t = y0 + h // 4 + i * (h // 5)
            line((x0, t), (x0 + w // 6, t))
            line((x1 - w // 6, t), (x1, t))
    elif key == "lock":
        body_top = y0 + h // 3
        draw.rounded_rectangle((x0 + w // 8, body_top, x1 - w // 8, y1), radius=2, outline=GOLD_RGBA, width=stroke)
        draw.arc((x0 + w // 4, y0, x1 - w // 4, body_top + h // 6), 200, 340, fill=GOLD_RGBA, width=stroke)
    elif key == "target":
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        for frac in (0.95, 0.55):
            r = int(min(w, h) * frac / 2)
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=GOLD_RGBA, width=stroke)
        draw.ellipse((cx - 1, cy - 1, cx + 1, cy + 1), fill=GOLD_RGBA)
    return img


def render_overlay(
    size: tuple[int, int],
    *,
    kicker: str,
    headline: str,
    icon: str = "bar_chart",
    max_headline_lines: int = 2,
) -> np.ndarray:
    """Top-left Nate plate. Host stays full-frame underneath."""
    width, height = size
    scale = KitScale(width, height)
    img = _blank(size)
    draw = ImageDraw.Draw(img)
    x0, y0, plate_w, _ = overlay_rect(width, height)
    pad = scale.px(OVERLAY_PAD)
    show_icon = bool((icon or "").strip())
    icon_s = scale.px(OVERLAY_ICON) if show_icon else 0
    inner_w = plate_w - 2 * pad
    pip_x, pip_y, _, _ = pip_rect(width, height)
    max_plate_bottom = pip_y - scale.px(24)
    max_plate_right = min(x0 + plate_w, pip_x - scale.px(24), width - scale.px(24))
    plate_w = max(scale.px(160), max_plate_right - x0)
    inner_w = plate_w - 2 * pad

    kicker_text = (kicker or "").strip().upper()
    kicker_lines = _fit_lines(kicker_text, scale, inner_w, max_lines=2, start=16, floor=12)
    head_limit = max(1, int(max_headline_lines or 2))
    head_font, head_lines, head_size = _fit_headline(
        (headline or "").strip(),
        scale,
        inner_w,
        max_lines=head_limit,
    )
    line_gap = max(4, scale.px(8))
    kicker_gap = scale.px(10) if kicker_lines and head_lines else 0
    kicker_h = sum(size + scale.px(4) for _, _, size in kicker_lines)
    head_h = len(head_lines) * (head_size + line_gap)
    icon_block = (scale.px(18) + icon_s) if show_icon else 0
    plate_h = pad + kicker_h + kicker_gap + head_h + icon_block + pad
    if y0 + plate_h > max_plate_bottom:
        plate_h = max(pad * 2 + scale.px(40), max_plate_bottom - y0)

    draw.rounded_rectangle(
        (x0, y0, x0 + plate_w, y0 + plate_h),
        radius=scale.px(OVERLAY_RADIUS),
        fill=DARK_PLATE,
    )
    tx, ty = x0 + pad, y0 + pad
    for font, line, size in kicker_lines:
        draw.text((tx, ty), line, font=font, fill=GOLD_RGBA)
        ty += size + scale.px(4)
    if kicker_lines and head_lines:
        ty += kicker_gap
    for line in head_lines:
        if ty + head_size > y0 + plate_h - pad - (icon_s if show_icon else 0):
            break
        draw.text((tx, ty), line, font=head_font, fill=WHITE_RGBA)
        ty += head_size + line_gap
    if show_icon:
        icon_img = draw_icon(icon, icon_s)
        img.alpha_composite(icon_img, (x0 + pad, y0 + plate_h - pad - icon_s))
    return np.array(img)


def render_lower_third(size: tuple[int, int], identity: HostIdentity) -> np.ndarray:
    """Two-column identity + FIND ME bar. No WRAP kicker."""
    width, height = size
    scale = KitScale(width, height)
    img = _blank(size)
    draw = ImageDraw.Draw(img)
    x0, y0, bar_w, bar_h = lower_third_rect(width, height)
    draw.rounded_rectangle(
        (x0, y0, x0 + bar_w, y0 + bar_h),
        radius=scale.px(LT_RADIUS),
        fill=DARK_PLATE,
    )
    rule = max(4, scale.px(LT_RULE))
    draw.rectangle((x0, y0, x0 + bar_w, y0 + rule), fill=GOLD_RGBA)

    pad = scale.px(32)
    col_gap = scale.px(48)
    left_w = int(bar_w * 0.62) - pad
    right_x = x0 + int(bar_w * 0.64) + col_gap // 4
    text_x = x0 + pad
    text_y = y0 + rule + scale.px(18)

    name_font = load_inter(scale.px(40), bold=True)
    title_font = load_inter(scale.px(18), bold=False)
    aff_font = load_inter(scale.px(17), bold=False)
    mission_font = load_inter(scale.px(16), bold=False)

    draw.text((text_x, text_y), identity.name, font=name_font, fill=WHITE_RGBA)
    text_y += scale.px(48)
    muted = (255, 255, 255, 210)
    if identity.title_line:
        draw.text((text_x, text_y), identity.title_line, font=title_font, fill=muted)
        text_y += scale.px(26)
    if identity.affiliations:
        draw.text((text_x, text_y), identity.affiliations, font=aff_font, fill=GOLD_RGBA)
        text_y += scale.px(26)
    if identity.mission:
        draw.text((text_x, text_y), identity.mission, font=mission_font, fill=muted)

    find_font = load_inter(scale.px(14), bold=True)
    link_font = load_inter(scale.px(20), bold=False)
    fy = y0 + rule + scale.px(22)
    draw.text((right_x, fy), identity.find_me_kicker.strip().upper() or "FIND ME", font=find_font, fill=GOLD_RGBA)
    fy += scale.px(28)
    for link in identity.find_me:
        cleaned = link.strip()
        if not cleaned:
            continue
        draw.text((right_x, fy), cleaned, font=link_font, fill=WHITE_RGBA)
        fy += scale.px(30)
    del left_w
    return np.array(img)


def render_bookend(
    size: tuple[int, int],
    *,
    identity: HostIdentity,
    kicker: str,
    headline: str,
    icon: str,
) -> np.ndarray:
    """Open/close: overlay card + identity lower third on one transparent canvas."""
    overlay = Image.fromarray(render_overlay(size, kicker=kicker, headline=headline, icon=icon))
    bar = Image.fromarray(render_lower_third(size, identity))
    overlay.alpha_composite(bar)
    return np.array(overlay)


def render_pip_type(
    size: tuple[int, int],
    *,
    kicker: str,
    sub: str,
    quote: str = "",
) -> np.ndarray:
    """Same Nate plate as overlay cards. One field, one role. Never split sub on period."""
    headline_parts = [part for part in ((sub or "").strip(), (quote or "").strip()) if part]
    headline = "\n".join(headline_parts)
    return render_overlay(
        size,
        kicker=(kicker or "").strip(),
        headline=headline,
        icon="bar_chart",
        max_headline_lines=5,
    )


def default_identity() -> HostIdentity:
    from pipeline.models import HostIdentity

    return HostIdentity()


def default_talk_sheet() -> TalkSheet:
    from pipeline.models import TalkSheet

    return TalkSheet()


def render_kit_fixtures(dest: Path, *, identity: HostIdentity | None = None) -> dict[str, Path]:
    """Still of each chrome so CI and the PR can prove the look."""
    dest.mkdir(parents=True, exist_ok=True)
    identity = identity or default_identity()
    canvas = (CANVAS_W, CANVAS_H)
    host = _proof_host(canvas)
    files: dict[str, Path] = {}

    overlay = render_overlay(
        canvas,
        kicker="THE MONEY",
        headline="$1.5B in Procurements. That's the Floor.",
        icon="bar_chart",
    )
    files["overlay"] = _composite_proof(host, overlay, dest / "overlay.png")

    still = _proof_still(canvas)
    pip_type = render_pip_type(
        canvas,
        kicker="$1.5B",
        sub="in procurements",
        quote="I think that's even low.",
    )
    files["pip"] = _composite_pip_proof(still, host, pip_type, dest / "pip.png")

    open_card = render_bookend(
        canvas,
        identity=identity,
        kicker="SKYNET IS COMING · PART 2",
        headline="$1.5B is the floor.\nNot the program.",
        icon="bar_chart",
    )
    files["bookend_open"] = _composite_proof(host, open_card, dest / "bookend_open.png")

    close_card = render_bookend(
        canvas,
        identity=identity,
        kicker="WORK WITH ME",
        headline="Independent AI T&E.\nVendor-agnostic.",
        icon="share",
    )
    files["bookend_close"] = _composite_proof(host, close_card, dest / "bookend_close.png")

    nothing = dest / "nothing.png"
    Image.fromarray(host).convert("RGB").save(nothing)
    files["nothing"] = nothing
    manifest = dest / "manifest.json"
    manifest.write_text(
        json.dumps({name: path.name for name, path in files.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    return files


def _proof_host(size: tuple[int, int]) -> np.ndarray:
    img = Image.new("RGB", size, (42, 48, 56))
    draw = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, int(size[1] * 0.46)
    draw.ellipse((cx - 220, cy - 260, cx + 220, cy + 280), fill=(118, 122, 128))
    draw.ellipse((cx - 110, cy - 160, cx + 110, cy + 40), fill=(196, 168, 140))
    return np.array(img.convert("RGBA"))


def _proof_still(size: tuple[int, int]) -> np.ndarray:
    img = Image.new("RGB", size, (48, 62, 48))
    draw = ImageDraw.Draw(img)
    draw.rectangle((80, 200, 900, 880), fill=(70, 82, 64))
    draw.rectangle((1100, 160, 1700, 720), fill=(58, 70, 54))
    draw.rectangle((200, 520, 620, 820), fill=(30, 34, 32))
    return np.array(img.convert("RGBA"))


def _composite_proof(host: np.ndarray, chrome: np.ndarray, dest: Path) -> Path:
    base = Image.fromarray(host).convert("RGBA")
    base.alpha_composite(Image.fromarray(chrome))
    dest.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(dest)
    return dest


def _composite_pip_proof(still: np.ndarray, host: np.ndarray, chrome: np.ndarray, dest: Path) -> Path:
    from pipeline.layouts import PIP_BORDER, PIP_RADIUS, pip_rect

    width, height = still.shape[1], still.shape[0]
    base = Image.fromarray(still).convert("RGBA")
    x, y, box_w, box_h = pip_rect(width, height)
    cam = Image.fromarray(host).convert("RGBA").resize((box_w, box_h), Image.Resampling.LANCZOS)
    mask = Image.new("L", (box_w, box_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=PIP_RADIUS, fill=255)
    cam.putalpha(mask)
    base.paste(cam, (x, y), cam)
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle(
        (x, y, x + box_w - 1, y + box_h - 1),
        radius=PIP_RADIUS,
        outline=GOLD_RGBA,
        width=PIP_BORDER,
    )
    base.alpha_composite(Image.fromarray(chrome))
    dest.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(dest)
    return dest


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render locked picture-kit fixture stills.")
    parser.add_argument("--out", default="work/picture_kit", help="Directory for PNG fixtures.")
    args = parser.parse_args(argv)
    files = render_kit_fixtures(Path(args.out))
    for name, path in files.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
