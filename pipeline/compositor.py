from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from moviepy import ColorClip, CompositeVideoClip, ImageClip, VideoFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

from pipeline.broll.local import VIDEO_SUFFIXES, apply_local_broll
from pipeline.config import Settings, require_ffmpeg
from pipeline.encoder import (
    NVENC_CODEC,
    remember_nvenc_failure,
    select_video_encoder,
    software_encoder,
)
from pipeline.layouts import (
    BORDER_COLOR,
    DARK_RGB,
    LayoutKind,
    cover_scale,
    pip_rect,
    split_webcam_rect,
)
from pipeline.media import MediaError, concat_scene_files, probe_duration
from pipeline.models import (
    EditScript,
    LowerThird,
    MicroEvent,
    OverlayCallout,
    Scene,
)
from pipeline.shotlist import (
    compose_mode,
    resolve_edit_script,
    resolve_scene,
    resolved_media_path,
    scene_has_visual,
)

logger = logging.getLogger(__name__)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)

_DARK = DARK_RGB
_BORDER_COLOR = BORDER_COLOR
_ACCENT = (56, 189, 248)


class CompositorError(RuntimeError):
    """MoviePy / render failure."""


def canvas_size(settings: Settings) -> tuple[int, int]:
    return int(settings.output_width), int(settings.output_height)


def scene_fingerprint(scene: Scene, settings: Settings) -> str:
    payload = {
        "start": round(scene.start, 3),
        "end": round(scene.end, 3),
        "layout": scene.layout.value,
        "said": scene.said,
        "shown": scene.shown,
        "asset_kind": scene.asset_kind,
        "asset_ref": scene.asset_ref,
        "graphic": scene.graphic.model_dump(),
        "micro": [event.model_dump() for event in scene.micro_events],
        "canvas": [settings.output_width, settings.output_height, settings.pip_scale],
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def scene_encode_path(dest_dir: Path, index: int, fingerprint: str) -> Path:
    return dest_dir / f"scene_{index:04d}_{fingerprint}.mp4"


def scene_cache_valid(
    path: Path,
    scene: Scene,
    settings: Settings,
    *,
    fingerprint: str | None = None,
) -> bool:
    """True when a scene MP4 exists and matches this scene (resume)."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    key = fingerprint or scene_fingerprint(scene, settings)
    sidecar = path.with_suffix(".json")
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if data.get("fingerprint") == key:
            return True
        return False
    try:
        duration = probe_duration(path, settings)
    except MediaError:
        return False
    return abs(duration - scene.duration) < 0.2


def write_scene_sidecar(path: Path, scene: Scene, fingerprint: str) -> Path:
    sidecar = path.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "start": scene.start,
                "end": scene.end,
                "layout": scene.layout.value,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return sidecar


def render_video(
    video_path: Path,
    script: EditScript,
    output_path: Path,
    settings: Settings,
    *,
    broll_dir: Path | None = None,
) -> Path:
    """Encode each scene, concat with ffmpeg, mux H.264/AAC toward -14 LUFS."""
    require_ffmpeg(settings)
    select_video_encoder(settings)
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Working video not found: {video_path}")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.ensure_dirs()
    if broll_dir is not None:
        apply_local_broll(script, Path(broll_dir))
    resolve_edit_script(script)

    duration = probe_duration(video_path, settings)
    canvas = canvas_size(settings)
    scenes = _scenes_or_full(script, duration)
    scene_dir = (settings.scenes_dir / video_path.stem).resolve()
    scene_dir.mkdir(parents=True, exist_ok=True)

    parts = _encode_scenes(video_path, script, scenes, scene_dir, settings, canvas)
    if not parts:
        raise CompositorError("No scenes to composite")

    try:
        concat_scene_files(parts, output_path, settings, loudnorm=True)
        print(f"      concat {len(parts)} scene(s) + loudnorm {settings.target_lufs:.0f} LUFS")
    except MediaError as exc:
        logger.warning("ffmpeg concat/loudnorm failed (%s). Falling back to MoviePy.", exc)
        print(f"      ffmpeg concat failed, falling back to in-memory MoviePy: {exc}")
        _render_in_memory(video_path, script, scenes, output_path, settings, canvas, duration)
        try:
            tmp = output_path.with_name(output_path.stem + "_loud.mp4")
            from pipeline.media import apply_loudnorm

            apply_loudnorm(output_path, tmp, settings)
            tmp.replace(output_path)
        except MediaError as loud_exc:
            print(f"      loudnorm fallback skipped: {loud_exc}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise CompositorError(f"Render produced no output at {output_path}")
    return output_path


def _encode_scenes(
    video_path: Path,
    script: EditScript,
    scenes: list[Scene],
    scene_dir: Path,
    settings: Settings,
    canvas: tuple[int, int],
) -> list[Path]:
    for scene in scenes:
        resolve_scene(scene)
    total = len(scenes)
    parts: list[Path | None] = [None] * total
    jobs: list[tuple[int, Scene, Path, str]] = []
    for index, scene in enumerate(scenes):
        fingerprint = scene_fingerprint(scene, settings)
        dest = scene_encode_path(scene_dir, index, fingerprint)
        stale = list(scene_dir.glob(f"scene_{index:04d}_*.mp4"))
        if scene_cache_valid(dest, scene, settings, fingerprint=fingerprint):
            print(f"      resume scene {index + 1}/{total} {dest.name}")
            parts[index] = dest
            continue
        for old in stale:
            if old != dest and old.exists():
                old.unlink()
            sidecar = old.with_suffix(".json")
            if sidecar.exists() and sidecar != dest.with_suffix(".json"):
                sidecar.unlink()
        jobs.append((index, scene, dest, fingerprint))

    if jobs:
        workers = max(1, min(int(settings.encode_concurrency), len(jobs)))
        if workers == 1:
            for job in jobs:
                index, dest = _run_scene_job(video_path, script, settings, canvas, total, job)
                parts[index] = dest
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _run_scene_job, video_path, script, settings, canvas, total, job
                    )
                    for job in jobs
                ]
                for future in as_completed(futures):
                    index, dest = future.result()
                    parts[index] = dest

    finished = [path for path in parts if path is not None]
    if len(finished) != total:
        raise CompositorError("Scene encode missed one or more outputs")
    return finished


def _run_scene_job(
    video_path: Path,
    script: EditScript,
    settings: Settings,
    canvas: tuple[int, int],
    total: int,
    job: tuple[int, Scene, Path, str],
) -> tuple[int, Path]:
    index, scene, dest, fingerprint = job
    backend = _encode_one_scene_or_fallback(
        video_path, script, scene, dest, settings, canvas
    )
    write_scene_sidecar(dest, scene, fingerprint)
    encoder = select_video_encoder(settings)
    print(
        f"      encode scene {index + 1}/{total} {scene.layout.value} "
        f"encoder={encoder.name} backend={backend}"
    )
    return index, dest


def _encode_one_scene_or_fallback(
    video_path: Path,
    script: EditScript,
    scene: Scene,
    dest: Path,
    settings: Settings,
    canvas: tuple[int, int],
) -> str:
    """ffmpeg filter_complex first; MoviePy only if that graph fails."""
    from pipeline.ffmpeg_scene import FFmpegSceneError, encode_scene_ffmpeg

    try:
        encode_scene_ffmpeg(video_path, script, scene, dest, settings, canvas)
        return "ffmpeg"
    except (FFmpegSceneError, MediaError, OSError, RuntimeError) as exc:
        logger.warning(
            "ffmpeg scene graph failed (%s). Falling back to MoviePy for this scene.",
            exc,
        )
        print(f"      ffmpeg graph failed, MoviePy fallback: {exc}")
        if dest.exists():
            dest.unlink()
        _encode_one_scene(video_path, script, scene, dest, settings, canvas, {})
        return "moviepy"


def _encode_one_scene(
    video_path: Path,
    script: EditScript,
    scene: Scene,
    dest: Path,
    settings: Settings,
    canvas: tuple[int, int],
    stills: dict[str, np.ndarray],
) -> Path:
    # Open a fresh source clip per scene. MoviePy 2 CompositeVideoClip.close()
    # closes self.audio, and AudioFileClip.close() kills the shared ffmpeg
    # reader (proc=None). The audio reader has no initialize() recovery, so
    # reusing one VideoFileClip then raises None.stdout on the next scene.
    with VideoFileClip(str(video_path)) as a_roll:
        piece = _compose_scene(a_roll, scene, settings, canvas, stills)
        if piece is None:
            raise CompositorError(f"Scene {scene.start:.2f}-{scene.end:.2f} produced no clip")
        hold = float(piece.duration or scene.duration)
        layers: list[object] = [piece]
        for overlay in _scene_overlay_clips(script, scene, canvas):
            layers.append(overlay)
        final = CompositeVideoClip(layers, size=canvas).with_duration(hold)
        start = max(0.0, min(scene.start, float(a_roll.duration or 0)))
        end = max(start, min(scene.end, float(a_roll.duration or 0)))
        if a_roll.audio is not None and end > start:
            final = final.with_audio(a_roll.subclipped(start, end).audio)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_composed_clip(final, dest, a_roll.fps or 30, settings)
        final.close()
        piece.close()
    if not dest.exists() or dest.stat().st_size == 0:
        raise CompositorError(f"Scene encode produced no output at {dest}")
    return dest


def _write_composed_clip(
    clip: CompositeVideoClip,
    dest: Path,
    fps: float,
    settings: Settings,
) -> None:
    """MoviePy write with NVENC when selected; retry libx264 if NVENC fails."""
    encoder = select_video_encoder(settings)
    kwargs = encoder.moviepy_write_kwargs(fps=fps)
    try:
        clip.write_videofile(str(dest), **kwargs)
        return
    except (OSError, RuntimeError) as exc:
        if encoder.name != NVENC_CODEC:
            raise
        remember_nvenc_failure(str(exc))
        clip.write_videofile(str(dest), **software_encoder().moviepy_write_kwargs(fps=fps))


def _render_in_memory(
    video_path: Path,
    script: EditScript,
    scenes: list[Scene],
    output_path: Path,
    settings: Settings,
    canvas: tuple[int, int],
    duration: float,
) -> Path:
    base = VideoFileClip(str(video_path))
    try:
        timeline = float(base.duration or duration)
        stills: dict[str, np.ndarray] = {}
        for scene in scenes:
            resolve_scene(scene)
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

        _write_composed_clip(final, output_path, base.fps or 30, settings)
        final.close()
        composed.close()
        for piece in pieces:
            piece.close()
    finally:
        base.close()
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
    media = resolved_media_path(scene)
    graphic_path = str(media) if media is not None else ""
    if scene.asset_kind == "card" and scene.graphic.asset_path:
        graphic_path = scene.graphic.asset_path
    graphic = (
        _graphic_clip(graphic_path, canvas, hold, stills)
        if scene_has_visual(scene)
        else None
    )

    mode = compose_mode(scene)
    # none, or a missing broll/site file, is talking-head only. No empty slide.
    if graphic is None or mode == "talking_head":
        layers.extend(_full_frame_layers(webcam, scene, canvas, hold, start, None))
    elif mode == "cutaway":
        # DVIDS / local b-roll covers the face. Host audio is muxed separately.
        layers.extend(_full_frame_layers(webcam, scene, canvas, hold, start, graphic))
    elif scene.layout is LayoutKind.PIP_BOTTOM_RIGHT:
        layers.extend(
            _pip_layers(webcam, scene, settings, canvas, hold, start, graphic)
        )
    elif scene.layout is LayoutKind.SPLIT_TOP:
        layers.extend(
            _split_layers(webcam, scene, settings, canvas, hold, start, graphic)
        )
    else:
        layers.extend(_full_frame_layers(webcam, scene, canvas, hold, start, graphic))

    return CompositeVideoClip(layers, size=canvas).with_duration(hold)


def _full_frame_layers(
    webcam: VideoFileClip,
    scene: Scene,
    canvas: tuple[int, int],
    hold: float,
    scene_start: float,
    graphic: object | None = None,
) -> list[object]:
    width, height = canvas
    if graphic is not None and (
        _is_video_asset(scene.graphic.asset_path) or compose_mode(scene) == "cutaway"
    ):
        return [graphic]
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
    graphic: object | None,
) -> list[object]:
    width, height = canvas
    x, y, box_w, box_h = pip_rect(width, height, settings.pip_scale)
    layers: list[object] = []
    if graphic is not None:
        layers.append(graphic)
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
    graphic: object | None,
) -> list[object]:
    width, height = canvas
    x, y, box_w, box_h = split_webcam_rect(width, height, settings.split_top_ratio)
    layers: list[object] = [
        _cover(webcam, box_w, box_h).with_duration(hold).with_position((x, y))
    ]
    layers.extend(
        _punch_layers(webcam, scene, scene_start, hold, dest=(x, y, box_w, box_h))
    )
    if graphic is not None:
        layers.append(graphic)
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


def _is_video_asset(path: str) -> bool:
    return bool(path) and Path(path).suffix.lower() in VIDEO_SUFFIXES


def _graphic_clip(
    path: str,
    canvas: tuple[int, int],
    hold: float,
    stills: dict[str, np.ndarray],
) -> object | None:
    if not path:
        return None
    resolved = Path(path)
    if not resolved.is_file():
        return None
    if _is_video_asset(path):
        return _broll_video_clip(resolved, canvas, hold)
    return _still_clip(path, canvas, hold, stills)


def _broll_video_clip(path: Path, canvas: tuple[int, int], hold: float) -> VideoFileClip:
    clip = VideoFileClip(str(path)).without_audio()
    duration = float(clip.duration or 0)
    if duration <= 0:
        clip.close()
        raise CompositorError(f"B-roll video has no duration: {path}")
    if duration + 0.04 < hold:
        loops = int(hold / duration) + 1
        clip = concatenate_videoclips([clip] * loops, method="compose")
    if float(clip.duration or 0) > hold:
        clip = clip.subclipped(0, hold)
    covered = _cover(clip, canvas[0], canvas[1]).with_duration(hold)
    return covered


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


def _scene_overlay_clips(
    script: EditScript, scene: Scene, canvas: tuple[int, int]
) -> list[ImageClip]:
    """Lower-thirds and takeaways clipped to this scene, times relative to 0."""
    hold = scene.duration
    layers: list[ImageClip] = []
    for overlay in _lower_third_overlays(script, canvas, scene.end + 0.01):
        shifted = _shift_overlay_to_scene(overlay, scene.start, hold)
        if shifted is not None:
            layers.append(shifted)
    for callout in script.collected_text_overlays():
        layer = _callout_clip(callout, canvas, scene.end + 0.01)
        if layer is None:
            continue
        shifted = _shift_overlay_to_scene(layer, scene.start, hold)
        if shifted is not None:
            layers.append(shifted)
    return layers


def _shift_overlay_to_scene(
    clip: ImageClip, scene_start: float, hold: float
) -> ImageClip | None:
    start = float(getattr(clip, "start", 0.0) or 0.0)
    duration = float(clip.duration or 0.0)
    end = start + duration
    rel_start = max(0.0, start - scene_start)
    rel_end = min(hold, end - scene_start)
    if rel_end - rel_start < 0.2:
        return None
    return clip.with_start(rel_start).with_duration(rel_end - rel_start)


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
