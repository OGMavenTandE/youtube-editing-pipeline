from pathlib import Path

from pipeline.config import Settings
from pipeline.layouts import PictureTag
from pipeline.models import EditScript, GraphicCard, Scene, TalkPoint, TalkSheet, field_is_locked
from pipeline.pacing import enforce_pacing
from pipeline.stills import match_local_still
from pipeline.talk_sheet import (
    KNOWN_MARKDOWN_SHAPE,
    apply_user_point_locks,
    attach_talk_sheet,
    autofill_talk_sheet,
    collect_form_text,
    copy_point_still,
    job_talk_sheet_path,
    load_talk_sheet,
    parse_talk_sheet_markdown,
    persist_talk_sheet,
    point_still_filename,
    save_talk_sheet,
)


def test_talk_sheet_json_roundtrip(tmp_path: Path) -> None:
    sheet = TalkSheet(
        title="SKYNET IS COMING · PART 2",
        title_source="user",
        exec_headline="$1.5B is the floor.\nNot the program.",
        exec_headline_source="user",
        exec_notes="Spoken only.",
        points=[
            TalkPoint(
                platform="MQ-9 Reaper",
                platform_source="user",
                still_path="/tmp/reaper.jpg",
                still_source="user",
                cards=["User card one.", "User card two.", ""],
                card_sources=["user", "user", "empty"],
            ),
            TalkPoint(),
            TalkPoint(),
        ],
    )
    path = tmp_path / "talk.json"
    save_talk_sheet(sheet, path)
    loaded = load_talk_sheet(path)
    assert loaded.title == "SKYNET IS COMING · PART 2"
    assert loaded.title_source == "user"
    assert loaded.headline_lines() == ("$1.5B is the floor.", "Not the program.")
    assert loaded.points[0].platform == "MQ-9 Reaper"
    assert loaded.points[0].cards[0] == "User card one."
    assert loaded.points[0].still_source == "user"
    assert loaded.close_card.kicker == "WORK WITH ME"
    assert "Vendor-agnostic" in loaded.close_card.headline
    assert len(loaded.points) == 3
    assert len(loaded.points[1].cards) == 3


def test_legacy_json_infers_user_source(tmp_path: Path) -> None:
    path = tmp_path / "old.json"
    path.write_text(
        '{"title": "OLD TITLE", "exec_headline": "Line one\\nLine two",'
        ' "points": [{"platform": "MQ-9", "cards": ["Said this."]}]}',
        encoding="utf-8",
    )
    loaded = load_talk_sheet(path)
    assert loaded.title_source == "user"
    assert loaded.exec_headline_source == "user"
    assert loaded.points[0].platform_source == "user"
    assert loaded.points[0].card_sources[0] == "user"
    assert loaded.points[0].card_sources[1] == "empty"


def test_markdown_import_known_sheet_shape() -> None:
    sheet = parse_talk_sheet_markdown(KNOWN_MARKDOWN_SHAPE)
    assert sheet.title == "SKYNET IS COMING · PART 2"
    assert sheet.title_source == "user"
    line1, line2 = sheet.headline_lines()
    assert "floor" in line1
    assert "program" in line2.lower()
    assert sheet.points[0].platform == "MQ-9 Reaper"
    assert sheet.points[0].cards[0].startswith("$1.5B")
    assert sheet.points[0].cards[2].startswith("Programs")
    assert sheet.points[1].platform == "M1 Abrams"
    assert sheet.points[2].platform == "Patriot"
    assert sheet.points[2].cards[2].startswith("Last point, third")
    assert "Spoken" in sheet.exec_notes or "not painted" in sheet.exec_notes.lower()
    assert sheet.close_card.kicker == "WORK WITH ME"
    assert "Vendor-agnostic" in sheet.close_card.headline


def test_markdown_paste_does_not_overwrite_close() -> None:
    sheet = parse_talk_sheet_markdown(
        "# A Title\n\n## Overview\nOne.\nTwo.\n\n## Close\nPLEASE CHANGE ME\nHOST_NAME=Nope\n"
    )
    assert sheet.title == "A Title"
    assert sheet.close_card.kicker == "WORK WITH ME"
    assert "Vendor-agnostic" in sheet.close_card.headline
    assert "PLEASE CHANGE ME" not in sheet.close_card.headline
    assert "Nope" not in sheet.close_card.headline


def test_user_lock_autofill_skips_filled_fields() -> None:
    sheet = TalkSheet(
        title="USER TITLE",
        title_source="user",
        points=[
            TalkPoint(
                platform="MQ-9 Reaper",
                platform_source="user",
                still_path="/locked/user.jpg",
                still_source="user",
                cards=["User locked card.", "", ""],
                card_sources=["user", "empty", "empty"],
            ),
            TalkPoint(),
            TalkPoint(),
        ],
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=40,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="THE MONEY", title="Gemini wrote this."),
            ),
            Scene(
                start=40,
                end=70,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="STACK", title="Second auto card."),
            ),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ],
        talk_sheet=TalkSheet(title="AUTO TITLE", exec_headline="Auto thesis."),
    )
    autofill_talk_sheet(sheet, script, stills_dir=None)
    assert sheet.title == "USER TITLE"
    assert sheet.title_source == "user"
    assert sheet.points[0].cards[0] == "User locked card."
    assert sheet.points[0].card_sources[0] == "user"
    assert sheet.points[0].cards[1] == "Second auto card."
    assert sheet.points[0].card_sources[1] == "auto"
    assert sheet.points[0].still_path == "/locked/user.jpg"
    assert sheet.points[0].still_source == "user"


def test_autofill_empty_still_uses_local_matcher(tmp_path: Path) -> None:
    still = tmp_path / "point1_mq-9-reaper.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(platform="MQ-9 Reaper", platform_source="user"),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = enforce_pacing(EditScript.empty(), 90.0, Settings(bookend_seconds=10))
    autofill_talk_sheet(sheet, script, stills_dir=tmp_path)
    assert sheet.points[0].still_path.endswith("point1_mq-9-reaper.jpg")
    assert sheet.points[0].still_source == "auto"
    assert sheet.points[1].still_path == ""
    assert sheet.points[1].still_source == "empty"


def test_still_copy_names_point_and_platform(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    src.write_bytes(b"png")
    dest_dir = tmp_path / "stills"
    copied = copy_point_still(src, dest_dir, 1, "MQ-9 Reaper", stem="talk")
    assert copied.name == "talk_point1_mq-9-reaper.png"
    assert copied.is_file()
    assert match_local_still("MQ-9 Reaper", dest_dir) == copied
    assert point_still_filename(2, "M1 Abrams", ".jpg") == "point2_m1-abrams.jpg"


def test_apply_user_locks_stamps_cards_and_still(tmp_path: Path) -> None:
    still = tmp_path / "user.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        title="USER TITLE",
        title_source="user",
        exec_headline="Thesis one.\nThesis two.",
        exec_headline_source="user",
        points=[
            TalkPoint(
                platform="MQ-9 Reaper",
                platform_source="user",
                still_path=str(still),
                still_source="user",
                cards=["First spoken card.", "Second spoken card.", ""],
                card_sources=["user", "user", "empty"],
            ),
            TalkPoint(),
            TalkPoint(),
        ],
    )
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    attach_talk_sheet(script, sheet)
    script = enforce_pacing(script, 120.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, script.talk_sheet)
    assert script.scenes[0].graphic.kicker == "USER TITLE"
    assert "Thesis one" in script.scenes[0].graphic.title
    body = [scene for scene in script.scenes if scene.role == "body"]
    pip = [scene for scene in body if scene.layout is PictureTag.PIP]
    overlays = [scene for scene in body if scene.layout is PictureTag.OVERLAY]
    assert pip
    assert pip[0].graphic.asset_path.endswith("user.jpg")
    titles = [scene.graphic.title for scene in overlays]
    assert "First spoken card." in titles
    assert "Second spoken card." in titles
    assert script.scenes[-1].graphic.kicker == "WORK WITH ME"


def test_field_lock_helper() -> None:
    assert field_is_locked("user", "hello")
    assert not field_is_locked("user", "")
    assert not field_is_locked("auto", "hello")
    assert not field_is_locked("empty", "hello")
    text, source = collect_form_text("hello", "hello", "auto")
    assert source == "auto"
    text, source = collect_form_text("changed", "hello", "auto")
    assert text == "changed" and source == "user"
    text, source = collect_form_text("", "hello", "user")
    assert text == "" and source == "empty"


def test_job_path_is_next_to_video_stem(tmp_path: Path) -> None:
    video = tmp_path / "inbox" / "talk.mp4"
    video.parent.mkdir()
    video.write_bytes(b"x")
    path = job_talk_sheet_path(video)
    assert path == tmp_path / "inbox" / "talk_talk_sheet.json"
    persist_talk_sheet(TalkSheet(title="X", title_source="user"), video_path=video)
    assert path.is_file()


def test_empty_form_is_valid_for_autofill() -> None:
    sheet = TalkSheet()
    assert not sheet.title_locked()
    assert all(not point.card_locked(0) for point in sheet.points)
    script = enforce_pacing(EditScript.empty(), 60.0, Settings(bookend_seconds=10))
    autofill_talk_sheet(sheet, script)
    assert sheet.close_card.kicker == "WORK WITH ME"
