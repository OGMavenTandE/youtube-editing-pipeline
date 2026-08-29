from pathlib import Path

from pipeline.config import Settings
from pipeline.gemini_director import (
    GeminiConfigError,
    analyze_video,
    director_windows,
    fit_scenes_to_window,
    normalize_youtube_metadata,
    parse_transcript,
    plan_from_transcript,
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


def test_stitch_does_not_keep_window_zero_metadata() -> None:
    plans = [
        DirectorPlan(
            scenes=[PlannedScene(start=0, end=20, reason="a")],
            metadata=YouTubeMetadata(
                titles=["How to cut talking-head footage"],
                chapters=[
                    ChapterMarker(start=0, title="Window zero intro"),
                    ChapterMarker(start=40, title="Window zero body"),
                    ChapterMarker(start=80, title="Window zero out"),
                ],
            ),
        ),
        DirectorPlan(
            scenes=[PlannedScene(start=0, end=20, reason="b")],
            metadata=YouTubeMetadata(titles=["Ignore me"]),
        ),
    ]
    stitched = stitch_director_plans(plans, [(0.0, 300.0), (300.0, 600.0)], 600)
    assert stitched.metadata.titles == []
    assert stitched.metadata.chapters == []
    assert stitched.scenes[0].start == 0
    assert stitched.scenes[1].start == 300
    assert stitched.scenes[1].end == 320


def test_multi_window_plans_do_not_keep_only_window_zero_chapters() -> None:
    """Dedicated full-cut metadata covers the whole duration, not window 0."""
    window_zero = YouTubeMetadata(
        titles=["W0", "W0b", "W0c", "W0d", "W0e"],
        chapters=[
            ChapterMarker(start=0, title="First five minutes"),
            ChapterMarker(start=90, title="Still window zero"),
            ChapterMarker(start=180, title="End of window zero"),
        ],
    )
    stitched = stitch_director_plans(
        [
            DirectorPlan(scenes=[PlannedScene(start=0, end=20, reason="a")], metadata=window_zero),
            DirectorPlan(scenes=[PlannedScene(start=0, end=20, reason="b")]),
            DirectorPlan(scenes=[PlannedScene(start=0, end=20, reason="c")]),
            DirectorPlan(scenes=[PlannedScene(start=0, end=20, reason="d")]),
        ],
        [(0.0, 300.0), (300.0, 600.0), (600.0, 900.0), (900.0, 1200.0)],
        1200,
    )
    assert all(chapter.start < 300 for chapter in window_zero.chapters)
    assert stitched.metadata.chapters == []

    full = normalize_youtube_metadata(
        YouTubeMetadata(
            titles=["Full A", "Full B", "Full C", "Full D", "Full E"],
            chapters=[
                ChapterMarker(start=0, title="Open"),
                ChapterMarker(start=180, title="Setup"),
                ChapterMarker(start=480, title="Middle"),
                ChapterMarker(start=840, title="Payoff"),
                ChapterMarker(start=1080, title="Close"),
            ],
            tags=["edit"] * 10,
        ),
        1200.0,
        fallback_title="Talk",
    )
    assert any(chapter.start >= 300 for chapter in full.chapters)
    assert full.chapters[-1].start >= 840
    assert full.chapters[0].start == 0.0


def test_plan_from_transcript_uses_full_cut_metadata_pass(monkeypatch) -> None:
    calls: list[str] = []

    def fake_generate(client, *, model, contents, schema, temperature=0.4):
        del client, model, contents, temperature
        name = schema.__name__
        calls.append(name)
        if name == "_PackagingSchema":
            return {
                "titles": ["A", "B", "C", "D", "E"],
                "description": "Full-cut body.",
                "chapters": [
                    {"start": 0, "title": "Open"},
                    {"start": 20, "title": "Middle"},
                    {"start": 35, "title": "Close"},
                ],
                "tags": ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"],
            }
        return {
            "scenes": [
                {
                    "start": 0,
                    "end": 10,
                    "layout": "FULL_FRAME",
                    "reason": "talk",
                    "graphic": {"title": "Talk", "slide_id": "s1"},
                }
            ],
            "metadata": {
                "titles": ["Window only"],
                "chapters": [{"start": 0, "title": "Window 0 only"}],
            },
        }

    monkeypatch.setattr("pipeline.gemini_director._generate_json", fake_generate)
    settings = Settings(
        gemini_api_key="test",
        director_chunk_threshold=10,
        director_chunk_seconds=10,
    )
    transcript = TimedTranscript(
        duration=40,
        full_text="Hello later closer finish",
        cues=[
            TranscriptCue(start=0, end=8, text="Hello"),
            TranscriptCue(start=12, end=20, text="later"),
            TranscriptCue(start=24, end=32, text="closer"),
            TranscriptCue(start=32, end=40, text="finish"),
        ],
    )
    script = plan_from_transcript(
        transcript, 40.0, settings, fallback_title="Talk", client=object()
    )
    assert calls.count("DirectorPlan") == 4
    assert calls.count("_PackagingSchema") == 1
    assert calls[-1] == "_PackagingSchema"
    assert "Full-cut body." in script.metadata.description
    assert any(chapter.start >= 20 for chapter in script.metadata.chapters)
    assert script.metadata.titles[0] == "A"
    assert "Window only" not in script.metadata.titles


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
