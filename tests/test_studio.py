from argparse import Namespace
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.config import Settings
from pipeline.media import write_json
from pipeline.models import ChapterMarker, EditScript, YouTubeMetadata
from pipeline.gemini_director import GENERIC_FILLER_TAGS, sanitize_chapters
from pipeline.studio import (
    DESCRIPTION_CHAR_LIMIT,
    TITLE_CHAR_LIMIT,
    assemble_description,
    build_studio_texts,
    clip_title,
    format_chapter_block,
    format_chapter_timestamp,
    format_tags_file,
    format_titles_file,
    parse_titles_file,
    select_title,
    write_studio_package,
)
from run import build_parser, run_pipeline


def test_timestamp_under_hour_is_m_ss() -> None:
    assert format_chapter_timestamp(0) == "0:00"
    assert format_chapter_timestamp(5) == "0:05"
    assert format_chapter_timestamp(65) == "1:05"
    assert format_chapter_timestamp(599) == "9:59"
    assert format_chapter_timestamp(3599) == "59:59"


def test_timestamp_at_and_over_hour_is_h_mm_ss() -> None:
    assert format_chapter_timestamp(3600) == "1:00:00"
    assert format_chapter_timestamp(3661) == "1:01:01"
    assert format_chapter_timestamp(7325) == "2:02:05"
    assert format_chapter_timestamp(36000) == "10:00:00"


def test_sanitize_forces_first_chapter_to_zero() -> None:
    chapters = sanitize_chapters(
        [
            ChapterMarker(start=12, title="Late start"),
            ChapterMarker(start=40, title="Middle"),
            ChapterMarker(start=80, title="Close"),
        ],
        120.0,
    )
    assert chapters[0].start == 0.0
    assert chapters[0].title == "Late start"
    assert format_chapter_block(chapters).startswith("0:00 Late start")


def test_sanitize_drops_sub_10s_chapters() -> None:
    chapters = sanitize_chapters(
        [
            ChapterMarker(start=0, title="Intro"),
            ChapterMarker(start=5, title="Too soon"),
            ChapterMarker(start=8, title="Also soon"),
            ChapterMarker(start=40, title="Setup"),
            ChapterMarker(start=80, title="Payoff"),
        ],
        120.0,
    )
    starts = [chapter.start for chapter in chapters]
    assert starts == [0.0, 40.0, 80.0]


def test_sanitize_pads_to_three_when_gemini_under_delivers() -> None:
    chapters = sanitize_chapters(
        [ChapterMarker(start=0, title="Only one")],
        180.0,
    )
    assert len(chapters) == 3
    assert chapters[0].start == 0.0
    assert chapters[0].title == "Only one"
    assert chapters[1].start >= 10
    assert chapters[2].start - chapters[1].start >= 10
    assert 180.0 - chapters[2].start >= 10


def test_description_is_body_then_chapters() -> None:
    text = assemble_description(
        "Hook line.\n\nWhy this cut works.",
        [
            ChapterMarker(start=0, title="Intro"),
            ChapterMarker(start=45, title="Setup"),
            ChapterMarker(start=120, title="Payoff"),
        ],
    )
    assert text.startswith("Hook line.")
    assert "Why this cut works." in text
    assert "0:00 Intro" in text
    assert "0:45 Setup" in text
    assert "2:00 Payoff" in text
    assert text.index("Hook line.") < text.index("0:00 Intro")
    assert "How to edit" not in text


def test_description_strips_gemini_chapter_tail_only() -> None:
    body = (
        "Hook first.\n\n"
        "1:20 is when we cut, keep that sentence.\n\n"
        "More copy.\n\n"
        "Chapters:\n"
        "0:00 Intro\n"
        "1:20 Middle\n"
        "3:00 End"
    )
    text = assemble_description(
        body,
        [
            ChapterMarker(start=0, title="Open"),
            ChapterMarker(start=90, title="Work"),
            ChapterMarker(start=150, title="Close"),
        ],
    )
    assert "Hook first." in text
    assert "1:20 is when we cut, keep that sentence." in text
    assert "More copy." in text
    assert "1:20 Middle" not in text
    assert "0:00 Intro" not in text
    assert "0:00 Open" in text
    assert "1:30 Work" in text
    assert text.count("0:00") == 1


def test_description_stays_under_5000() -> None:
    chapters = [
        ChapterMarker(start=0, title="Intro"),
        ChapterMarker(start=60, title="Middle"),
        ChapterMarker(start=120, title="End"),
    ]
    text = assemble_description("x" * 6000, chapters)
    assert len(text) <= DESCRIPTION_CHAR_LIMIT
    assert "0:00 Intro" in text
    assert text.endswith("2:00 End")


def test_titles_file_clips_to_100_and_keeps_five_lines() -> None:
    long_title = "A" * 150
    titles = [long_title, "Second", "Third", "Fourth", "Fifth extra words"]
    pasted = format_titles_file(titles)
    titles_in_file, selected = parse_titles_file(pasted)
    assert selected == 0
    assert len(titles_in_file) == 5
    assert titles_in_file[0] == "A" * TITLE_CHAR_LIMIT
    assert titles_in_file[1] == "Second"
    assert clip_title(long_title) == "A" * 100
    assert pasted.splitlines()[0] == "A" * TITLE_CHAR_LIMIT
    assert "selected: 0" in pasted


def test_build_studio_texts_does_not_put_all_titles_in_description() -> None:
    meta = YouTubeMetadata(
        titles=["Default hook title", "Angle two", "Angle three", "Angle four", "Angle five"],
        description="SEO body only.",
        chapters=[
            ChapterMarker(start=0, title="Intro"),
            ChapterMarker(start=40, title="Body"),
            ChapterMarker(start=80, title="Out"),
        ],
        tags=["edit", "talking head"],
    )
    titles, description, tags, chapters, paste_title = build_studio_texts(meta, 120.0)
    assert titles[0] == "Default hook title"
    assert paste_title == "Default hook title"
    assert "Angle two" not in description
    assert "Angle five" not in description
    assert "SEO body only." in description
    assert tags == ["edit"]
    assert "talking head" not in tags
    assert format_tags_file(["edit", "talking head"]) == "edit\n"
    assert chapters[0].start == 0.0


def test_title_index_selects_paste_title() -> None:
    meta = YouTubeMetadata(
        titles=["One", "Two", "Three", "Four", "Five"],
        description="Body only.",
        chapters=[
            ChapterMarker(start=0, title="Intro"),
            ChapterMarker(start=40, title="Body"),
            ChapterMarker(start=80, title="Out"),
        ],
        tags=["edit"],
    )
    titles, description, _tags, _chapters, paste_title = build_studio_texts(
        meta, 120.0, title_index=2
    )
    assert titles == ["One", "Two", "Three", "Four", "Five"]
    assert paste_title == "Three"
    assert select_title(titles, 4) == "Five"
    assert "Body only." in description
    pasted = format_titles_file(titles, title_index=2)
    file_titles, selected = parse_titles_file(pasted)
    assert selected == 2
    assert file_titles[0] == "Three"
    assert set(file_titles) == set(titles)


def test_filler_tags_kept_when_they_are_the_only_tags() -> None:
    meta = YouTubeMetadata(
        titles=["One", "Two", "Three", "Four", "Five"],
        tags=["youtube", "tutorial"],
    )
    _titles, _description, tags, _chapters, _paste = build_studio_texts(meta, 120.0)
    assert "youtube" in tags
    assert "tutorial" in tags
    assert all(tag.casefold() in GENERIC_FILLER_TAGS for tag in tags)


def test_help_lists_skip_studio() -> None:
    help_text = build_parser().format_help()
    assert "--skip-studio" in help_text
    assert "--title-index" in help_text
    assert "--repack-studio" in help_text
    assert "Studio" in help_text


def test_repack_flag_does_not_require_input() -> None:
    args = build_parser().parse_args(
        ["--repack-studio", "output/talk_studio", "--title-index", "2"]
    )
    assert args.repack_studio == "output/talk_studio"
    assert args.title_index == 2
    assert args.input is None


def test_write_studio_package_folder(tmp_path: Path | None = None) -> None:
    work = Path("/tmp/yt-pipe-studio-test")
    work.mkdir(parents=True, exist_ok=True)
    video = work / "talk_final.mp4"
    _write_source_video(video, seconds=3)
    settings = Settings(work_dir=work, output_dir=work, slides_dir=work / "slides")
    meta = YouTubeMetadata(
        titles=["Cut the pause keep the point " + ("extra " * 30), "B", "C", "D", "E"],
        description="Hook.\n\nBody copy.\n\n0:00 Bad gemini chapter\n0:05 Too soon",
        chapters=[
            ChapterMarker(start=4, title="Late"),
            ChapterMarker(start=8, title="Close"),
        ],
        tags=["youtube", "edit"],
    )
    package = write_studio_package(
        video_path=video,
        webcam_path=video,
        metadata=meta,
        dest_dir=work / "talk_studio",
        settings=settings,
        fallback_title="talk",
        duration=180.0,
        title_index=1,
    )
    assert package.directory.is_dir()
    assert package.video_path.is_file()
    assert package.video_path.stat().st_size == video.stat().st_size
    titles, selected = parse_titles_file(package.titles_path.read_text(encoding="utf-8"))
    assert selected == 1
    assert len(titles) == 5
    assert titles[0] == "B"
    assert len(titles[0]) <= 100
    description = package.description_path.read_text(encoding="utf-8")
    assert description.startswith("Hook.")
    assert "0:00 Late" in description
    assert "0:05 Too soon" not in description
    assert "0:00 Bad gemini chapter" not in description
    assert package.paste_title == "B"
    assert package.title_index == 1
    pasted_tags = package.tags_path.read_text(encoding="utf-8")
    assert "edit" in pasted_tags
    assert "youtube" not in pasted_tags
    thumb = Image.open(package.thumbnail_path)
    assert thumb.size == (1280, 720)
    assert package.thumbnail_path.stat().st_size < 2 * 1024 * 1024
    assert package.thumbnail_path.suffix == ".jpg"


def test_write_studio_package_title_index_drives_paste_and_thumbnail(monkeypatch) -> None:
    work = Path("/tmp/yt-pipe-studio-title-index")
    work.mkdir(parents=True, exist_ok=True)
    video = work / "pick_final.mp4"
    _write_source_video(video, seconds=2)
    seen: dict[str, str] = {}

    def fake_render(title, webcam_path, dest, settings, duration=None):
        seen["title"] = title
        dest.write_bytes(b"fake-thumb")
        return dest

    monkeypatch.setattr("pipeline.studio.render_studio_thumbnail", fake_render)
    settings = Settings(work_dir=work, output_dir=work, slides_dir=work / "slides")
    meta = YouTubeMetadata(
        titles=["Alpha hook", "Bravo angle", "Charlie take", "Delta line", "Echo close"],
        description="SEO body stays put.",
        chapters=[
            ChapterMarker(start=0, title="Open"),
            ChapterMarker(start=40, title="Work"),
            ChapterMarker(start=80, title="Close"),
        ],
        tags=["talk"],
    )
    package = write_studio_package(
        video_path=video,
        webcam_path=video,
        metadata=meta,
        dest_dir=work / "pick_studio",
        settings=settings,
        fallback_title="pick",
        duration=180.0,
        title_index=2,
    )
    titles, selected = parse_titles_file(package.titles_path.read_text(encoding="utf-8"))
    assert selected == 2
    assert titles[0] == "Charlie take"
    assert set(titles) == {
        "Alpha hook",
        "Bravo angle",
        "Charlie take",
        "Delta line",
        "Echo close",
    }
    assert package.paste_title == "Charlie take"
    assert package.title_index == 2
    assert seen["title"] == "Charlie take"
    description = package.description_path.read_text(encoding="utf-8")
    assert "SEO body stays put." in description
    assert "0:00 Open" in description
    assert "Charlie take" not in description


def test_pipeline_skip_gemini_writes_studio_folder() -> None:
    work = Path("/tmp/yt-pipe-studio-run")
    work.mkdir(parents=True, exist_ok=True)
    source = work / "raw_talk.mp4"
    _write_source_video(source, seconds=3)
    script_path = work / "raw_talk_edit_script.json"
    write_json(
        script_path,
        EditScript(
            metadata=YouTubeMetadata(
                titles=["One", "Two", "Three", "Four", "Five"],
                description="Paste this body.",
                chapters=[
                    ChapterMarker(start=0, title="Open"),
                    ChapterMarker(start=40, title="Work"),
                    ChapterMarker(start=80, title="Close"),
                ],
                tags=["talk"],
            )
        ).model_dump(),
    )
    settings = Settings(work_dir=work, output_dir=work, slides_dir=work / "slides")
    args = Namespace(
        input=str(source),
        output=str(work / "raw_talk_final.mp4"),
        skip_silence=True,
        skip_gemini=True,
        edit_script=str(script_path),
        transcript=None,
        broll_dir=None,
        auto_editor=False,
        skip_slides=True,
        skip_studio=False,
        title_index=0,
    )
    run_pipeline(args, settings)
    studio = work / "raw_talk_studio"
    assert studio.is_dir()
    assert (studio / "raw_talk_final.mp4").is_file()
    assert (studio / "titles.txt").read_text(encoding="utf-8").splitlines()[0] == "One"
    assert "Paste this body." in (studio / "description.txt").read_text(encoding="utf-8")
    pasted_tags = (studio / "tags.txt").read_text(encoding="utf-8")
    assert "talk" in pasted_tags
    assert "youtube" not in pasted_tags
    assert (studio / "thumbnail.jpg").is_file()
    assert (work / "raw_talk_youtube_metadata.json").is_file()


def test_skip_studio_leaves_no_folder() -> None:
    work = Path("/tmp/yt-pipe-studio-skip")
    work.mkdir(parents=True, exist_ok=True)
    source = work / "skip_talk.mp4"
    _write_source_video(source, seconds=3)
    settings = Settings(work_dir=work, output_dir=work, slides_dir=work / "slides")
    args = Namespace(
        input=str(source),
        output=str(work / "skip_talk_final.mp4"),
        skip_silence=True,
        skip_gemini=True,
        edit_script=None,
        transcript=None,
        broll_dir=None,
        auto_editor=False,
        skip_slides=True,
        skip_studio=True,
        title_index=0,
    )
    run_pipeline(args, settings)
    assert not (work / "skip_talk_studio").exists()
    assert (work / "skip_talk_final.mp4").is_file()


def _write_source_video(path: Path, *, seconds: int) -> None:
    frame = Image.new("RGB", (1280, 720), (40, 90, 160))
    draw = ImageDraw.Draw(frame)
    draw.ellipse((440, 120, 840, 520), fill=(240, 200, 160))
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
