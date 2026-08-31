from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from pipeline.compositor import _encode_scenes
from pipeline.media import probe_duration
from pipeline.config import Settings
from pipeline.ffmpeg_scene import (
    FFmpegSceneError,
    OverlayLayer,
    _graphic_path,
    build_ffmpeg_command,
    build_scene_graph,
    cover_filter,
    gpu_filters_suitable,
)
from pipeline.shotlist import resolve_scene
from pipeline.encoder import nvenc_encoder, software_encoder
from pipeline.hwaccel import HwDecode
from pipeline.layouts import LayoutKind, pip_rect, split_webcam_rect
from pipeline.models import EditScript, GraphicCard, MicroEvent, Scene


def _settings(tmp_path: Path, *, concurrency: int = 2) -> Settings:
    return Settings(
        output_width=1920,
        output_height=1080,
        work_dir=tmp_path,
        output_dir=tmp_path,
        slides_dir=tmp_path,
        scenes_dir=tmp_path / "scenes",
        encode_concurrency=concurrency,
    )


def _scene(layout: LayoutKind, *, graphic: str = "", punch: bool = False) -> Scene:
    events = []
    if punch:
        events.append(MicroEvent(start=0.4, end=0.9, kind="punch_in", scale=1.15))
    return Scene(
        start=0.0,
        end=1.0,
        layout=layout,
        graphic=GraphicCard(title="Card", asset_path=graphic),
        micro_events=events,
    )


def _graph(
    tmp_path: Path,
    scene: Scene,
    *,
    graphic: Path | None = None,
    overlays: tuple[OverlayLayer, ...] = (),
    mask: Path | None = None,
    border: Path | None = None,
    use_gpu_filters: bool = False,
    hw: HwDecode | None = None,
):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"x")
    settings = _settings(tmp_path)
    return build_scene_graph(
        video_path=video,
        scene=scene,
        settings=settings,
        canvas=(1920, 1080),
        fps=30.0,
        graphic=graphic,
        overlays=overlays,
        mask=mask,
        border=border,
        use_gpu_filters=use_gpu_filters,
        hw=hw,
    )


def test_none_leftover_slide_is_not_an_ffmpeg_graphic(tmp_path: Path) -> None:
    leftover = tmp_path / "empty.png"
    leftover.write_bytes(b"png")
    scene = Scene(
        start=0,
        end=1,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        asset_kind="none",
        graphic=GraphicCard(title="Empty", asset_path=str(leftover)),
    )
    resolve_scene(scene)
    assert _graphic_path(scene) is None
    assert scene.layout is LayoutKind.FULL_FRAME


def test_broll_file_is_ffmpeg_full_frame_cutaway(tmp_path: Path) -> None:
    clip = tmp_path / "dvids.mp4"
    clip.write_bytes(b"vid")
    scene = Scene(
        start=0,
        end=1,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        asset_kind="broll",
        asset_ref=str(clip),
        graphic=GraphicCard(title="DVIDS"),
    )
    resolve_scene(scene)
    graphic = _graphic_path(scene)
    assert graphic is not None
    assert graphic == clip.resolve()
    assert scene.layout is LayoutKind.FULL_FRAME
    graph = _graph(tmp_path, scene, graphic=graphic)
    assert graph.layout is LayoutKind.FULL_FRAME
    assert "[1:v]" in graph.filter_complex


def test_cover_filter_scales_then_crops() -> None:
    filt = cover_filter(1920, 1080)
    assert "scale=1920:1080:force_original_aspect_ratio=increase" in filt
    assert "crop=1920:1080" in filt
    zoomed = cover_filter(1920, 1080, 1.15)
    assert "scale=2208:1242" in zoomed
    assert "crop=1920:1080" in zoomed


def test_full_frame_graph_cover_and_punch(tmp_path: Path) -> None:
    scene = _scene(LayoutKind.FULL_FRAME, punch=True)
    graph = _graph(tmp_path, scene)
    assert graph.layout is LayoutKind.FULL_FRAME
    assert not graph.uses_gpu_filters
    assert "crop=1920:1080" in graph.filter_complex
    assert "overlay=0:0:enable='between(t,0.400,0.900)'" in graph.filter_complex
    assert graph.inputs[0].args[:2] == ("-ss", "0.000")
    assert graph.video_map == "vout"


def test_pip_graph_mask_border_and_overlay(tmp_path: Path) -> None:
    slide = tmp_path / "slide.png"
    slide.write_bytes(b"png")
    mask = tmp_path / "mask.png"
    border = tmp_path / "border.png"
    mask.write_bytes(b"m")
    border.write_bytes(b"b")
    scene = _scene(LayoutKind.PIP_BOTTOM_RIGHT, graphic=str(slide))
    graph = _graph(tmp_path, scene, graphic=slide, mask=mask, border=border)
    x, y, box_w, box_h = pip_rect(1920, 1080, 0.25)
    assert "alphamerge" in graph.filter_complex
    assert f"crop={box_w}:{box_h}" in graph.filter_complex
    assert f"overlay={x}:{y}" in graph.filter_complex
    assert str(mask) in {item.path for item in graph.inputs}
    assert str(border) in {item.path for item in graph.inputs}
    punched = _scene(LayoutKind.PIP_BOTTOM_RIGHT, graphic=str(slide), punch=True)
    punch_graph = _graph(tmp_path, punched, graphic=slide, mask=mask, border=border)
    assert "split=2[mask0][mask1]" in punch_graph.filter_complex
    assert "[mask1]alphamerge" in punch_graph.filter_complex


def test_split_graph_top_band(tmp_path: Path) -> None:
    slide = tmp_path / "split.png"
    slide.write_bytes(b"png")
    scene = _scene(LayoutKind.SPLIT_TOP, graphic=str(slide))
    graph = _graph(tmp_path, scene, graphic=slide)
    _x, _y, box_w, box_h = split_webcam_rect(1920, 1080, 2.0 / 3.0)
    assert box_w == 1920
    assert box_h == 720
    assert f"crop={box_w}:{box_h}" in graph.filter_complex
    assert "overlay=0:0" in graph.filter_complex
    assert "[1:v]scale=1920:1080" in graph.filter_complex


def test_gpu_graph_only_for_plain_full_frame(tmp_path: Path) -> None:
    scene = _scene(LayoutKind.FULL_FRAME)
    assert gpu_filters_suitable(scene)
    graph = _graph(tmp_path, scene, use_gpu_filters=True)
    assert graph.uses_gpu_filters
    assert "scale_cuda=1920:1080" in graph.filter_complex
    punched = _scene(LayoutKind.FULL_FRAME, punch=True)
    assert not gpu_filters_suitable(punched)


def test_command_uses_hidden_style_and_encoder(tmp_path: Path) -> None:
    scene = _scene(LayoutKind.FULL_FRAME)
    graph = _graph(tmp_path, scene)
    dest = tmp_path / "out.mp4"
    cmd = build_ffmpeg_command(
        "ffmpeg",
        graph,
        dest,
        software_encoder(),
        hw=None,
        hold=1.0,
    )
    assert cmd[0] == "ffmpeg"
    assert "-filter_complex" in cmd
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "aac" in cmd
    hw = HwDecode("h264_cuvid", ("-c:v", "h264_cuvid"))
    nvenc_cmd = build_ffmpeg_command(
        "ffmpeg",
        graph,
        dest,
        nvenc_encoder(),
        hw=hw,
        hold=1.0,
    )
    assert "h264_cuvid" in nvenc_cmd
    assert "h264_nvenc" in nvenc_cmd


def test_ffmpeg_success_skips_moviepy(monkeypatch, tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    video = tmp_path / "src.mp4"
    video.write_bytes(b"vid")
    scenes = [_scene(LayoutKind.FULL_FRAME), _scene(LayoutKind.SPLIT_TOP)]
    script = EditScript(scenes=scenes)
    moviepy_calls = {"n": 0}

    def fake_ffmpeg(
        video_path: Path,
        script_obj: EditScript,
        scene: Scene,
        dest: Path,
        settings_obj: Settings,
        canvas: tuple[int, int],
    ) -> None:
        del video_path, script_obj, scene, settings_obj, canvas
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ffmpeg-scene")

    def fake_moviepy(*_args: object, **_kwargs: object) -> None:
        moviepy_calls["n"] += 1
        raise AssertionError("MoviePy should not run when ffmpeg succeeds")

    monkeypatch.setattr("pipeline.ffmpeg_scene.encode_scene_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr("pipeline.compositor._encode_one_scene", fake_moviepy)
    scene_dir = tmp_path / "scenes" / "src"
    scene_dir.mkdir(parents=True)
    parts = _encode_scenes(video, script, scenes, scene_dir, settings, (1920, 1080))
    assert moviepy_calls["n"] == 0
    assert len(parts) == 2
    for part in parts:
        assert part.read_bytes() == b"ffmpeg-scene"
    logged = capsys.readouterr().out
    assert "backend=ffmpeg" in logged
    assert "encoder=" in logged


def test_ffmpeg_fail_falls_back_moviepy(monkeypatch, tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    video = tmp_path / "src.mp4"
    video.write_bytes(b"vid")
    scenes = [_scene(LayoutKind.PIP_BOTTOM_RIGHT)]
    script = EditScript(scenes=scenes)

    def fake_ffmpeg(*_args: object, **_kwargs: object) -> None:
        raise FFmpegSceneError("rounded PIP exploded")

    def fake_moviepy(
        video_path: Path,
        script_obj: EditScript,
        scene: Scene,
        dest: Path,
        settings_obj: Settings,
        canvas: tuple[int, int],
        stills: dict,
    ) -> Path:
        del video_path, script_obj, scene, settings_obj, canvas, stills
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"moviepy-scene")
        return dest

    monkeypatch.setattr("pipeline.ffmpeg_scene.encode_scene_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr("pipeline.compositor._encode_one_scene", fake_moviepy)
    scene_dir = tmp_path / "scenes" / "src"
    scene_dir.mkdir(parents=True)
    parts = _encode_scenes(video, script, scenes, scene_dir, settings, (1920, 1080))
    assert parts[0].read_bytes() == b"moviepy-scene"
    logged = capsys.readouterr().out
    assert "backend=moviepy" in logged
    assert "MoviePy fallback" in logged


def test_encode_concurrency_cap(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path, concurrency=2)
    video = tmp_path / "src.mp4"
    video.write_bytes(b"vid")
    scenes = [
        Scene(start=float(i), end=float(i + 1), layout=LayoutKind.FULL_FRAME)
        for i in range(4)
    ]
    script = EditScript(scenes=scenes)
    current = 0
    max_seen = 0
    lock = threading.Lock()

    def fake_ffmpeg(
        video_path: Path,
        script_obj: EditScript,
        scene: Scene,
        dest: Path,
        settings_obj: Settings,
        canvas: tuple[int, int],
    ) -> None:
        del video_path, script_obj, scene, settings_obj, canvas
        nonlocal current, max_seen
        with lock:
            current += 1
            max_seen = max(max_seen, current)
        time.sleep(0.12)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")
        with lock:
            current -= 1

    monkeypatch.setattr("pipeline.ffmpeg_scene.encode_scene_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(
        "pipeline.compositor._encode_one_scene",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("moviepy")),
    )
    scene_dir = tmp_path / "scenes" / "src"
    scene_dir.mkdir(parents=True)
    parts = _encode_scenes(video, script, scenes, scene_dir, settings, (1920, 1080))
    assert len(parts) == 4
    assert max_seen == 2
    assert max_seen <= settings.encode_concurrency


def test_real_ffmpeg_encodes_pip_with_punch(tmp_path: Path, capsys) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    from tests.test_compositor import _write_slide, _write_source_video

    source = tmp_path / "talk.mp4"
    slide = tmp_path / "slide.png"
    _write_source_video(source, seconds=2)
    _write_slide(slide, kind="pip")
    settings = Settings(
        output_width=640,
        output_height=360,
        work_dir=tmp_path,
        output_dir=tmp_path,
        slides_dir=tmp_path,
        scenes_dir=tmp_path / "scenes",
        encode_concurrency=1,
    )
    scene = Scene(
        start=0.0,
        end=1.2,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        graphic=GraphicCard(title="List", asset_path=str(slide)),
        micro_events=[MicroEvent(start=0.4, end=0.9, kind="punch_in", scale=1.15)],
    )
    script = EditScript(scenes=[scene])
    scene_dir = tmp_path / "scenes" / source.stem
    scene_dir.mkdir(parents=True)
    parts = _encode_scenes(source, script, [scene], scene_dir, settings, (640, 360))
    assert parts[0].is_file()
    assert parts[0].stat().st_size > 0
    duration = probe_duration(parts[0], settings)
    assert 1.0 <= duration <= 1.4
    assert "backend=ffmpeg" in capsys.readouterr().out
