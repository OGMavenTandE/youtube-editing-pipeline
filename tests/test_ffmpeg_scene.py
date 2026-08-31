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
from pipeline.encoder import (
    HQ_X264_CRF,
    HQ_X264_PRESET,
    nvenc_encoder,
    picture_encode_args_are_hq,
    software_encoder,
)
from pipeline.media import probe_video_stream
from pipeline.hwaccel import HwDecode
from pipeline.layouts import PictureTag, pip_rect
from pipeline.models import EditScript, GraphicCard, Scene


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


def _scene(layout: PictureTag, *, graphic: str = "", kicker: str = "THE MONEY") -> Scene:
    return Scene(
        start=0.0,
        end=1.0,
        layout=layout,
        graphic=GraphicCard(kicker=kicker, title="Card", asset_path=graphic),
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
        layout=PictureTag.NOTHING,
        graphic=GraphicCard(title="Empty", asset_path=str(leftover)),
    )
    resolve_scene(scene)
    assert _graphic_path(scene) is None
    assert scene.layout is PictureTag.NOTHING


def test_pip_still_is_ffmpeg_graphic(tmp_path: Path) -> None:
    still = tmp_path / "dvids.jpg"
    still.write_bytes(b"jpg")
    scene = Scene(
        start=0,
        end=1,
        layout=PictureTag.PIP,
        asset_ref=str(still),
        graphic=GraphicCard(kicker="$1.5B", title="in procurements", still_query="DVIDS"),
    )
    resolve_scene(scene)
    graphic = _graphic_path(scene)
    assert graphic is not None
    assert graphic == still.resolve()
    assert scene.layout is PictureTag.PIP
    mask = tmp_path / "mask.png"
    border = tmp_path / "border.png"
    mask.write_bytes(b"m")
    border.write_bytes(b"b")
    graph = _graph(tmp_path, scene, graphic=graphic, mask=mask, border=border)
    assert graph.layout is PictureTag.PIP
    assert "[1:v]" in graph.filter_complex


def test_cover_filter_scales_then_crops() -> None:
    filt = cover_filter(1920, 1080)
    assert "scale=1920:1080:force_original_aspect_ratio=increase" in filt
    assert "flags=lanczos" in filt
    assert "crop=1920:1080" in filt
    zoomed = cover_filter(1920, 1080, 1.15)
    assert "scale=2208:1242" in zoomed
    assert "crop=1920:1080" in zoomed


def test_cover_filter_is_noop_when_source_matches_canvas() -> None:
    filt = cover_filter(1920, 1080, src_w=1920, src_h=1080)
    assert filt == "setsar=1"
    assert "scale=" not in filt
    assert "fps=" not in filt


def test_nothing_graph_covers_host(tmp_path: Path) -> None:
    scene = _scene(PictureTag.NOTHING)
    graph = _graph(tmp_path, scene)
    assert graph.layout is PictureTag.NOTHING
    assert not graph.uses_gpu_filters
    assert "crop=1920:1080" in graph.filter_complex
    assert "fps=" not in graph.filter_complex
    assert "punch" not in graph.filter_complex
    assert graph.inputs[0].args[:2] == ("-ss", "0.000")
    assert graph.video_map == "vout"


def test_nothing_graph_skips_scale_when_source_matches(tmp_path: Path) -> None:
    scene = _scene(PictureTag.NOTHING)
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"x")
    graph = build_scene_graph(
        video_path=video,
        scene=scene,
        settings=_settings(tmp_path),
        canvas=(1920, 1080),
        fps=30.0,
        graphic=None,
        overlays=(),
        mask=None,
        border=None,
        src_size=(1920, 1080),
    )
    assert "scale=" not in graph.filter_complex
    assert "fps=" not in graph.filter_complex
    assert "setsar=1" in graph.filter_complex


def test_pip_graph_mask_border_and_fit(tmp_path: Path) -> None:
    slide = tmp_path / "slide.png"
    slide.write_bytes(b"png")
    mask = tmp_path / "mask.png"
    border = tmp_path / "border.png"
    mask.write_bytes(b"m")
    border.write_bytes(b"b")
    scene = _scene(PictureTag.PIP, graphic=str(slide))
    graph = _graph(tmp_path, scene, graphic=slide, mask=mask, border=border)
    x, y, box_w, box_h = pip_rect(1920, 1080)
    assert "alphamerge" in graph.filter_complex
    assert f"force_original_aspect_ratio=decrease" in graph.filter_complex
    assert f"pad={box_w}:{box_h}" in graph.filter_complex
    assert f"overlay={x}:{y}" in graph.filter_complex
    assert str(mask) in {item.path for item in graph.inputs}
    assert str(border) in {item.path for item in graph.inputs}


def test_overlay_graph_is_full_frame_plus_png(tmp_path: Path) -> None:
    scene = _scene(PictureTag.OVERLAY)
    overlay = tmp_path / "kit.png"
    overlay.write_bytes(b"png")
    graph = _graph(
        tmp_path,
        scene,
        overlays=(OverlayLayer(overlay, 0, 0, 0.0, 1.0),),
    )
    assert "crop=1920:1080" in graph.filter_complex
    assert "overlay=0:0" in graph.filter_complex
    assert "[1:v]scale=1920:1080" not in graph.filter_complex


def test_gpu_graph_only_for_plain_nothing(tmp_path: Path) -> None:
    scene = _scene(PictureTag.NOTHING)
    assert gpu_filters_suitable(scene)
    graph = _graph(tmp_path, scene, use_gpu_filters=True)
    assert graph.uses_gpu_filters
    assert "scale_cuda=1920:1080" in graph.filter_complex
    chrome = _scene(PictureTag.OVERLAY)
    assert not gpu_filters_suitable(chrome)


def test_command_uses_hidden_style_and_encoder(tmp_path: Path) -> None:
    scene = _scene(PictureTag.NOTHING)
    graph = _graph(tmp_path, scene)
    dest = tmp_path / "out.mp4"
    cmd = build_ffmpeg_command(
        "ffmpeg",
        graph,
        dest,
        software_encoder(),
        hw=None,
        hold=1.0,
        fps=30.0,
    )
    assert cmd[0] == "ffmpeg"
    assert "-filter_complex" in cmd
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-preset") + 1] == HQ_X264_PRESET
    assert cmd[cmd.index("-crf") + 1] == str(HQ_X264_CRF)
    assert cmd[cmd.index("-r") + 1] == "30"
    assert "aac" in cmd
    assert "fps=" not in cmd[cmd.index("-filter_complex") + 1]
    assert picture_encode_args_are_hq(cmd)
    hw = HwDecode("h264_cuvid", ("-c:v", "h264_cuvid"))
    nvenc_cmd = build_ffmpeg_command(
        "ffmpeg",
        graph,
        dest,
        nvenc_encoder(),
        hw=hw,
        hold=1.0,
        fps=30.0,
    )
    assert "h264_cuvid" in nvenc_cmd
    assert "h264_nvenc" in nvenc_cmd
    assert nvenc_cmd[nvenc_cmd.index("-r") + 1] == "30"
    assert picture_encode_args_are_hq(nvenc_cmd)


def test_ffmpeg_success_skips_moviepy(monkeypatch, tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    video = tmp_path / "src.mp4"
    video.write_bytes(b"vid")
    scenes = [_scene(PictureTag.NOTHING), _scene(PictureTag.OVERLAY)]
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
    scenes = [_scene(PictureTag.PIP)]
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
        Scene(start=float(i), end=float(i + 1), layout=PictureTag.NOTHING)
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


def test_real_ffmpeg_encodes_pip(tmp_path: Path, capsys) -> None:
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
        layout=PictureTag.PIP,
        graphic=GraphicCard(kicker="$1.5B", title="in procurements", asset_path=str(slide)),
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


def test_gpu_graph_skips_scale_when_source_matches(tmp_path: Path) -> None:
    scene = _scene(PictureTag.NOTHING)
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"x")
    graph = build_scene_graph(
        video_path=video,
        scene=scene,
        settings=_settings(tmp_path),
        canvas=(1920, 1080),
        fps=30.0,
        graphic=None,
        overlays=(),
        mask=None,
        border=None,
        use_gpu_filters=True,
        src_size=(1920, 1080),
    )
    assert graph.uses_gpu_filters
    assert "scale_cuda=" not in graph.filter_complex
    assert "fps=" not in graph.filter_complex


def _write_1080p30(path: Path, seconds: float = 1.0) -> None:
    import subprocess

    from PIL import Image, ImageDraw

    png = path.with_suffix(".png")
    img = Image.new("RGB", (1920, 1080), (40, 90, 160))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 80, 80), fill=(255, 0, 0))
    draw.rectangle((1840, 1000, 1920, 1080), fill=(0, 255, 0))
    img.save(png)
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "30",
        "-i",
        str(png),
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-t",
        str(seconds),
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def test_real_ffmpeg_keeps_1080p30_and_hq_args(monkeypatch, tmp_path: Path, capsys) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    source = tmp_path / "talk.mp4"
    _write_1080p30(source, seconds=1.0)
    src_w, src_h, src_fps = probe_video_stream(source, _settings(tmp_path))
    assert (src_w, src_h) == (1920, 1080)
    assert abs(src_fps - 30.0) < 0.05
    settings = _settings(tmp_path, concurrency=1)
    scene = Scene(start=0.0, end=1.0, layout=PictureTag.NOTHING)
    script = EditScript(scenes=[scene])
    scene_dir = tmp_path / "scenes" / source.stem
    scene_dir.mkdir(parents=True)
    seen: list[list[str]] = []
    import pipeline.media as media

    real_run = media._run

    def spy_run(cmd: list[str], label: str) -> None:
        seen.append(list(cmd))
        real_run(cmd, label)

    monkeypatch.setattr("pipeline.media._run", spy_run)
    parts = _encode_scenes(source, script, [scene], scene_dir, settings, (1920, 1080))
    assert parts[0].is_file()
    out_w, out_h, out_fps = probe_video_stream(parts[0], settings)
    assert (out_w, out_h) == (1920, 1080)
    assert abs(out_fps - 30.0) < 0.05
    assert seen, "expected an ffmpeg encode command"
    encode_cmds = [cmd for cmd in seen if "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] != "copy"]
    assert encode_cmds
    for cmd in encode_cmds:
        assert picture_encode_args_are_hq(cmd)
        graph = cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""
        assert "fps=" not in graph
        assert cmd[cmd.index("-r") + 1] == "30"
    assert "backend=ffmpeg" in capsys.readouterr().out
