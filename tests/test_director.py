from pathlib import Path

from pipeline.config import Settings
from pipeline.gemini_director import (
    GeminiConfigError,
    analyze_video,
    director_windows,
    fit_scenes_to_window,
    normalize_youtube_metadata,
    parse_transcript,
    stitch_director_plans,
)
from pipeline.layouts import LayoutKind
from pipeline.models import (
    ChapterMarker,
    DirectorPlan,
    PlannedScene,
    TimedTranscript,
    TranscriptCue,
    YouTubeMetadata,
)


def test_director_windows_stay_single_under_threshold() -> None:
    settings = Settings()
    assert director_windows(479, settings) == [(0.0, 479.0)]
    assert director_windows(480, settings) == [(0.0, 480.0)]


def test_director_windows_split_after_threshold() -> None:
    settings = Settings()
    windows = director_windows(1200, settings)
    assert windows == [(0.0, 300.0), (300.0, 600.0), (600.0, 900.0), (900.0, 1200.0)]


def test_fit_scenes_shifts_relative_timestamps() -> None:
    scenes = [
        PlannedScene(start=0, end=12, layout=LayoutKind.FULL_FRAME, reason="open"),
        PlannedScene(start=12, end=30, layout=LayoutKind.SPLIT_TOP, reason="claim"),
    ]
    fitted = fit_scenes_to_window(scenes, 300, 600)
    assert fitted[0].start == 300
    assert fitted[0].end == 312
    assert fitted[1].start == 312
    assert fitted[1].end == 330


def test_fit_scenes_keeps_absolute_timestamps() -> None:
    scenes = [
        PlannedScene(start=300, end=318, layout=LayoutKind.PIP_BOTTOM_RIGHT, reason="list"),
        PlannedScene(start=318, end=340, layout=LayoutKind.FULL_FRAME, reason="aside"),
    ]
    fitted = fit_scenes_to_window(scenes, 300, 600)
    assert fitted[0].start == 300
    assert fitted[1].end == 340


def test_stitch_uses_first_window_metadata() -> None:
    plans = [
        DirectorPlan(
            scenes=[PlannedScene(start=0, end=20, reason="a")],
            metadata=YouTubeMetadata(titles=["How to cut talking-head footage"]),
        ),
        DirectorPlan(
            scenes=[PlannedScene(start=0, end=20, reason="b")],
            metadata=YouTubeMetadata(titles=["Ignore me"]),
        ),
    ]
    stitched = stitch_director_plans(plans, [(0.0, 300.0), (300.0, 600.0)], 600)
    assert stitched.metadata.titles[0] == "How to cut talking-head footage"
    assert stitched.scenes[0].start == 0
    assert stitched.scenes[1].start == 300
    assert stitched.scenes[1].end == 320


def test_normalize_fills_titles_chapters_and_tags() -> None:
    meta = normalize_youtube_metadata(
        YouTubeMetadata(titles=["Same", "same"], tags=["edit"]),
        180.0,
        fallback_title="Camera talk",
    )
    assert len(meta.titles) == 5
    assert meta.titles[0] == "Same"
    assert meta.chapters[0].start == 0.0
    assert len(meta.chapters) >= 3
    assert meta.chapters[1].start - meta.chapters[0].start >= 10
    assert 10 <= len(meta.tags) <= 15
    assert "edit" in meta.tags
    assert meta.description.startswith("Same")


def test_normalize_drops_chapters_closer_than_10s() -> None:
    meta = normalize_youtube_metadata(
        YouTubeMetadata(
            titles=["A", "B", "C", "D", "E"],
            chapters=[
                ChapterMarker(start=3, title="Intro"),
                ChapterMarker(start=8, title="Too soon"),
                ChapterMarker(start=95, title="Middle"),
                ChapterMarker(start=170, title="Close"),
            ],
            tags=["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"],
        ),
        180.0,
        fallback_title="Talk",
    )
    assert meta.chapters[0].start == 0.0
    starts = [chapter.start for chapter in meta.chapters]
    assert starts == [0.0, 95.0, 170.0]


def test_parse_transcript_json_and_plain() -> None:
    timed = parse_transcript(
        '{"text": "Hello there.", "cues": [{"start": 0, "end": 2, "text": "Hello there."}]}',
        duration=2.0,
    )
    assert timed.text == "Hello there."
    assert timed.cues[0].end == 2.0
    plain = parse_transcript("Just words", duration=10)
    assert plain.text == "Just words"
    assert plain.cues[0].end == 10


def test_window_text_includes_timestamps() -> None:
    transcript = TimedTranscript(
        duration=30,
        full_text="Hello world later",
        cues=[
            TranscriptCue(start=0, end=5, text="Hello"),
            TranscriptCue(start=10, end=14, text="world"),
            TranscriptCue(start=20, end=24, text="later"),
        ],
    )
    chunk = transcript.window_text(8, 16)
    assert "[10.00-14.00] world" in chunk
    assert "Hello" not in chunk


def test_analyze_video_requires_api_key() -> None:
    try:
        analyze_video(Path("/tmp/missing.mp4"), Settings(gemini_api_key=""))
    except GeminiConfigError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:
        raise AssertionError("expected GeminiConfigError")
