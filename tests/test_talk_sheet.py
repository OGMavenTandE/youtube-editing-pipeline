from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.config import Settings
from pipeline.layouts import OVERLAY_PAD, OVERLAY_W, PictureTag
from pipeline.models import EditScript, GraphicCard, Scene, TalkPoint, TalkSheet, field_is_locked
from pipeline.pacing import enforce_pacing
from pipeline.stills import match_local_still
from pipeline.picture_kit import (
    HEADLINE_CHAR_LIMIT,
    KICKER_CHAR_LIMIT,
    KitScale,
    _fit_headline,
    clip_plate_headline,
    clip_plate_kicker,
    render_overlay,
    render_pip_type,
)
from pipeline.talk_sheet import (
    KNOWN_MARKDOWN_SHAPE,
    PIP_HOLD_SECONDS,
    apply_open_form_values,
    apply_point_form_values,
    apply_user_point_locks,
    attach_talk_sheet,
    autofill_talk_sheet,
    collect_form_text,
    copy_point_still,
    enforce_pip_holds,
    job_talk_sheet_path,
    load_talk_sheet,
    paired_card_copy,
    parse_talk_sheet_markdown,
    persist_talk_sheet,
    point_still_filename,
    point_windows,
    resolve_auto_kicker,
    rewrite_house_style,
    save_talk_sheet,
    sanitize_script_kickers,
    still_plate_copy,
    talk_sheet_to_markdown,
)

DRONE_CABLE = "Look at this drone laying cable to keep comms with it's pilot. Adorable."


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
                titles=["THE MONEY", "EVEN LOW", ""],
                title_sources=["user", "user", "empty"],
                image_title="MQ-9 REAPER",
                image_title_source="user",
                image_text="Reaper on station",
                image_text_source="user",
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
    assert loaded.points[0].titles[0] == "THE MONEY"
    assert loaded.points[0].image_title == "MQ-9 REAPER"
    assert loaded.points[0].image_text == "Reaper on station"
    assert loaded.points[0].still_source == "user"
    assert loaded.points[0].title_sources[0] == "user"
    assert loaded.points[0].image_title_source == "user"
    assert loaded.points[0].image_text_source == "user"
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
    assert sheet.points[0].start_cue == "first unique opener"
    assert sheet.points[0].start_cue_source == "user"
    assert sheet.points[0].image_title == "MQ-9 REAPER"
    assert sheet.points[0].image_text == "Reaper on station"
    assert sheet.points[0].titles[0] == "THE MONEY"
    assert sheet.points[0].cards[0].startswith("$1.5B")
    assert sheet.points[0].cards[2].startswith("Programs")
    assert sheet.points[1].platform == "M1 Abrams"
    assert sheet.points[1].start_cue == "second unique opener"
    assert sheet.points[2].platform == "Patriot"
    assert sheet.points[2].start_cue == "third unique opener"
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
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="THE MONEY", title="Gemini wrote this."),
            ),
            Scene(
                start=18,
                end=26,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="STACK", title="Second auto card."),
            ),
            Scene(start=26, end=100, role="body", layout=PictureTag.NOTHING),
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
                titles=["THE MONEY", "EVEN LOW", ""],
                title_sources=["user", "user", "empty"],
                image_title="MQ-9 REAPER",
                image_title_source="user",
                image_text="Reaper on station",
                image_text_source="user",
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
    assert pip[0].graphic.kicker == "MQ-9 REAPER"
    assert pip[0].graphic.title == "Reaper on station"
    assert pip[0].end - pip[0].start + 1e-6 >= PIP_HOLD_SECONDS
    kickers = [scene.graphic.kicker for scene in overlays]
    assert "THE MONEY" in kickers
    assert "EVEN LOW" in kickers
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


def test_user_card_title_is_locked_kicker() -> None:
    sheet = TalkSheet(
        points=[
            TalkPoint(
                cards=["Military drones do not coordinate with each other"],
                card_sources=["user"],
                titles=["DRONE SWARMS"],
                title_sources=["user"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        transcript="Military drones do not coordinate with each other.",
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="DOD DIRECTIVE 3000.09", title="Gemini rewrite."),
            ),
            Scene(start=18, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ],
    )
    apply_user_point_locks(script, sheet)
    overlays = [scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY]
    assert overlays
    assert overlays[0].graphic.kicker == "DRONE SWARMS"
    assert "dod" not in overlays[0].graphic.kicker.casefold()
    assert "3000.09" not in overlays[0].graphic.kicker


def test_empty_title_autofill_does_not_invent_dod_or_directive() -> None:
    sheet = TalkSheet(
        points=[
            TalkPoint(cards=["", "", ""], card_sources=["empty", "empty", "empty"]),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        transcript="Military drones do not coordinate with each other.",
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                said="Military drones do not coordinate with each other.",
                graphic=GraphicCard(
                    kicker="DOD DIRECTIVE 3000.09",
                    title="Military drones do not coordinate with each other",
                ),
            ),
            Scene(start=18, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ],
    )
    autofill_talk_sheet(sheet, script)
    kicker = sheet.points[0].titles[0]
    overlay = next(scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY)
    assert sheet.points[0].title_sources[0] == "auto"
    assert "dod" not in kicker.casefold()
    assert "department of defense" not in kicker.casefold()
    assert "3000.09" not in kicker
    assert "directive" not in kicker.casefold()
    assert "dod" not in overlay.graphic.kicker.casefold()
    assert "3000.09" not in overlay.graphic.kicker
    assert kicker
    assert overlay.graphic.kicker == kicker


def test_auto_copy_uses_department_of_war() -> None:
    assert rewrite_house_style("Department of Defense drones") == "Department of War drones"
    assert rewrite_house_style("DOD policy") == "DOW policy"
    label = resolve_auto_kicker(
        "Department of Defense",
        headline="Department of Defense owns the program",
        allowed="",
    )
    assert "DOD" not in label
    assert "DEPARTMENT OF DEFENSE" not in label
    assert "WAR" in label or label == "DOW"


def test_user_may_type_dod_as_a_title() -> None:
    sheet = TalkSheet(
        points=[
            TalkPoint(
                cards=["He said the old name on purpose."],
                card_sources=["user"],
                titles=["DoD"],
                title_sources=["user"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="AUTO", title="He said the old name on purpose."),
            ),
            Scene(start=18, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    apply_user_point_locks(script, sheet)
    overlay = next(scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY)
    assert overlay.graphic.kicker == "DoD"


def test_user_image_text_empty_title_is_content_only(tmp_path: Path) -> None:
    still = tmp_path / "user.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(
                platform="MQ-9 Reaper",
                platform_source="user",
                still_path=str(still),
                still_source="user",
                image_text="REAPER ON STATION",
                image_text_source="user",
                cards=["First spoken card.", "", ""],
                card_sources=["user", "empty", "empty"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, sheet)
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    assert pip.graphic.kicker == ""
    assert pip.graphic.title == "REAPER ON STATION"
    assert pip.end - pip.start + 1e-6 >= PIP_HOLD_SECONDS


def test_empty_image_text_autofill_from_platform_not_dod() -> None:
    sheet = TalkSheet(
        points=[
            TalkPoint(platform="MQ-9 Reaper", platform_source="user"),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        transcript="The Reaper does not talk to the next airframe.",
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=13,
                role="body",
                layout=PictureTag.PIP,
                graphic=GraphicCard(
                    kicker="DOD DIRECTIVE 3000.09",
                    title="Reaper",
                    still_query="MQ-9 Reaper",
                    asset_path="/tmp/reaper.jpg",
                ),
            ),
            Scene(start=13, end=40, role="body", layout=PictureTag.NOTHING),
            Scene(start=40, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ],
    )
    autofill_talk_sheet(sheet, script)
    assert sheet.points[0].image_title_source == "auto"
    assert sheet.points[0].image_text_source == "auto"
    assert "dod" not in sheet.points[0].image_title.casefold()
    assert "dod" not in sheet.points[0].image_text.casefold()
    assert "3000.09" not in sheet.points[0].image_title
    assert "3000.09" not in sheet.points[0].image_text
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    assert pip.graphic.kicker == sheet.points[0].image_title
    assert pip.graphic.title == sheet.points[0].image_text
    assert "dod" not in pip.graphic.kicker.casefold()
    assert pip.graphic.kicker
    assert pip.graphic.title


def test_markdown_export_includes_card_title_and_image_text() -> None:
    sheet = TalkSheet(
        title="A TITLE",
        title_source="user",
        exec_headline="Line one.\nLine two.",
        points=[
            TalkPoint(
                platform="MQ-9 Reaper",
                image_title="MQ-9 REAPER",
                image_text="Reaper on station",
                titles=["THE MONEY", "", ""],
                cards=["$1.5B is the floor.", "", ""],
            ),
            TalkPoint(),
            TalkPoint(),
        ],
    )
    text = talk_sheet_to_markdown(sheet)
    assert "Image title: MQ-9 REAPER" in text
    assert "Image text: Reaper on station" in text
    assert "Title 1: THE MONEY" in text
    assert "Card 1: $1.5B is the floor." in text
    imported = parse_talk_sheet_markdown(text)
    assert imported.points[0].image_title == "MQ-9 REAPER"
    assert imported.points[0].image_text == "Reaper on station"
    assert imported.points[0].titles[0] == "THE MONEY"
    assert imported.points[0].cards[0].startswith("$1.5B")


def test_pip_hold_grows_into_nothing_not_overlay() -> None:
    overlay = Scene(
        start=28,
        end=36,
        role="body",
        layout=PictureTag.OVERLAY,
        graphic=GraphicCard(kicker="THE MONEY", title="$1.5B is the floor."),
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=13,
                role="body",
                layout=PictureTag.PIP,
                graphic=GraphicCard(kicker="MQ-9", title="Reaper", asset_path="/tmp/x.jpg"),
            ),
            Scene(start=13, end=28, role="body", layout=PictureTag.NOTHING),
            overlay,
            Scene(start=36, end=110, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    enforce_pip_holds(script, 8.0)
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    kept = next(scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY)
    assert pip.end - pip.start + 1e-6 >= 8.0
    assert pip.end - pip.start <= 8.05
    assert kept.start == 28
    assert kept.end == 36
    assert kept.graphic.title == "$1.5B is the floor."


def test_pip_hold_does_not_swallow_long_nothing() -> None:
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=13,
                role="body",
                layout=PictureTag.PIP,
                graphic=GraphicCard(kicker="MQ-9", title="Reaper", asset_path="/tmp/x.jpg"),
            ),
            Scene(start=13, end=63, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    enforce_pip_holds(script, 8.0)
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    nothings = [scene for scene in script.scenes if scene.role == "body" and scene.layout is PictureTag.NOTHING]
    assert pip.end - pip.start <= 8.05
    assert pip.end - pip.start + 1e-6 >= 8.0
    assert sum(scene.end - scene.start for scene in nothings) > 40


def test_long_director_pip_is_capped_to_hold() -> None:
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=60,
                role="body",
                layout=PictureTag.PIP,
                graphic=GraphicCard(kicker="MQ-9", title="Reaper", asset_path="/tmp/x.jpg"),
            ),
            Scene(start=60, end=110, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    enforce_pip_holds(script, 8.0)
    pips = [scene for scene in script.scenes if scene.layout is PictureTag.PIP]
    assert len(pips) == 1
    assert pips[0].end - pips[0].start <= 8.05
    assert pips[0].start == 10
    leftover = next(
        scene
        for scene in script.scenes
        if scene.role == "body" and scene.layout is PictureTag.NOTHING and scene.start >= 18
    )
    assert leftover.start == pips[0].end
    assert leftover.layout is PictureTag.NOTHING


def test_image_text_does_not_drive_overlay_kicker(tmp_path: Path) -> None:
    still = tmp_path / "drone.jpg"
    still.write_bytes(b"x")
    image = "Look at this drone laying cable to keep comms with it"
    sheet = TalkSheet(
        points=[
            TalkPoint(
                still_path=str(still),
                still_source="user",
                image_text=image,
                image_text_source="user",
                titles=["DOW DIRECTIVE 3000.09", "", ""],
                title_sources=["user", "empty", "empty"],
                cards=["Humans must be in the loop for an attack, DoW Directive 3000.09", "", ""],
                card_sources=["user", "empty", "empty"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.PIP,
                graphic=GraphicCard(kicker="WRONG", title="x", asset_path=str(still)),
            ),
            Scene(
                start=18,
                end=28,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker=image, title="old body"),
            ),
            Scene(start=28, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    apply_user_point_locks(script, sheet)
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    overlay = next(scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY)
    assert pip.graphic.kicker == ""
    assert pip.graphic.title == image
    assert overlay.graphic.kicker == "DOW DIRECTIVE 3000.09"
    assert overlay.graphic.title.startswith("Humans must be in the loop")
    assert "drone laying cable" not in overlay.graphic.kicker.casefold()


def test_empty_overlay_title_comes_from_card_not_image_text() -> None:
    image = "Look at this drone laying cable to keep comms with it"
    sheet = TalkSheet(
        points=[
            TalkPoint(
                image_text=image,
                image_text_source="user",
                cards=["Humans must be in the loop for an attack"],
                card_sources=["user"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker=image, title="Humans must be in the loop for an attack"),
            ),
            Scene(start=18, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    apply_user_point_locks(script, sheet)
    autofill_talk_sheet(sheet, script)
    overlay = next(scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY)
    assert "drone laying cable" not in overlay.graphic.kicker.casefold()
    assert "drone laying cable" not in sheet.points[0].titles[0].casefold()
    assert overlay.graphic.title.startswith("Humans must be in the loop")
    words = overlay.graphic.kicker.split()
    assert 1 <= len(words) <= 4


def test_screenshot_overlay_does_not_use_still_image_text() -> None:
    """Gold kicker was the drone-cable still title. Card body is the 3000.09 line."""
    image = "Look at this drone laying cable to keep comms with it's pilot. Adorable."
    body = "Humans must be in the loop for an attack, DoW Directive 3000.09"
    sheet = TalkSheet(
        points=[
            TalkPoint(
                image_text=image,
                image_text_source="user",
                titles=[image, "", ""],
                title_sources=["user", "empty", "empty"],
                cards=[body, "", ""],
                card_sources=["user", "empty", "empty"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker=image, title=body),
            ),
            Scene(start=18, end=100, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    apply_user_point_locks(script, sheet)
    overlay = next(scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY)
    assert overlay.graphic.title == body
    assert "drone laying cable" not in overlay.graphic.kicker.casefold()
    assert "adorable" not in overlay.graphic.kicker.casefold()
    assert overlay.graphic.kicker.strip()
    words = overlay.graphic.kicker.split()
    assert 1 <= len(words) <= 4


def test_user_still_does_not_turn_whole_body_into_pip(tmp_path: Path) -> None:
    still = tmp_path / "user.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(
                platform="MQ-9 Reaper",
                platform_source="user",
                still_path=str(still),
                still_source="user",
                image_text="REAPER",
                image_text_source="user",
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, sheet)
    pips = [scene for scene in script.scenes if scene.layout is PictureTag.PIP]
    nothings = [scene for scene in script.scenes if scene.role == "body" and scene.layout is PictureTag.NOTHING]
    assert len(pips) == 1
    assert 7.5 <= pips[0].end - pips[0].start <= 8.05
    assert nothings
    assert sum(scene.end - scene.start for scene in nothings) > 40


def test_still_plate_does_not_split_image_text_on_period(tmp_path: Path) -> None:
    still = tmp_path / "drone.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(
                still_path=str(still),
                still_source="user",
                image_text=DRONE_CABLE,
                image_text_source="user",
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, sheet)
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    gold, white = still_plate_copy(sheet.points[0])
    assert gold == ""
    assert white == DRONE_CABLE
    assert pip.graphic.kicker == ""
    assert pip.graphic.title == DRONE_CABLE
    assert "Adorable" in pip.graphic.title
    assert "LOOK AT THIS DRONE" not in pip.graphic.kicker


def test_filled_image_title_keeps_full_image_text(tmp_path: Path) -> None:
    still = tmp_path / "drone.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(
                still_path=str(still),
                still_source="user",
                image_title="CABLE DRONE",
                image_title_source="user",
                image_text=DRONE_CABLE,
                image_text_source="user",
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, sheet)
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    assert pip.graphic.kicker == "CABLE DRONE"
    assert pip.graphic.title == DRONE_CABLE
    assert pip.graphic.title.endswith("Adorable.")
    assert "pilot" in pip.graphic.title


def test_autofill_does_not_split_user_image_text(tmp_path: Path) -> None:
    still = tmp_path / "point1_cable-drone.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(
                platform="cable drone",
                platform_source="user",
                still_path=str(still),
                still_source="user",
                image_text=DRONE_CABLE,
                image_text_source="user",
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    autofill_talk_sheet(sheet, script)
    apply_user_point_locks(script, sheet)
    assert sheet.points[0].image_text == DRONE_CABLE
    assert "Adorable" in sheet.points[0].image_text
    assert "look at this drone" not in sheet.points[0].image_title.casefold()
    assert "adorable" not in sheet.points[0].image_title.casefold()
    if sheet.points[0].image_title.strip():
        assert "dod" not in sheet.points[0].image_title.casefold()
        assert "3000.09" not in sheet.points[0].image_title
    pip = next(scene for scene in script.scenes if scene.layout is PictureTag.PIP)
    assert pip.graphic.title == DRONE_CABLE
    assert pip.graphic.kicker != DRONE_CABLE.split(".", 1)[0]


def _sentinel_sheet(tmp_path: Path) -> TalkSheet:
    sheet = TalkSheet()
    apply_open_form_values(sheet, "OPEN_T", "OPEN_H1", "OPEN_H2", "")
    for point_i, point in enumerate(sheet.points):
        still = tmp_path / f"p{point_i + 1}.jpg"
        still.write_bytes(b"x")
        apply_point_form_values(
            point,
            platform=f"P{point_i + 1}PLAT",
            image_title=f"P{point_i + 1}ST",
            image_text=f"P{point_i + 1}SX",
            titles=[f"P{point_i + 1}C{card + 1}T" for card in range(3)],
            cards=[f"P{point_i + 1}C{card + 1}B" for card in range(3)],
        )
        point.still_path = str(still)
        point.still_source = "user"
        point.platform_source = "user"
        point.image_title_source = "user"
        point.image_text_source = "user"
        point.title_sources = ["user", "user", "user"]
        point.card_sources = ["user", "user", "user"]
    return sheet


def test_form_values_keep_every_slot_paired() -> None:
    sheet = TalkSheet()
    apply_open_form_values(sheet, "OPEN_T", "OPEN_H1", "OPEN_H2", "notes")
    apply_point_form_values(
        sheet.points[0],
        image_title="P1ST",
        image_text="P1SX",
        titles=["P1C1T", "P1C2T", "P1C3T"],
        cards=["P1C1B", "P1C2B", "P1C3B"],
    )
    assert sheet.title == "OPEN_T"
    assert sheet.exec_headline == "OPEN_H1\nOPEN_H2"
    assert sheet.open_card.kicker == "OPEN_T"
    assert "OPEN_H1" in sheet.open_card.headline
    assert sheet.points[0].titles == ["P1C1T", "P1C2T", "P1C3T"]
    assert sheet.points[0].cards == ["P1C1B", "P1C2B", "P1C3B"]
    assert paired_card_copy(sheet.points[0], 1) == ("P1C2T", "P1C2B")
    assert still_plate_copy(sheet.points[0]) == ("P1ST", "P1SX")


def test_form_values_clip_to_plate_limits() -> None:
    long_kicker = "K" * (KICKER_CHAR_LIMIT + 12)
    long_body = "B" * (HEADLINE_CHAR_LIMIT + 20)
    sheet = TalkSheet()
    apply_open_form_values(sheet, long_kicker, long_body, "", "")
    apply_point_form_values(
        sheet.points[0],
        image_title=long_kicker,
        image_text=long_body,
        titles=[long_kicker, "T2", "T3"],
        cards=[long_body, "C2", "C3"],
    )
    assert len(sheet.title) == KICKER_CHAR_LIMIT
    assert len(sheet.points[0].image_title) == KICKER_CHAR_LIMIT
    assert len(sheet.points[0].image_text) == HEADLINE_CHAR_LIMIT
    assert len(sheet.points[0].titles[0]) == KICKER_CHAR_LIMIT
    assert len(sheet.points[0].cards[0]) == HEADLINE_CHAR_LIMIT


def test_card_two_and_three_do_not_cross_pair(tmp_path: Path) -> None:
    still = tmp_path / "x.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet(
        points=[
            TalkPoint(
                still_path=str(still),
                still_source="user",
                titles=["P1C1T", "NOT AUTONOMOUS", "CDAO GAVE ANDURIL 100M"],
                title_sources=["user", "user", "user"],
                cards=["P1C1B", "Card two body only", "Humans must be in the loop for an attack"],
                card_sources=["user", "user", "user"],
            ),
            TalkPoint(),
            TalkPoint(),
        ]
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=10, role="open", layout=PictureTag.LOWER_THIRD),
            Scene(
                start=10,
                end=18,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="WRONG2", title="WRONG2B"),
            ),
            Scene(
                start=18,
                end=26,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="WRONG3", title="WRONG3B"),
            ),
            Scene(
                start=26,
                end=34,
                role="body",
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="WRONG1", title="WRONG1B"),
            ),
            Scene(start=34, end=110, role="body", layout=PictureTag.NOTHING),
            Scene(start=110, end=120, role="close", layout=PictureTag.LOWER_THIRD),
        ]
    )
    apply_user_point_locks(script, sheet)
    overlays = [scene for scene in script.scenes if scene.layout is PictureTag.OVERLAY]
    assert len(overlays) >= 3
    assert overlays[0].graphic.kicker == "P1C1T"
    assert overlays[0].graphic.title == "P1C1B"
    assert overlays[1].graphic.kicker == "NOT AUTONOMOUS"
    assert overlays[1].graphic.title == "Card two body only"
    assert overlays[2].graphic.kicker == "CDAO GAVE ANDURIL 100M"
    assert overlays[2].graphic.title == "Humans must be in the loop for an attack"
    assert overlays[1].graphic.title != overlays[2].graphic.title
    assert "Humans must be in the loop" not in overlays[1].graphic.title


def test_full_matrix_form_sheet_json_markdown_render(tmp_path: Path) -> None:
    sheet = _sentinel_sheet(tmp_path)
    path = tmp_path / "talk.json"
    save_talk_sheet(sheet, path)
    loaded = load_talk_sheet(path)
    md = talk_sheet_to_markdown(loaded)
    imported = parse_talk_sheet_markdown(md)
    assert imported.title == "OPEN_T"
    assert "OPEN_H1" in imported.exec_headline
    for src, dest in zip(loaded.points, imported.points):
        dest.still_path = src.still_path
        dest.still_source = src.still_source
    sentinels: list[str] = ["OPEN_T", "OPEN_H1", "OPEN_H2"]
    for point_i, point in enumerate(imported.points):
        assert point.image_title == f"P{point_i + 1}ST"
        assert point.image_text == f"P{point_i + 1}SX"
        sentinels.extend([point.image_title, point.image_text])
        for card_i in range(3):
            title, body = paired_card_copy(point, card_i)
            assert title == f"P{point_i + 1}C{card_i + 1}T"
            assert body == f"P{point_i + 1}C{card_i + 1}B"
            sentinels.extend([title, body])

    script = EditScript.empty()
    attach_talk_sheet(script, imported)
    script = enforce_pacing(script, 180.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, script.talk_sheet)

    opened = script.scenes[0]
    assert opened.role == "open"
    assert opened.graphic.kicker == "OPEN_T"
    assert "OPEN_H1" in opened.graphic.title
    assert "P1C1T" not in opened.graphic.kicker
    assert "P1C1B" not in opened.graphic.title

    for point_i, point in enumerate(imported.points):
        w0, w1 = point_windows(script)[point_i]
        overlays = [
            scene
            for scene in script.scenes
            if scene.role == "body"
            and scene.layout is PictureTag.OVERLAY
            and scene.start < w1
            and scene.end > w0
        ]
        pips = [
            scene
            for scene in script.scenes
            if scene.layout is PictureTag.PIP and scene.start < w1 and scene.end > w0
        ]
        assert len(overlays) >= 3
        assert pips
        for card_i in range(3):
            gold, white = overlays[card_i].graphic.kicker, overlays[card_i].graphic.title
            assert gold == f"P{point_i + 1}C{card_i + 1}T"
            assert white == f"P{point_i + 1}C{card_i + 1}B"
            chrome = render_overlay((1920, 1080), kicker=gold, headline=white)
            assert chrome[:, :, 3].max() > 80
            foreign = [s for s in sentinels if s not in {gold, white, "OPEN_T", "OPEN_H1", "OPEN_H2"}]
            for other in (f"P{point_i + 1}C{k + 1}T" for k in range(3) if k != card_i):
                assert other != gold
            for other in (f"P{point_i + 1}C{k + 1}B" for k in range(3) if k != card_i):
                assert other != white
            del foreign
        pip = pips[0]
        assert pip.graphic.kicker == f"P{point_i + 1}ST"
        assert pip.graphic.title == f"P{point_i + 1}SX"
        for card_i in range(3):
            assert pip.graphic.kicker != f"P{point_i + 1}C{card_i + 1}T"
            assert pip.graphic.title != f"P{point_i + 1}C{card_i + 1}B"
        pip_chrome = render_pip_type(
            (1920, 1080),
            kicker=pip.graphic.kicker,
            sub=pip.graphic.title,
        )
        expected = render_overlay(
            (1920, 1080),
            kicker=f"P{point_i + 1}ST",
            headline=f"P{point_i + 1}SX",
            icon="bar_chart",
        )
        assert (pip_chrome == expected).all()


def test_clip_helpers_match_measured_plate() -> None:
    assert KICKER_CHAR_LIMIT == 40
    assert HEADLINE_CHAR_LIMIT == 76
    assert clip_plate_kicker("A" * 50) == "A" * 40
    assert clip_plate_headline("B" * 90) == "B" * 76
    long = "Humans must be in the loop for an attack, DoW Directive 3000.09 extra overflow"
    chrome = render_overlay((1920, 1080), kicker="NOT AUTONOMOUS", headline=long)
    from pipeline.layouts import overlay_rect, pip_rect

    ox, oy, ow, _ = overlay_rect()
    rgb = chrome[:, :, :3]
    gold_xs = [
        x
        for y in range(oy, min(oy + 80, 1080))
        for x in range(1920)
        if abs(int(rgb[y, x, 0]) - 224) < 36 and abs(int(rgb[y, x, 1]) - 180) < 36
    ]
    assert gold_xs
    assert max(gold_xs) < ox + ow - 4
    pip_box = pip_rect()
    alpha = chrome[:, :, 3]
    ys, xs = (alpha > 80).nonzero()
    assert int(xs.max()) < pip_box[0]
    assert int(ys.max()) < pip_box[1]


HUMAN_REQUIRED_KICKER = "HUMAN REQUIRED"
HUMAN_REQUIRED_BODY = (
    "Attack drones are still directed by pilots using joysticks and a video feed."
)


def test_human_required_live_card_pairs_and_wraps_in_full_matrix(tmp_path: Path) -> None:
    """Scott live overlay: gold HUMAN REQUIRED must keep its own body, wrapped to the plate."""
    still = tmp_path / "p1.jpg"
    still.write_bytes(b"x")
    sheet = TalkSheet()
    apply_open_form_values(sheet, "OPEN_T", "OPEN_H1", "OPEN_H2", "")
    apply_point_form_values(
        sheet.points[0],
        platform="P1PLAT",
        image_title="P1ST",
        image_text="P1SX",
        titles=[HUMAN_REQUIRED_KICKER, "NOT AUTONOMOUS", "CDAO GAVE ANDURIL 100M"],
        cards=[
            HUMAN_REQUIRED_BODY,
            "Card two body only",
            "Humans must be in the loop for an attack",
        ],
    )
    sheet.points[0].still_path = str(still)
    sheet.points[0].still_source = "user"
    sheet.points[0].platform_source = "user"
    sheet.points[0].image_title_source = "user"
    sheet.points[0].image_text_source = "user"
    sheet.points[0].title_sources = ["user", "user", "user"]
    sheet.points[0].card_sources = ["user", "user", "user"]
    for point_i, point in enumerate(sheet.points[1:], start=2):
        extra = tmp_path / f"p{point_i}.jpg"
        extra.write_bytes(b"x")
        apply_point_form_values(
            point,
            platform=f"P{point_i}PLAT",
            image_title=f"P{point_i}ST",
            image_text=f"P{point_i}SX",
            titles=[f"P{point_i}C{card + 1}T" for card in range(3)],
            cards=[f"P{point_i}C{card + 1}B" for card in range(3)],
        )
        point.still_path = str(extra)
        point.still_source = "user"
        point.platform_source = "user"
        point.image_title_source = "user"
        point.image_text_source = "user"
        point.title_sources = ["user", "user", "user"]
        point.card_sources = ["user", "user", "user"]

    path = tmp_path / "talk.json"
    save_talk_sheet(sheet, path)
    loaded = load_talk_sheet(path)
    md = talk_sheet_to_markdown(loaded)
    imported = parse_talk_sheet_markdown(md)
    for src, dest in zip(loaded.points, imported.points):
        dest.still_path = src.still_path
        dest.still_source = src.still_source

    assert paired_card_copy(imported.points[0], 0) == (HUMAN_REQUIRED_KICKER, HUMAN_REQUIRED_BODY)
    assert paired_card_copy(imported.points[0], 1) == ("NOT AUTONOMOUS", "Card two body only")
    assert "Card two body only" not in paired_card_copy(imported.points[0], 0)[1]
    assert HUMAN_REQUIRED_BODY.startswith("Attack drones are still directed by pilots using")
    assert "joysticks" in HUMAN_REQUIRED_BODY

    script = EditScript.empty()
    attach_talk_sheet(script, imported)
    script = enforce_pacing(script, 180.0, Settings(bookend_seconds=10))
    apply_user_point_locks(script, script.talk_sheet)

    w0, w1 = point_windows(script)[0]
    overlays = [
        scene
        for scene in script.scenes
        if scene.role == "body"
        and scene.layout is PictureTag.OVERLAY
        and scene.start < w1
        and scene.end > w0
    ]
    assert len(overlays) >= 3
    assert overlays[0].graphic.kicker == HUMAN_REQUIRED_KICKER
    assert overlays[0].graphic.title == HUMAN_REQUIRED_BODY
    assert overlays[1].graphic.kicker == "NOT AUTONOMOUS"
    assert overlays[1].graphic.title == "Card two body only"
    assert overlays[0].graphic.title != overlays[1].graphic.title
    assert "Card two body only" not in overlays[0].graphic.title
    assert overlays[0].graphic.title.endswith("using") is False
    assert "joysticks" in overlays[0].graphic.title

    inner_w = OVERLAY_W - 2 * OVERLAY_PAD
    font, lines, size = _fit_headline(
        overlays[0].graphic.title,
        KitScale(1920, 1080),
        inner_w,
        max_lines=2,
    )
    joined = " ".join(lines)
    assert "using" in joined
    assert "joysticks" in joined
    assert "feed" in joined
    assert size < 40
    draw = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    assert draw.textlength(lines[0], font=font) >= inner_w * 0.85

    chrome = render_overlay(
        (1920, 1080),
        kicker=overlays[0].graphic.kicker,
        headline=overlays[0].graphic.title,
    )
    assert chrome[:, :, 3].max() > 80
    wrong = render_overlay(
        (1920, 1080),
        kicker=HUMAN_REQUIRED_KICKER,
        headline="Card two body only",
    )
    assert not (chrome == wrong).all()
