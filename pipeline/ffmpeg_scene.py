"""ffmpeg filter_complex scene encode. MoviePy is the per-scene fallback."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.broll.local import VIDEO_SUFFIXES
from pipeline.config import Settings, require_ffmpeg
from pipeline.encoder import NVENC_CODEC, VideoEncoder, select_video_encoder
from pipeline.hwaccel import HwDecode, gpu_filters_available, select_hw_decode
from pipeline.layouts import (
    DARK_RGB,
    GOLD,
    PIP_BORDER,
    PIP_RADIUS,
    PictureTag,
    pip_rect,
)
from pipeline.media import MediaError, _run_encode, probe_video_stream
from pipeline.models import EditScript, Scene
from pipeline.picture_kit import render_bookend, render_overlay, render_pip_type
from pipeline.shotlist import compose_mode, resolved_still_path

logger = logging.getLogger(__name__)


class FFmpegSceneError(RuntimeError):
    """filter_complex build or encode failed. Caller may fall back to MoviePy."""


@dataclass(frozen=True)
class FilterInput:
    """One ``-i`` (optional flags before the path)."""

    path: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class OverlayLayer:
    path: Path
    x: int
    y: int
    start: float
    end: float


@dataclass(frozen=True)
class PunchWindow:
    start: float
    end: float
    scale: float


@dataclass(frozen=True)
class SceneGraph:
    """Built filter graph plus the inputs it consumes (index 0 is the A-roll)."""

    inputs: tuple[FilterInput, ...]
    filter_complex: str
    video_map: str
    audio_from: int | None
    uses_gpu_filters: bool
    layout: PictureTag


def cover_filter(dest_w: int, dest_h: int, zoom: float = 1.0) -> str:
    """Scale-to-cover then center-crop. Full-frame host only."""
    width = max(2, dest_w)
    height = max(2, dest_h)
    factor = max(float(zoom), 1.0)
    scale_w = max(width, int(round(width * factor)))
    scale_h = max(height, int(round(height * factor)))
    return (
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"crop={width}:{height},setsar=1"
    )


def fit_filter(dest_w: int, dest_h: int) -> str:
    """Scale-to-fit the entire frame, then pad. PiP host. Never a face crop."""
    width = max(2, dest_w)
    height = max(2, dest_h)
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x080A0E,setsar=1"
    )


def gpu_filters_suitable(scene: Scene) -> bool:
    """CUDA scale only covers a full-canvas host with no chrome."""
    return compose_mode(scene) == "nothing" and scene.layout is PictureTag.NOTHING


def build_scene_graph(
    *,
    video_path: Path,
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    fps: float,
    graphic: Path | None,
    overlays: tuple[OverlayLayer, ...],
    mask: Path | None,
    border: Path | None,
    use_gpu_filters: bool = False,
    hw: HwDecode | None = None,
) -> SceneGraph:
    """Build filter_complex for nothing, overlay, pip, or bookend."""
    hold = max(0.04, float(scene.duration))
    width, height = canvas
    dark = _rgb_hex(DARK_RGB)
    webcam_args = ("-ss", f"{scene.start:.3f}", "-t", f"{hold:.3f}")
    inputs: list[FilterInput] = [FilterInput(path=str(video_path), args=webcam_args)]

    graphic_is_video = bool(graphic and graphic.suffix.lower() in VIDEO_SUFFIXES)
    if graphic is not None and graphic.is_file():
        if graphic_is_video:
            inputs.append(
                FilterInput(
                    path=str(graphic),
                    args=("-stream_loop", "-1", "-t", f"{hold:.3f}"),
                )
            )
        else:
            inputs.append(
                FilterInput(
                    path=str(graphic),
                    args=("-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{hold:.3f}"),
                )
            )

    if use_gpu_filters and gpu_filters_suitable(scene):
        filt = (
            f"[0:v]scale_cuda={width}:{height}:force_original_aspect_ratio=increase,"
            f"hwdownload,format=nv12,crop={width}:{height},setsar=1,format=yuv420p[vout]"
        )
        return SceneGraph(
            inputs=tuple(inputs),
            filter_complex=filt,
            video_map="vout",
            audio_from=0,
            uses_gpu_filters=True,
            layout=scene.layout,
        )

    cam_prefix = _cpu_prefix(hw)

    mode = compose_mode(scene)
    if mode == "pip":
        if mask is None or border is None:
            raise FFmpegSceneError("PIP layout needs a rounded mask and border PNG")
        return _pip_graph(
            scene=scene,
            canvas=canvas,
            hold=hold,
            fps=fps,
            dark=dark,
            inputs=inputs,
            graphic=graphic,
            mask=mask,
            border=border,
            overlays=overlays,
            cam_prefix=cam_prefix,
        )

    return _full_frame_graph(
        scene=scene,
        canvas=canvas,
        hold=hold,
        fps=fps,
        dark=dark,
        inputs=inputs,
        graphic=graphic,
        graphic_is_video=graphic_is_video,
        overlays=overlays,
        cam_prefix=cam_prefix,
    )


def encode_scene_ffmpeg(
    video_path: Path,
    script: EditScript,
    scene: Scene,
    dest: Path,
    settings: Settings,
    canvas: tuple[int, int],
) -> None:
    """Encode one scene with filter_complex. Raises if ffmpeg cannot do it."""
    hold = float(scene.duration)
    if hold < 0.04:
        raise FFmpegSceneError(f"Scene {scene.start:.2f}-{scene.end:.2f} is too short")

    try:
        _width, _height, fps = probe_video_stream(video_path, settings)
    except MediaError:
        fps = 30.0
    if fps <= 1.0:
        fps = 30.0

    encoder = select_video_encoder(settings)
    hw = select_hw_decode(
        settings,
        video_path,
        enabled=encoder.name == NVENC_CODEC,
    )
    want_gpu = (
        encoder.name == NVENC_CODEC
        and hw is not None
        and hw.cuda_frames
        and gpu_filters_available(settings)
        and gpu_filters_suitable(scene)
        and not _scene_needs_overlays(script, scene)
    )

    work = dest.with_name(dest.stem + "_fftmp")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        graphic = _graphic_path(scene)
        overlays = write_scene_overlay_pngs(script, scene, canvas, work)
        mask, border = _write_pip_assets(scene, settings, canvas, work)
        if want_gpu:
            try:
                _encode_graph(
                    video_path=video_path,
                    scene=scene,
                    settings=settings,
                    canvas=canvas,
                    fps=fps,
                    dest=dest,
                    encoder=encoder,
                    hw=hw,
                    use_gpu_filters=True,
                    graphic=graphic,
                    overlays=overlays,
                    mask=mask,
                    border=border,
                    hold=hold,
                )
                return
            except (FFmpegSceneError, MediaError) as exc:
                logger.warning("GPU filters failed (%s). Retrying CPU overlay/scale.", exc)

        try:
            _encode_graph(
                video_path=video_path,
                scene=scene,
                settings=settings,
                canvas=canvas,
                fps=fps,
                dest=dest,
                encoder=encoder,
                hw=hw,
                use_gpu_filters=False,
                graphic=graphic,
                overlays=overlays,
                mask=mask,
                border=border,
                hold=hold,
            )
            return
        except (FFmpegSceneError, MediaError) as exc:
            if hw is None:
                raise
            logger.warning("hw decode + CPU filters failed (%s). Retrying software decode.", exc)
            _encode_graph(
                video_path=video_path,
                scene=scene,
                settings=settings,
                canvas=canvas,
                fps=fps,
                dest=dest,
                encoder=encoder,
                hw=None,
                use_gpu_filters=False,
                graphic=graphic,
                overlays=overlays,
                mask=mask,
                border=border,
                hold=hold,
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not dest.is_file() or dest.stat().st_size == 0:
        raise FFmpegSceneError(f"ffmpeg scene encode produced no output at {dest}")


def write_scene_overlay_pngs(
    script: EditScript,
    scene: Scene,
    canvas: tuple[int, int],
    dest_dir: Path,
) -> tuple[OverlayLayer, ...]:
    """Rasterize locked-kit chrome for this scene."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    mode = compose_mode(scene)
    hold = scene.duration
    if mode == "bookend":
        image = render_bookend(
            canvas,
            identity=script.identity,
            kicker=scene.graphic.kicker,
            headline=scene.graphic.title,
            icon=scene.graphic.icon or ("share" if scene.role == "close" else "bar_chart"),
        )
    elif mode == "overlay":
        image = render_overlay(
            canvas,
            kicker=scene.graphic.kicker,
            headline=scene.graphic.title,
            icon=scene.graphic.icon or "bar_chart",
        )
    elif mode == "pip":
        image = render_pip_type(
            canvas,
            kicker=scene.graphic.kicker,
            sub=scene.graphic.title,
            quote=scene.graphic.quote,
        )
    else:
        return ()
    png = dest_dir / "kit.png"
    Image.fromarray(image).save(png)
    return (OverlayLayer(png, 0, 0, 0.0, hold),)


def _encode_graph(
    *,
    video_path: Path,
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    fps: float,
    dest: Path,
    encoder: VideoEncoder,
    hw: HwDecode | None,
    use_gpu_filters: bool,
    graphic: Path | None,
    overlays: tuple[OverlayLayer, ...],
    mask: Path | None,
    border: Path | None,
    hold: float,
) -> None:
    graph = build_scene_graph(
        video_path=video_path,
        scene=scene,
        settings=settings,
        canvas=canvas,
        fps=fps,
        graphic=graphic,
        overlays=overlays,
        mask=mask,
        border=border,
        use_gpu_filters=use_gpu_filters,
        hw=hw,
    )
    ffmpeg_bin = require_ffmpeg(settings)

    def build(chosen: VideoEncoder) -> list[str]:
        return build_ffmpeg_command(
            ffmpeg_bin,
            graph,
            dest,
            chosen,
            hw=hw,
            hold=hold,
        )

    try:
        _run_encode(settings, f"ffmpeg scene {scene.layout.value}", build)
    except MediaError as exc:
        raise FFmpegSceneError(str(exc)) from exc
    if not dest.is_file() or dest.stat().st_size == 0:
        raise FFmpegSceneError(f"ffmpeg scene encode produced no output at {dest}")


def build_ffmpeg_command(
    ffmpeg_bin: str,
    graph: SceneGraph,
    dest: Path,
    encoder: VideoEncoder,
    *,
    hw: HwDecode | None,
    hold: float,
) -> list[str]:
    cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    for index, source in enumerate(graph.inputs):
        if index == 0 and hw is not None:
            cmd.extend(hw.input_args)
        cmd.extend(source.args)
        cmd.extend(["-i", source.path])
    cmd.extend(["-filter_complex", graph.filter_complex, "-map", f"[{graph.video_map}]"])
    if graph.audio_from is not None:
        cmd.extend(["-map", f"{graph.audio_from}:a?"])
    else:
        cmd.append("-an")
    cmd.extend(encoder.ffmpeg_video_args(quality="medium"))
    cmd.extend(
        [
            "-c:a",
            "aac",
            "-t",
            f"{hold:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    return cmd


def _full_frame_graph(
    *,
    scene: Scene,
    canvas: tuple[int, int],
    hold: float,
    fps: float,
    dark: str,
    inputs: list[FilterInput],
    graphic: Path | None,
    graphic_is_video: bool,
    overlays: tuple[OverlayLayer, ...],
    cam_prefix: str,
) -> SceneGraph:
    width, height = canvas
    filters: list[str] = []
    del graphic_is_video
    filters.append(f"{cam_prefix}{cover_filter(width, height)},format=rgba[cam]")
    current = "cam"
    current, extra = _append_overlays(inputs, overlays, filters, current)
    inputs.extend(extra)
    filters.append(f"[{current}]format=yuv420p[vout]")
    return SceneGraph(
        inputs=tuple(inputs),
        filter_complex=";".join(filters),
        video_map="vout",
        audio_from=0,
        uses_gpu_filters=False,
        layout=scene.layout,
    )


def _pip_graph(
    *,
    scene: Scene,
    canvas: tuple[int, int],
    hold: float,
    fps: float,
    dark: str,
    inputs: list[FilterInput],
    graphic: Path | None,
    mask: Path,
    border: Path,
    overlays: tuple[OverlayLayer, ...],
    cam_prefix: str,
) -> SceneGraph:
    width, height = canvas
    x, y, box_w, box_h = pip_rect(width, height)
    filters: list[str] = []
    if graphic is not None:
        filters.append(f"[1:v]scale={width}:{height},setsar=1,format=rgba[bg]")
    else:
        filters.append(
            f"color=c={dark}:s={width}x{height}:d={hold:.3f}:r={fps:.3f},format=rgba[bg]"
        )

    still_args = ("-loop", "1", "-t", f"{hold:.3f}")
    mask_index = len(inputs)
    inputs.append(FilterInput(path=str(mask), args=still_args))
    border_index = len(inputs)
    inputs.append(FilterInput(path=str(border), args=still_args))

    filters.append(f"{cam_prefix}{fit_filter(box_w, box_h)},format=rgba[camrgb]")
    filters.append(f"[{mask_index}:v]scale={box_w}:{box_h},format=gray[mask0]")
    filters.append("[camrgb][mask0]alphamerge[pip]")
    filters.append(f"[bg][{border_index}:v]overlay={x}:{y}[bgb]")
    filters.append(f"[bgb][pip]overlay={x}:{y}[base]")
    current = "base"

    current, extra = _append_overlays(inputs, overlays, filters, current)
    inputs.extend(extra)
    filters.append(f"[{current}]format=yuv420p[vout]")
    return SceneGraph(
        inputs=tuple(inputs),
        filter_complex=";".join(filters),
        video_map="vout",
        audio_from=0,
        uses_gpu_filters=False,
        layout=scene.layout,
    )


def _split_graph(
    *,
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    hold: float,
    fps: float,
    dark: str,
    inputs: list[FilterInput],
    graphic: Path | None,
    overlays: tuple[OverlayLayer, ...],
    punches: tuple[PunchWindow, ...],
    cam_prefix: str,
) -> SceneGraph:
    width, height = canvas
    x, y, box_w, box_h = split_webcam_rect(width, height, settings.split_top_ratio)
    filters: list[str] = [
        f"color=c={dark}:s={width}x{height}:d={hold:.3f}:r={fps:.3f},format=rgba[canvas]",
        f"{cam_prefix}{cover_filter(box_w, box_h)},format=rgba[cam]",
        f"[canvas][cam]overlay={x}:{y}[base]",
    ]
    current = "base"
    for index, punch in enumerate(punches):
        label = f"punch{index}"
        nxt = f"pout{index}"
        filters.append(
            f"{cam_prefix}{cover_filter(box_w, box_h, punch.scale)},format=rgba[{label}]"
        )
        filters.append(
            f"[{current}][{label}]overlay={x}:{y}:enable='{_between(punch.start, punch.end)}'[{nxt}]"
        )
        current = nxt

    if graphic is not None:
        filters.append(f"[1:v]scale={width}:{height},setsar=1,format=rgba[g]")
        nxt = "splitg"
        filters.append(f"[{current}][g]overlay=0:0[{nxt}]")
        current = nxt

    current, extra = _append_overlays(inputs, overlays, filters, current)
    inputs.extend(extra)
    filters.append(f"[{current}]format=yuv420p[vout]")
    return SceneGraph(
        inputs=tuple(inputs),
        filter_complex=";".join(filters),
        video_map="vout",
        audio_from=0,
        uses_gpu_filters=False,
        layout=scene.layout,
    )


def _append_overlays(
    inputs: list[FilterInput],
    overlays: tuple[OverlayLayer, ...],
    filters: list[str],
    current: str,
) -> tuple[str, list[FilterInput]]:
    extra: list[FilterInput] = []
    for index, layer in enumerate(overlays):
        if not layer.path.is_file():
            raise FFmpegSceneError(f"overlay missing: {layer.path}")
        inp_index = len(inputs) + len(extra)
        extra.append(
            FilterInput(
                path=str(layer.path),
                args=("-loop", "1", "-t", f"{max(layer.end, 0.04):.3f}"),
            )
        )
        nxt = f"ov{index}"
        filters.append(
            f"[{current}][{inp_index}:v]overlay={layer.x}:{layer.y}:"
            f"enable='{_between(layer.start, layer.end)}'[{nxt}]"
        )
        current = nxt
    return current, extra


def _cpu_prefix(hw: HwDecode | None) -> str:
    if hw is not None and hw.cuda_frames:
        return "[0:v]hwdownload,format=nv12,"
    return "[0:v]"


def _graphic_path(scene: Scene) -> Path | None:
    if compose_mode(scene) != "pip":
        return None
    return resolved_still_path(scene)


def _write_pip_assets(
    scene: Scene,
    settings: Settings,
    canvas: tuple[int, int],
    work: Path,
) -> tuple[Path | None, Path | None]:
    del settings
    if compose_mode(scene) != "pip":
        return None, None
    width, height = canvas
    _x, _y, box_w, box_h = pip_rect(width, height)
    radius = max(8, int(round(PIP_RADIUS * (height / 1080))))
    mask = work / "pip_mask.png"
    border = work / "pip_border.png"
    write_rounded_mask(mask, box_w, box_h, radius)
    write_rounded_border(border, box_w, box_h, radius)
    return mask, border


def write_rounded_mask(path: Path, width: int, height: int, radius: int) -> Path:
    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def write_rounded_border(path: Path, width: int, height: int, radius: int) -> Path:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        outline=(*GOLD, 255),
        width=max(PIP_BORDER, height // 100),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def _punch_windows(scene: Scene) -> tuple[PunchWindow, ...]:
    hold = scene.duration
    windows: list[PunchWindow] = []
    for event in scene.micro_events:
        if event.kind != "punch_in":
            continue
        rel = _relative_window(event, scene.start, hold)
        if rel is None:
            continue
        start, end = rel
        windows.append(PunchWindow(start, end, max(1.05, float(event.scale or 1.15))))
    return tuple(windows)


def _relative_window(
    event: MicroEvent, scene_start: float, hold: float
) -> tuple[float, float] | None:
    start = max(0.0, event.start - scene_start)
    end = min(hold, event.end - scene_start)
    if end - start < 0.2:
        return None
    return start, end


def _shift_to_scene(
    start: float, end: float, scene_start: float, hold: float
) -> tuple[float, float] | None:
    rel_start = max(0.0, start - scene_start)
    rel_end = min(hold, end - scene_start)
    if rel_end - rel_start < 0.2:
        return None
    return rel_start, rel_end


def _scene_needs_overlays(script: EditScript, scene: Scene) -> bool:
    del script
    return compose_mode(scene) in {"overlay", "pip", "bookend"}


def _between(start: float, end: float) -> str:
    return f"between(t,{start:.3f},{end:.3f})"


def _rgb_hex(rgb: tuple[int, int, int]) -> str:
    return f"0x{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
