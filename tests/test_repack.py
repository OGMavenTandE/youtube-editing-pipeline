import inspect
import sys
from pathlib import Path

from pipeline.config import Settings
from pipeline.media import write_json
from pipeline.models import ChapterMarker, YouTubeMetadata
from pipeline.repack import find_webcam_path, infer_stem, repack_studio
from pipeline.studio import parse_titles_file, write_studio_package
from run import main


def test_repack_keeps_description_and_chapters(monkeypatch) -> None:
    work = Path("/tmp/yt-pipe-repack-keep")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        work_dir=work / "work",
        output_dir=work / "output",
        input_dir=work / "input",
        slides_dir=work / "work" / "slides",
    )
    settings.ensure_dirs()
    video = settings.output_dir / "talk_final.mp4"
    trimmed = settings.work_dir / "talk_trimmed.mp4"
    _write_source_video(video, seconds=2)
    _write_source_video(trimmed, seconds=2)
    meta = YouTubeMetadata(
        titles=["One", "Two", "Three", "Four", "Five"],
        description="Hook body.\n\nMore SEO.",
        chapters=[
            ChapterMarker(start=0, title="Intro"),
            ChapterMarker(start=45, title="Setup"),
            ChapterMarker(start=120, title="Payoff"),
        ],
        tags=["edit", "talk"],
    )
    write_json(settings.output_dir / "talk_youtube_metadata.json", meta.model_dump())
    monkeypatch.setattr("pipeline.studio.render_studio_thumbnail", _fake_thumb)

    first = write_studio_package(
        video_path=video,
        webcam_path=trimmed,
        metadata=meta,
        dest_dir=settings.output_dir / "talk_studio",
        settings=settings,
        fallback_title="talk",
        duration=180.0,
        title_index=0,
    )
    original_description = first.description_path.read_text(encoding="utf-8")
    original_tags = first.tags_path.read_text(encoding="utf-8")

    package = repack_studio(
        settings.output_dir / "talk_studio",
        settings,
        title_index=3,
    )
    titles, selected = parse_titles_file(package.titles_path.read_text(encoding="utf-8"))
    assert selected == 3
    assert titles[0] == "Four"
    assert set(titles) == {"One", "Two", "Three", "Four", "Five"}
    assert package.paste_title == "Four"
    assert package.description_path.read_text(encoding="utf-8") == original_description
    assert "Hook body." in original_description
    assert "0:00 Intro" in original_description
    assert "0:45 Setup" in original_description
    assert "2:00 Payoff" in original_description
    assert package.tags_path.read_text(encoding="utf-8") == original_tags
    assert package.video_path.is_file()


def test_repack_prefers_trimmed_webcam() -> None:
    work = Path("/tmp/yt-pipe-repack-webcam")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        work_dir=work / "work",
        output_dir=work / "output",
        input_dir=work / "input",
        slides_dir=work / "work" / "slides",
    )
    settings.ensure_dirs()
    original = settings.input_dir / "talk.mp4"
    trimmed = settings.work_dir / "talk_trimmed.mp4"
    _write_source_video(original, seconds=1)
    _write_source_video(trimmed, seconds=1)
    found = find_webcam_path("talk", settings, extra=original)
    assert found == trimmed.resolve()


def test_repack_fails_without_webcam_frame_source() -> None:
    work = Path("/tmp/yt-pipe-repack-missing")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        work_dir=work / "work",
        output_dir=work / "output",
        input_dir=work / "input",
        slides_dir=work / "work" / "slides",
    )
    settings.ensure_dirs()
    try:
        find_webcam_path("ghost", settings)
    except FileNotFoundError as exc:
        message = str(exc)
        assert "black frame" in message
        assert "ghost" in message
    else:
        raise AssertionError("expected FileNotFoundError")


def test_infer_stem_from_studio_folder_and_final() -> None:
    work = Path("/tmp/yt-pipe-repack-stem")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        work_dir=work / "work",
        output_dir=work / "output",
        input_dir=work / "input",
        slides_dir=work / "work" / "slides",
    )
    settings.ensure_dirs()
    studio = settings.output_dir / "raw_talk_studio"
    studio.mkdir(parents=True, exist_ok=True)
    (studio / "titles.txt").write_text("selected: 0\nOne\n", encoding="utf-8")
    stem, dest = infer_stem(studio, settings)
    assert stem == "raw_talk"
    assert dest == studio.resolve()
    stem, dest = infer_stem("raw_talk", settings)
    assert stem == "raw_talk"
    assert dest == studio.resolve()


def test_cli_repack_skips_moviepy(monkeypatch) -> None:
    work = Path("/tmp/yt-pipe-repack-cli")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        work_dir=work / "work",
        output_dir=work / "output",
        input_dir=work / "input",
        slides_dir=work / "work" / "slides",
    )
    settings.ensure_dirs()
    video = settings.output_dir / "phone_final.mp4"
    trimmed = settings.work_dir / "phone_trimmed.mp4"
    studio = settings.output_dir / "phone_studio"
    _write_source_video(video, seconds=2)
    _write_source_video(trimmed, seconds=2)
    meta = YouTubeMetadata(
        titles=["One", "Two", "Three", "Four", "Five"],
        description="Keep this body.",
        chapters=[
            ChapterMarker(start=0, title="Open"),
            ChapterMarker(start=40, title="Work"),
            ChapterMarker(start=80, title="Close"),
        ],
        tags=["phone"],
    )
    write_json(settings.output_dir / "phone_youtube_metadata.json", meta.model_dump())
    monkeypatch.setattr("pipeline.studio.render_studio_thumbnail", _fake_thumb)
    write_studio_package(
        video_path=video,
        webcam_path=trimmed,
        metadata=meta,
        dest_dir=studio,
        settings=settings,
        fallback_title="phone",
        duration=180.0,
        title_index=0,
    )

    monkeypatch.setattr("run.load_settings", lambda: settings)
    sys.modules.pop("pipeline.compositor", None)
    sys.modules.pop("moviepy", None)
    code = main(["--repack-studio", str(studio), "--title-index", "4"])
    assert code == 0
    assert "pipeline.compositor" not in sys.modules
    titles, selected = parse_titles_file((studio / "titles.txt").read_text(encoding="utf-8"))
    assert selected == 4
    assert titles[0] == "Five"
    assert "Keep this body." in (studio / "description.txt").read_text(encoding="utf-8")
    assert "0:00 Open" in (studio / "description.txt").read_text(encoding="utf-8")


def test_ui_rewrite_calls_write_studio_package() -> None:
    from pipeline import ui
    from pipeline.repack import repack_studio

    assert callable(ui.main)
    assert "write_studio_package" in inspect.getsource(ui.render_review_page)
    assert "write_studio_package" in inspect.getsource(repack_studio)
    assert "render_video" not in inspect.getsource(ui)
    assert "analyze_video" not in inspect.getsource(ui)


def _fake_thumb(title, webcam_path, dest, settings, duration=None):
    dest.write_bytes(b"fake-thumb")
    return dest


def _write_source_video(path: Path, *, seconds: int) -> None:
    from PIL import Image, ImageDraw
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = Image.new("RGB", (640, 360), (40, 90, 160))
    draw = ImageDraw.Draw(frame)
    draw.ellipse((180, 40, 460, 320), fill=(240, 200, 160))
    png = path.with_suffix(".png")
    frame.save(png)
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
