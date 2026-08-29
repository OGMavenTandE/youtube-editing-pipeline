from __future__ import annotations

from pathlib import Path

import numpy as np
from moviepy import ColorClip, CompositeVideoClip, ImageClip, VideoFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

from pipeline.config import Settings, require_ffmpeg
from pipeline.layouts import LayoutKind
from pipeline.media import probe_duration
from pipeline.models import (
    EditScript,
    LowerThird,
    MicroEvent,
    OverlayCallout,
    Scene,
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)

_DARK = (11, 16, 22)
_PIP_MARGIN = 0.04
_PIP_ASPECT = 9.0 / 16.0
_BORDER_COLOR = (232, 241, 248, 230)
_ACCENT = (56, 189, 248)


class CompositorError(RuntimeError):
    """MoviePy / render failure."""


def canvas_size(settings: Settings) -> tuple[int, int]:
    return int(settings.output_width), int(settings.output_height)


def cover_scale(src_w: int, src_h: int, dest_w: int, dest_h: int, zoom: float = 1.0) -> float:
    if src_w <= 0 or src_h <= 0:
        return 1.0
    return max(dest_w / src_w, dest_h / src_h) * max(zoom, 1.0)


def pip_rect(
    width: int,
    height: int,
    scale: float,
    margin_ratio: float = _PIP_MARGIN,
) -> tuple[int, int, int, int]:
    """Lower-right 16:9 bubble: x, y, w, h."""
    box_w = max(80, int(width * scale))
    box_h = max(45, int(box_w * _PIP_ASPECT))
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


def render_video(
    video_path: Path,
    script: EditScript,
    output_path: Path,
    settings: Settings,
    *,
    broll_dir: Path | None = None,
) -> Path:
    """Composite each scene onto a 1920x1080 canvas and write the final MP4."""
    del broll_dir
    require_ffmpeg(settings)
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Working video not found: {video_path}")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video_path, settings)
    canvas = canvas_size(settings)

    base = VideoFileClip(str(video_path))
    try:
        timeline = float(base.duration or duration)
        scenes = _scenes_or_full(script, timeline)
        stills: dict[str, np.ndarray] = {}
        pieces = [
            _compose_scene(base, scene, settings, canvas, stills) for scene in scenes
        ]
        pieces = [piece for piece in pieces if piece is not None]
        if not pieces:
            raise CompositorError("No scenes to composite")
        composed = concatenate_videoclips(pieces, method="chain")
        composed = composed.with_duration(sum(float(piece.duration or 0) for piece in pieces))

        overlay_layers: list[object] = [composed]
        for overlay in _lower_third_overlays(script, canvas, timeline):
            overlay_layers.append(overlay)
        for callout in script.collected_text_overlays():
            layer = _callout_clip(callout, canvas, timeline)
            if layer is not None:
                overlay_layers.append(layer)

        final = CompositeVideoClip(overlay_layers, size=canvas)
        final = final.with_duration(composed.duration)
        if base.audio is not None:
            final = final.with_audio(base.audio)

        final.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            fps=base.fps or 30,
            preset="medium",
            threads=0,
            logger=None,
        )
        final.close()
        composed.close()
        for piece in pieces:
            piece.close()
    finally:
        base.close()

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise CompositorError(f"Render produced no output at {output_path}")
    return output_path


def _scenes_or_full(script: EditScript, duration: float) -> list[Scene]:
    if script.scenes:
        return script.scenes
    return [Scene(start=0.0, end=duration, layout=LayoutKind.FULL_FRAME)]


def _compose_scene(
    a_roll: VideoFileClip,
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    stills: dict[str, np.ndarray],
) -> CompositeVideoClip | None:
    duration = float(a_roll.duration or 0)
    start = max(0.0, min(scene.start, duration))
    end = max(start, min(scene.end, duration))
    hold = end - start
    if hold < 0.04:
        return None
    webcam = a_roll.subclipped(start, end).without_audio()
    width, height = canvas
    bg = ColorClip(size=canvas, color=_DARK).with_duration(hold)
    layers: list[object] = [bg]

    if scene.layout is LayoutKind.PIP_BOTTOM_RIGHT:
        layers.extend(
            _pip_layers(webcam, scene, settings, canvas, hold, start, stills)
        )
    elif scene.layout is LayoutKind.SPLIT_TOP:
        layers.extend(
            _split_layers(webcam, scene, settings, canvas, hold, start, stills)
        )
    else:
        layers.extend(_full_frame_layers(webcam, scene, canvas, hold, start))

    return CompositeVideoClip(layers, size=canvas).with_duration(hold)


def _full_frame_layers(
    webcam: VideoFileClip,
    scene: Scene,
    canvas: tuple[int, int],
    hold: float,
    scene_start: float,
) -> list[object]:
    width, height = canvas
    layers = [_cover(webcam, width, height).with_duration(hold)]
    layers.extend(
        _punch_layers(webcam, scene, scene_start, hold, dest=(0, 0, width, height))
    )
    return layers


def _pip_layers(
    webcam: VideoFileClip,
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    hold: float,
    scene_start: float,
    stills: dict[str, np.ndarray],
) -> list[object]:
    width, height = canvas
    x, y, box_w, box_h = pip_rect(width, height, settings.pip_scale)
    layers: list[object] = []
    slide = _still_clip(scene.graphic.asset_path, canvas, hold, stills)
    if slide is not None:
        layers.append(slide)
    radius = max(16, box_h // 6)
    border = _rounded_border_clip(box_w, box_h, radius).with_duration(hold).with_position((x, y))
    layers.append(border)
    cam = _rounded_webcam(webcam, box_w, box_h, radius, zoom=1.0).with_duration(hold)
    layers.append(cam.with_position((x, y)))
    for punch in _punch_layers(
        webcam, scene, scene_start, hold, dest=(x, y, box_w, box_h), radius=radius
    ):
        layers.append(punch)
    return layers


def _split_layers(
    webcam: VideoFileClip,
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    hold: float,
    scene_start: float,
    stills: dict[str, np.ndarray],
) -> list[object]:
    width, height = canvas
    x, y, box_w, box_h = split_webcam_rect(width, height, settings.split_top_ratio)
    layers: list[object] = [
        _cover(webcam, box_w, box_h).with_duration(hold).with_position((x, y))
    ]
    layers.extend(
        _punch_layers(webcam, scene, scene_start, hold, dest=(x, y, box_w, box_h))
    )
    slide = _still_clip(scene.graphic.asset_path, canvas, hold, stills)
    if slide is not None:
        layers.append(slide)
    return layers


def _punch_layers(
    webcam: VideoFileClip,
    scene: Scene,
    scene_start: float,
    hold: float,
    dest: tuple[int, int, int, int],
    radius: int | None = None,
) -> list[object]:
    x, y, box_w, box_h = dest
    layers: list[object] = []
    for event in scene.micro_events:
        if event.kind != "punch_in":
            continue
        window = _relative_window(event, scene_start, hold)
        if window is None:
            continue
        rel_start, rel_end = window
        zoom = max(1.05, float(event.scale or 1.15))
        src = webcam.subclipped(rel_start, rel_end)
        if radius is not None:
            piece = _rounded_webcam(src, box_w, box_h, radius, zoom=zoom)
        else:
            piece = _cover(src, box_w, box_h, zoom=zoom)
        layers.append(
            piece.with_start(rel_start).with_duration(rel_end - rel_start).with_position((x, y))
        )
    return layers


def _relative_window(
    event: MicroEvent, scene_start: float, hold: float
) -> tuple[float, float] | None:
    start = max(0.0, event.start - scene_start)
    end = min(hold, event.end - scene_start)
    if end - start < 0.2:
        return None
    return start, end


def _cover(clip: VideoFileClip, dest_w: int, dest_h: int, zoom: float = 1.0) -> VideoFileClip:
    src_w, src_h = int(clip.w), int(clip.h)
    factor = cover_scale(src_w, src_h, dest_w, dest_h, zoom)
    new_w = max(dest_w, int(round(src_w * factor)))
    new_h = max(dest_h, int(round(src_h * factor)))
    resized = clip.resized(new_size=(new_w, new_h))
    if int(resized.w) < dest_w or int(resized.h) < dest_h:
        return clip.resized(new_size=(dest_w, dest_h))
    x1 = max(0, (int(resized.w) - dest_w) // 2)
    y1 = max(0, (int(resized.h) - dest_h) // 2)
    return resized.cropped(x1=x1, y1=y1, width=dest_w, height=dest_h)


def _rounded_webcam(
    webcam: VideoFileClip,
    box_w: int,
    box_h: int,
    radius: int,
    zoom: float,
) -> VideoFileClip:
    cam = _cover(webcam, box_w, box_h, zoom=zoom)
    mask = ImageClip(_rounded_mask(box_w, box_h, radius), is_mask=True).with_duration(
        cam.duration or 1
    )
    return cam.with_mask(mask)


def _rounded_mask(width: int, height: int, radius: int) -> np.ndarray:
    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    return np.asarray(img, dtype=np.float32) / 255.0


def _rounded_border_clip(width: int, height: int, radius: int) -> ImageClip:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        outline=_BORDER_COLOR,
        width=max(3, height // 70),
    )
    return ImageClip(np.array(img))


def _still_clip(
    path: str,
    canvas: tuple[int, int],
    hold: float,
    stills: dict[str, np.ndarray],
) -> ImageClip | None:
    if not path:
        return None
    resolved = Path(path)
    if not resolved.is_file():
        return None
    key = str(resolved)
    if key not in stills:
        stills[key] = np.array(Image.open(resolved).convert("RGBA"))
    image = stills[key]
    clip = ImageClip(image).with_duration(hold)
    if (int(clip.w), int(clip.h)) != canvas:
        clip = clip.resized(new_size=canvas)
    return clip


def _lower_third_overlays(
    script: EditScript, size: tuple[int, int], duration: float
) -> list[ImageClip]:
    layers: list[ImageClip] = []
    for scene in script.scenes:
        path = scene.graphic.lower_third_path
        if not path or not Path(path).is_file():
            continue
        window = _clamp_window(scene.start, scene.end, duration)
        if window is None:
            continue
        start, end = window
        clip = (
            ImageClip(str(Path(path)))
            .with_start(start)
            .with_duration(end - start)
            .with_position((0, 0))
        )
        if (int(clip.w), int(clip.h)) != size:
            clip = clip.resized(new_size=size)
        layers.append(clip)

    for card in _pil_lower_thirds(script):
        overlay = _lower_third_clip(card, size, duration)
        if overlay is not None:
            layers.append(overlay)
    return layers


def _pil_lower_thirds(script: EditScript) -> list[LowerThird]:
    cards: list[LowerThird] = list(script.lower_thirds)
    for scene in script.scenes:
        if not scene.graphic.lower_third_title:
            continue
        if scene.graphic.lower_third_path and Path(scene.graphic.lower_third_path).is_file():
            continue
        cards.append(
            LowerThird(
                start=scene.start,
                end=scene.end,
                title=scene.graphic.lower_third_title,
                subtitle=scene.graphic.lower_third_subtitle,
            )
        )
    return cards


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
        fill=(*_ACCENT, 255),
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
