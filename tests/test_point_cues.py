"""Point 1–3 picture locks to the trimmed transcript, not equal-thirds clocks."""

from __future__ import annotations

from pathlib import Path

from pipeline.config import Settings
from pipeline.layouts import PictureTag
from pipeline.models import (
    EditScript,
    GraphicCard,
    Scene,
    TalkPoint,
    TalkSheet,
    TimedTranscript,
    TranscriptCue,
)
from pipeline.pacing import enforce_pacing
from pipeline.point_align import find_transcript_hit, resolve_point_alignment
from pipeline.talk_sheet import apply_user_point_locks, parse_talk_sheet_markdown


def _still(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


def _three_point_sheet(tmp_path: Path, *, point2_cue: str = "second unique opener") -> TalkSheet:
    return TalkSheet(
        title="OPEN TITLE",
        title_source="user",
        exec_headline="Overview line one.\nOverview line two.",
        exec_headline_source="user",
        points=[
            TalkPoint(
                start_cue="first unique opener",
                start_cue_source="user",
                platform="MQ-9 Reaper",
                platform_source="user",
                still_path=str(_still(tmp_path, "p1.jpg")),
                still_source="user",
                image_title="P1 STILL",
                image_title_source="user",
                image_text="P1 STILL CONTENT",
                image_text_source="user",
                titles=["P1 GOLD ONE", "P1 GOLD TWO", ""],
                title_sources=["user", "user", "empty"],
                cards=["point one alpha line", "point one bravo line", ""],
                card_sources=["user", "user", "empty"],
            ),
            TalkPoint(
                start_cue=point2_cue,
                start_cue_source="user" if point2_cue.strip() else "empty",
                platform="M1 Abrams",
                platform_source="user",
                still_path=str(_still(tmp_path, "p2.jpg")),
                still_source="user",
                image_title="P2 STILL",
                image_title_source="user",
                image_text="P2 STILL CONTENT",
                image_text_source="user",
                titles=["P2 GOLD ONE", "P2 GOLD TWO", ""],
                title_sources=["user", "user", "empty"],
                cards=["point two alpha line", "point two bravo line", ""],
                card_sources=["user", "user", "empty"],
            ),
            TalkPoint(
                start_cue="third unique opener",
                start_cue_source="user",
                platform="Patriot",
                platform_source="user",
                still_path=str(_still(tmp_path, "p3.jpg")),
                still_source="user",
                image_title="P3 STILL",
                image_title_source="user",
                image_text="P3 STILL CONTENT",
                image_text_source="user",
                titles=["P3 GOLD ONE", "", ""],
                title_sources=["user", "empty", "empty"],
                cards=["point three alpha line", "", ""],
                card_sources=["user", "empty", "empty"],
            ),
        ],
    )


def _trimmed_transcript() -> TimedTranscript:
    """Point 2 starts at 155s (2:35) on the trimmed cut."""
    return TimedTranscript(
        duration=240.0,
        full_text=(
            "OPEN TITLE overview line one first unique opener point one alpha line "
            "point one bravo line second unique opener point two alpha line "
            "point two bravo line third unique opener point three alpha line"
        ),
        cues=[
            TranscriptCue(start=0.0, end=8.0, text="OPEN TITLE overview line one"),
            TranscriptCue(start=20.0, end=24.0, text="first unique opener"),
            TranscriptCue(start=28.0, end=34.0, text="point one alpha line"),
            TranscriptCue(start=40.0, end=46.0, text="point one bravo line"),
            TranscriptCue(start=155.0, end=160.0, text="second unique opener"),
            TranscriptCue(start=162.0, end=168.0, text="point two alpha line"),
            TranscriptCue(start=175.0, end=181.0, text="point two bravo line"),
            TranscriptCue(start=200.0, end=205.0, text="third unique opener"),
            TranscriptCue(start=210.0, end=216.0, text="point three alpha line"),
        ],
    )


def _script_with_long_point1_pip(transcript: TimedTranscript) -> EditScript:
    """Director tagged one long Point 1 pip through 2:35. That is the vanish bug."""
    script = EditScript(
        transcript=transcript.text,
        transcript_cues=list(transcript.cues),
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD, said="OPEN TITLE"),
            Scene(
                start=10,
                end=200,
                role="body",
                layout=PictureTag.PIP,
                said="first unique opener",
                graphic=GraphicCard(kicker="P1 STILL", title="wrong long pip", asset_path="/tmp/p1.jpg"),
            ),
            Scene(start=200, end=230, role="body", layout=PictureTag.NOTHING),
            Scene(start=230, end=240, role="close", layout=PictureTag.LOWER_THIRD),
        ],
    )
    return script


def _body_at(script: EditScript, t: float) -> list[Scene]:
    return [
        scene
        for scene in script.scenes
        if scene.role == "body" and scene.start - 1e-6 <= t < scene.end + 1e-6
    ]


def test_point2_locks_to_cue_not_point1(tmp_path: Path) -> None:
    sheet = _three_point_sheet(tmp_path)
    timed = _trimmed_transcript()
    script = _script_with_long_point1_pip(timed)
    apply_user_point_locks(script, sheet, timed)

    assert script.scenes[0].role == "open"
    assert script.scenes[0].start == 0.0

    pips = [scene for scene in script.scenes if scene.layout is PictureTag.PIP]
    point2_pip = next(scene for scene in pips if "p2.jpg" in (scene.graphic.asset_path or scene.asset_ref or ""))
    assert abs(point2_pip.start - 155.0) < 0.6
    assert point2_pip.graphic.kicker == "P2 STILL"
    assert point2_pip.end - point2_pip.start <= 8.05

    overlays = [scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY]
    point2_overlays = [scene for scene in overlays if 154 <= scene.start < 200]
    titles = [scene.graphic.title for scene in point2_overlays]
    kickers = [scene.graphic.kicker for scene in point2_overlays]
    assert "point two alpha line" in titles
    assert "point two bravo line" in titles
    assert "P2 GOLD ONE" in kickers
    assert "P2 GOLD TWO" in kickers
    assert "point one alpha line" not in titles
    assert "P1 GOLD ONE" not in kickers
    assert "point three alpha line" not in titles

    at_235 = _body_at(script, 155.2)
    assert at_235
    assert all("point one" not in (scene.graphic.title or "").casefold() for scene in at_235)
    assert all(scene.graphic.kicker != "P1 GOLD ONE" for scene in at_235)


def test_empty_cue_falls_back_to_first_card_line(tmp_path: Path) -> None:
    sheet = _three_point_sheet(tmp_path, point2_cue="")
    sheet.points[1].start_cue = ""
    sheet.points[1].start_cue_source = "empty"
    timed = _trimmed_transcript()
    script = _script_with_long_point1_pip(timed)
    apply_user_point_locks(script, sheet, timed)

    alignment = resolve_point_alignment(script, sheet, timed)
    assert alignment.reasons[1] == "card"
    assert abs(alignment.windows[1][0] - 162.0) < 0.6

    overlays = [scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY and scene.start >= 160]
    assert any(scene.graphic.title == "point two alpha line" for scene in overlays)
    assert any(scene.graphic.kicker == "P2 GOLD ONE" for scene in overlays)
    assert all(scene.graphic.title != "point one alpha line" for scene in overlays)


def test_missing_cue_does_not_paint_point1_at_point2_time(tmp_path: Path) -> None:
    sheet = _three_point_sheet(tmp_path, point2_cue="this phrase never appears in the cut")
    timed = _trimmed_transcript()
    script = _script_with_long_point1_pip(timed)
    apply_user_point_locks(script, sheet, timed)

    alignment = resolve_point_alignment(script, sheet, timed)
    assert 1 in alignment.skipped
    assert alignment.windows[1] == (0.0, 0.0)

    at_235 = _body_at(script, 155.2)
    assert at_235
    for scene in at_235:
        assert scene.layout is PictureTag.NOTHING
        assert scene.graphic.title != "point one alpha line"
        assert scene.graphic.title != "point two alpha line"
        assert scene.graphic.kicker != "P1 GOLD ONE"
        assert scene.graphic.kicker != "P2 GOLD ONE"
        assert "p1.jpg" not in (scene.graphic.asset_path or "")


def test_fuzzy_glossary_cue_matches_uh_and_dow() -> None:
    timed = TimedTranscript(
        duration=80.0,
        full_text="the department of defense then second unique, uh, opener",
        cues=[
            TranscriptCue(start=10.0, end=16.0, text="the Department of Defense owns this"),
            TranscriptCue(start=40.0, end=46.0, text="second unique, uh, opener"),
        ],
    )
    assert find_transcript_hit(timed, "second unique opener", after=0.0) == 40.0
    hit = find_transcript_hit(timed, "Department of War", after=0.0)
    assert hit is not None
    assert abs(hit - 10.0) < 0.2


def test_markdown_roundtrip_start_cue() -> None:
    text = (
        "# Title\n\n## Point 1\nStarts when I say: first unique opener\n"
        "Platform: MQ-9\nTitle 1: A\nCard 1: alpha\n"
        "## Point 2\nStart cue: second unique opener\n"
    )
    sheet = parse_talk_sheet_markdown(text)
    assert sheet.points[0].start_cue == "first unique opener"
    assert sheet.points[0].start_cue_source == "user"
    assert sheet.points[1].start_cue == "second unique opener"


def test_open_is_not_point1(tmp_path: Path) -> None:
    sheet = _three_point_sheet(tmp_path)
    timed = _trimmed_transcript()
    script = enforce_pacing(EditScript.empty(), 240.0, Settings(bookend_seconds=10))
    script.transcript = timed.text
    script.transcript_cues = list(timed.cues)
    apply_user_point_locks(script, sheet, timed)
    assert script.scenes[0].role == "open"
    assert script.scenes[0].start == 0.0
    point1_pip = next(
        scene
        for scene in script.scenes
        if scene.layout is PictureTag.PIP and "p1.jpg" in (scene.graphic.asset_path or "")
    )
    assert point1_pip.start >= 10.0
    assert point1_pip.start >= 19.0
