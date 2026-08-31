import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pipeline.compositor import (
    _encode_scenes,
    cover_scale,
    pip_rect,
    render_video,
    scene_cache_valid,
    scene_encode_path,
    scene_fingerprint,
    split_webcam_rect,
    write_scene_sidecar,
)
from pipeline.config import Settings
from pipeline.layouts import LayoutKind
from pipeline.media import probe_duration
from pipeline.models import EditScript, GraphicCard, MicroEvent, Scene
from pipeline.shotlist import scene_has_visual


def test_pip_rect_is_16x9_lower_right() -> None:
    x, y, box_w, box_h = pip_rect(1920, 1080, 0.25)
    assert box_w == 480
    assert box_h == 270
    assert x + box_w < 1920
    assert y + box_h < 1080
    assert x > 1920 * 0.5
    assert y > 1080 * 0.5


def test_split_rect_is_top_two_thirds() -> None:
    x, y, box_w, box_h = split_webcam_rect(1920, 1080, 2.0 / 3.0)
    assert (x, y) == (0, 0)
    assert box_w == 1920
    assert box_h == 720


def test_cover_scale_fills_taller_or_wider() -> None:
    assert cover_scale(1280, 720, 1920, 1080) == 1.5
    assert cover_scale(1920, 1080, 1920, 1080, zoom=1.15) == 1.15


def test_render_three_layouts(tmp_path: Path | None = None) -> None:
    work = Path("/tmp/yt-pipe-compositor-test")
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.mp4"
    pip_slide = work / "pip.png"
    split_slide = work / "split.png"
    output = work / "out.mp4"
    _write_source_video(source)
    _write_slide(pip_slide, kind="pip")
    _write_slide(split_slide, kind="split")

    settings = Settings(
        output_width=1920,
        output_height=1080,
        work_dir=work,
        output_dir=work,
        slides_dir=work,
        scenes_dir=work / "scenes",
    )
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=1,
                layout=LayoutKind.FULL_FRAME,
                graphic=GraphicCard(title="Talk"),
                micro_events=[MicroEvent(start=0.4, end=0.9, kind="punch_in", scale=1.15)],
            ),
            Scene(
                start=1,
                end=2,
                layout=LayoutKind.PIP_BOTTOM_RIGHT,
                asset_kind="card",
                graphic=GraphicCard(
                    kicker="List",
                    title="List",
                    bullets=["One", "Two"],
                    asset_path=str(pip_slide),
                ),
            ),
            Scene(
                start=2,
                end=3,
                layout=LayoutKind.SPLIT_TOP,
                asset_kind="card",
                graphic=GraphicCard(
                    kicker="Claim",
                    title="Claim",
                    bullets=["One", "Two"],
                    asset_path=str(split_slide),
                ),
            ),
        ]
    )
    render_video(source, script, output, settings)
    assert output.is_file()
    assert output.stat().st_size > 0
    duration = probe_duration(output, settings)
    assert 2.8 <= duration <= 3.2
    info = _probe_size(output)
    assert info == (1920, 1080)


def test_two_scenes_do_not_poison_source_audio_reader(
    tmp_path: Path, monkeypatch
) -> None:
    """Second scene must encode after the first close() (MoviePy None.stdout)."""
    from pipeline.ffmpeg_scene import FFmpegSceneError

    def _force_moviepy(*_args: object, **_kwargs: object) -> None:
        raise FFmpegSceneError("force MoviePy for audio-reader regression")

    monkeypatch.setattr("pipeline.ffmpeg_scene.encode_scene_ffmpeg", _force_moviepy)
    _require_ffmpeg()
    source = tmp_path / "source.mp4"
    # MoviePy AudioFileClip buffers ~4.5s. A 3s fixture never leaves that
    # buffer, so the closed reader is never touched. Use 8s / 3s+3s.
    _write_source_video(source, seconds=8)
    settings = Settings(
        output_width=640,
        output_height=360,
        work_dir=tmp_path,
        output_dir=tmp_path,
        slides_dir=tmp_path,
        scenes_dir=tmp_path / "scenes",
    )
    scenes = [
        Scene(
            start=0,
            end=3,
            layout=LayoutKind.FULL_FRAME,
            graphic=GraphicCard(title="Talk"),
        ),
        Scene(
            start=3,
            end=6,
            layout=LayoutKind.SPLIT_TOP,
            graphic=GraphicCard(title="Claim"),
        ),
    ]
    script = EditScript(scenes=scenes)
    canvas = (settings.output_width, settings.output_height)
    scene_dir = (settings.scenes_dir / source.stem).resolve()
    scene_dir.mkdir(parents=True, exist_ok=True)
    parts = _encode_scenes(source, script, scenes, scene_dir, settings, canvas)
    assert len(parts) == 2
    for index, (part, scene) in enumerate(zip(parts, scenes)):
        assert part.is_file()
        assert part.stat().st_size > 0
        fingerprint = scene_fingerprint(scene, settings)
        assert part == scene_encode_path(scene_dir, index, fingerprint)
        assert scene_cache_valid(part, scene, settings, fingerprint=fingerprint)

    # Resume must still skip a finished scene and encode the rest.
    parts[1].unlink()
    parts[1].with_suffix(".json").unlink(missing_ok=True)
    resumed = _encode_scenes(source, script, scenes, scene_dir, settings, canvas)
    assert resumed[0] == parts[0]
    assert resumed[1].is_file()
    assert resumed[1].stat().st_size > 0


def test_none_asset_means_no_slide_overlay(tmp_path: Path) -> None:
    _require_ffmpeg()
    source = tmp_path / "source.mp4"
    leftover = tmp_path / "leftover.png"
    _write_source_video(source, seconds=2)
    _write_slide(leftover, kind="pip")
    settings = Settings(
        output_width=640,
        output_height=360,
        work_dir=tmp_path,
        output_dir=tmp_path,
        slides_dir=tmp_path,
        scenes_dir=tmp_path / "scenes",
    )
    scene = Scene(
        start=0,
        end=2,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        asset_kind="none",
        graphic=GraphicCard(title="Empty", asset_path=str(leftover)),
    )
    script = EditScript(scenes=[scene])
    canvas = (settings.output_width, settings.output_height)
    scene_dir = (settings.scenes_dir / source.stem).resolve()
    scene_dir.mkdir(parents=True, exist_ok=True)
    parts = _encode_scenes(source, script, script.scenes, scene_dir, settings, canvas)
    assert len(parts) == 1
    assert parts[0].is_file()
    assert script.scenes[0].asset_kind == "none"
    assert script.scenes[0].layout is LayoutKind.FULL_FRAME
    assert not scene_has_visual(script.scenes[0])
    assert script.scenes[0].graphic.asset_path == ""


def test_scene_cache_skips_when_fingerprint_matches() -> None:
    work = Path("/tmp/yt-pipe-scene-resume")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(work_dir=work, output_dir=work, slides_dir=work, scenes_dir=work)
    scene = Scene(
        start=0,
        end=2,
        layout=LayoutKind.FULL_FRAME,
        graphic=GraphicCard(title="Talk"),
    )
    fingerprint = scene_fingerprint(scene, settings)
    dest = scene_encode_path(work, 0, fingerprint)
    dest.write_bytes(b"not-empty-scene")
    write_scene_sidecar(dest, scene, fingerprint)
    assert scene_cache_valid(dest, scene, settings, fingerprint=fingerprint)
    changed = scene.model_copy(update={"end": 3.0})
    assert not scene_cache_valid(dest, changed, settings)


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")


def _write_source_video(path: Path, seconds: float = 3) -> None:
    frame = work_frame()
    png = path.with_suffix(".png")
    frame.save(png)
    import subprocess

    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(png),
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-t",
        str(seconds),
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


def work_frame() -> Image.Image:
    img = Image.new("RGB", (1280, 720), (40, 90, 160))
    draw = ImageDraw.Draw(img)
    draw.ellipse((440, 120, 840, 520), fill=(240, 200, 160))
    draw.rectangle((0, 0, 80, 80), fill=(255, 0, 0))
    draw.rectangle((1200, 640, 1280, 720), fill=(0, 255, 0))
    return img


def _write_slide(path: Path, *, kind: str) -> None:
    img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0) if kind == "split" else (11, 16, 22, 255))
    draw = ImageDraw.Draw(img)
    if kind == "split":
        draw.rectangle((0, 720, 1920, 1080), fill=(11, 16, 22, 255))
        draw.rectangle((0, 720, 1920, 726), fill=(56, 189, 248, 255))
        draw.text((96, 780), "SPLIT CARD", fill=(255, 255, 255, 255))
    else:
        draw.text((96, 96), "PIP SLIDE", fill=(255, 255, 255, 255))
        draw.rectangle((1360, 760, 1880, 1040), fill=(7, 10, 14, 255))
    img.save(path)


def _probe_size(path: Path) -> tuple[int, int]:
    import json
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])
