from __future__ import annotations

from pathlib import Path

import numpy as np
from moviepy import CompositeVideoClip, ImageClip, VideoFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

from pipeline.config import Settings, require_ffmpeg
from pipeline.media import probe_duration
from pipeline.models import (
    BRollCue,
    EditScript,
    LowerThird,
    MicroEvent,
    OverlayCallout,
    TalkingHeadCut,
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)


class CompositorError(RuntimeError):
    """MoviePy / render failure."""


def render_video(
    video_path: Path,
    script: EditScript,
    output_path: Path,
    settings: Settings,
    *,
    broll_dir: Path | None = None,
) -> Path:
    """Burn lower-thirds and callouts, composite PiP/B-roll, write the final MP4."""
    require_ffmpeg(settings)
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Working video not found: {video_path}")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video_path, settings)

    base = VideoFileClip(str(video_path))
    try:
        a_roll = _apply_talking_head_cuts(base, script.talking_head_cuts)
        layers: list[object] = [a_roll]
        size = (int(a_roll.w), int(a_roll.h))
        timeline_duration = float(a_roll.duration or duration)

        for event in script.collected_punch_ins():
            punch = _punch_in_clip(a_roll, event, timeline_duration)
            if punch is not None:
                layers.append(punch)

        for card in script.collected_lower_thirds():
            overlay = _lower_third_clip(card, size, timeline_duration)
            if overlay is not None:
                layers.append(overlay)

        for callout in script.collected_text_overlays():
            overlay = _callout_clip(callout, size, timeline_duration)
            if overlay is not None:
                layers.append(overlay)

        for cue in script.broll:
            overlay = _broll_clip(cue, a_roll, broll_dir, timeline_duration)
            if overlay is not None:
                layers.append(overlay)

        composed = CompositeVideoClip(layers, size=size)
        composed = composed.with_duration(timeline_duration)
        if a_roll.audio is not None:
            composed = composed.with_audio(a_roll.audio)

        composed.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            fps=base.fps or 30,
            preset="medium",
            threads=0,
            logger=None,
        )
        composed.close()
    finally:
        base.close()

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise CompositorError(f"Render produced no output at {output_path}")
    return output_path


def _apply_talking_head_cuts(
    clip: VideoFileClip, cuts: list[TalkingHeadCut]
) -> VideoFileClip:
    if not cuts:
        return clip
    duration = float(clip.duration or 0)
    pieces = []
    for cut in cuts:
        start = max(0.0, min(cut.start, duration))
        end = max(start, min(cut.end, duration))
        if end - start < 0.04:
            continue
        pieces.append(clip.subclipped(start, end))
    if not pieces:
        return clip
    if len(pieces) == 1:
        return pieces[0]
    return concatenate_videoclips(pieces, method="compose")


def _punch_in_clip(
    clip: VideoFileClip, event: MicroEvent, duration: float
) -> VideoFileClip | None:
    window = _clamp_window(event.start, event.end, duration)
    if window is None:
        return None
    start, end = window
    width, height = int(clip.w), int(clip.h)
    scale = max(1.05, float(event.scale or 1.15))
    piece = clip.subclipped(start, end).resized(scale)
    x1 = max(0, int((piece.w - width) / 2))
    y1 = max(0, int((piece.h - height) / 2))
    piece = piece.cropped(x1=x1, y1=y1, width=width, height=height)
    return piece.with_start(start).with_duration(end - start).without_audio()


def _clamp_window(start: float, end: float, duration: float) -> tuple[float, float] | None:
    start = max(0.0, start)
    end = min(duration, end)
    if end - start < 0.2:
        return None
    return start, end


def _lower_third_clip(
    card: LowerThird, size: tuple[int, int], duration: float
) -> ImageClip | None:
    window = _clamp_window(card.start, card.end, duration)
    if window is None:
        return None
    start, end = window
    image = _draw_lower_third(size, card.title, card.subtitle)
    return (
        ImageClip(image)
        .with_start(start)
        .with_duration(end - start)
        .with_position((0, 0))
    )


def _callout_clip(
    callout: OverlayCallout, size: tuple[int, int], duration: float
) -> ImageClip | None:
    window = _clamp_window(callout.start, callout.end, duration)
    if window is None:
        return None
    start, end = window
    image = _draw_callout(size, callout.text, callout.kind)
    return (
        ImageClip(image)
        .with_start(start)
        .with_duration(end - start)
        .with_position((0, 0))
    )


def _broll_clip(
    cue: BRollCue,
    a_roll: VideoFileClip,
    broll_dir: Path | None,
    duration: float,
) -> VideoFileClip | ImageClip | None:
    window = _clamp_window(cue.start, cue.end, duration)
    if window is None:
        return None
    start, end = window
    hold = end - start
    asset = _resolve_broll_asset(cue, broll_dir)
    if asset is None:
        return None

    insert = VideoFileClip(str(asset))
    src_duration = float(insert.duration or 0)
    if src_duration <= 0:
        insert.close()
        return None
    usable = min(hold, src_duration)
    insert = insert.subclipped(0, usable)

    if cue.transition == "pip":
        target_h = max(80, int(a_roll.h * 0.32))
        insert = insert.resized(height=target_h)
        margin = int(a_roll.w * 0.04)
        insert = insert.with_position((a_roll.w - insert.w - margin, margin))
    else:
        insert = insert.resized(new_size=(int(a_roll.w), int(a_roll.h)))
        insert = insert.with_position((0, 0))

    insert = insert.without_audio()
    insert = insert.with_start(start).with_duration(usable)
    if cue.transition == "fade":
        fade = min(0.25, usable / 3)
        try:
            from moviepy.video import fx as vfx

            insert = insert.with_effects([vfx.CrossFadeIn(fade), vfx.CrossFadeOut(fade)])
        except Exception:
            pass
    return insert


def _resolve_broll_asset(cue: BRollCue, broll_dir: Path | None) -> Path | None:
    if cue.asset_path:
        path = Path(cue.asset_path)
        if path.is_file():
            return path
    if broll_dir is None or not broll_dir.is_dir():
        return None
    tokens = [tok.lower() for tok in cue.query.replace("_", " ").split() if len(tok) > 2]
    videos = [
        item
        for item in broll_dir.iterdir()
        if item.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
    ]
    if not videos:
        return None
    if not tokens:
        return videos[0]
    scored = []
    for item in videos:
        name = item.stem.lower()
        score = sum(1 for tok in tokens if tok in name)
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1] if scored[0][0] > 0 else None


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_lower_third(size: tuple[int, int], title: str, subtitle: str) -> np.ndarray:
    width, height = size
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bar_h = max(72, int(height * 0.14))
    bar_y = int(height * 0.78)
    pad = int(width * 0.04)
    draw.rounded_rectangle(
        [(pad, bar_y), (width - pad, min(height - 16, bar_y + bar_h))],
        radius=12,
        fill=(12, 12, 16, 210),
    )
    accent_w = max(6, int(width * 0.006))
    draw.rectangle(
        [(pad, bar_y), (pad + accent_w, min(height - 16, bar_y + bar_h))],
        fill=(56, 189, 248, 255),
    )
    title_font = _load_font(max(22, int(height * 0.035)))
    sub_font = _load_font(max(16, int(height * 0.022)))
    text_x = pad + accent_w + 18
    draw.text((text_x, bar_y + 12), title[:80], font=title_font, fill=(255, 255, 255, 255))
    if subtitle:
        draw.text(
            (text_x, bar_y + 12 + int(height * 0.042)),
            subtitle[:100],
            font=sub_font,
            fill=(200, 210, 220, 255),
        )
    return np.array(img)


def _draw_callout(size: tuple[int, int], text: str, kind: str) -> np.ndarray:
    width, height = size
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(max(20, int(height * 0.032)))
    label = {"takeaway": "TAKEAWAY", "stat": "STAT", "quote": "QUOTE"}.get(kind, "NOTE")
    max_chars = max(24, int(width / 28))
    wrapped = _wrap(text, max_chars)
    label_font = _load_font(max(14, int(height * 0.018)))
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=6)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    box_w = min(width - 40, text_w + 48)
    box_h = text_h + 56
    x0 = (width - box_w) // 2
    y0 = int(height * 0.12)
    draw.rounded_rectangle(
        [(x0, y0), (x0 + box_w, y0 + box_h)],
        radius=14,
        fill=(15, 23, 42, 215),
    )
    draw.text((x0 + 22, y0 + 10), label, font=label_font, fill=(125, 211, 252, 255))
    draw.multiline_text(
        (x0 + 22, y0 + 30),
        wrapped,
        font=font,
        fill=(248, 250, 252, 255),
        spacing=6,
    )
    return np.array(img)


def _wrap(text: str, width: int) -> str:
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if len(trial) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return "\n".join(lines[:4])
