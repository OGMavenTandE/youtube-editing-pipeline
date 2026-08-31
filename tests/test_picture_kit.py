from pathlib import Path

import numpy as np
from PIL import Image

from pipeline.layouts import GOLD, PictureTag, lower_third_rect, overlay_rect, pip_rect
from pipeline.models import (
    BookendCard,
    EditScript,
    GraphicCard,
    HostIdentity,
    PlannedScene,
    Scene,
    TalkSheet,
)
from pipeline.pacing import apply_bookends, enforce_pacing
from pipeline.picture_kit import ICON_NAMES, draw_icon, load_inter, render_kit_fixtures, render_overlay
from pipeline.config import Settings
from pipeline.shotlist import compose_mode, resolve_scene


GOLD_RGB = np.array(GOLD, dtype=np.int16)
CYAN = np.array((56, 189, 248), dtype=np.int16)


def _near_gold(pixel: tuple[int, ...]) -> bool:
    rgb = np.array(pixel[:3], dtype=np.int16)
    return int(np.max(np.abs(rgb - GOLD_RGB))) < 36


def _has_cyan(img: Image.Image) -> bool:
    arr = np.asarray(img.convert("RGB"))
    dist = np.max(np.abs(arr.astype(np.int16) - CYAN), axis=2)
    return bool((dist < 18).any())


def test_inter_loads_from_bundle() -> None:
    font = load_inter(32, bold=True)
    assert font is not None
    assert getattr(font, "path", "").endswith("Inter-Bold.ttf") or "Inter" in type(font).__name__


def test_planned_scene_cannot_keep_lower_third() -> None:
    planned = PlannedScene(start=0, end=8, layout="lower_third", graphic=GraphicCard(kicker="X", title="Y"))
    assert planned.layout is PictureTag.NOTHING


def test_old_layouts_coerce_to_kit_tags() -> None:
    assert Scene(start=0, end=1, layout="FULL_FRAME").layout is PictureTag.NOTHING
    assert Scene(start=0, end=1, layout="PIP_BOTTOM_RIGHT").layout is PictureTag.PIP
    assert Scene(start=0, end=1, layout="SPLIT_TOP").layout is PictureTag.OVERLAY


def test_pip_without_still_falls_back() -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=PictureTag.PIP,
        graphic=GraphicCard(kicker="THE MONEY", title="$1.5B is the floor.", still_query="MQ-9"),
    )
    resolve_scene(scene)
    assert scene.layout is PictureTag.OVERLAY
    assert compose_mode(scene) == "overlay"


def test_bookends_are_forced_and_stacked(tmp_path: Path) -> None:
    del tmp_path
    settings = Settings(bookend_seconds=10)
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=60,
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="THE MONEY", title="$1.5B is the floor."),
            )
        ],
        talk_sheet=TalkSheet(
            title="SKYNET IS COMING · PART 2",
            exec_headline="$1.5B is the floor.\nNot the program.",
        ),
    )
    script = enforce_pacing(script, 60.0, settings)
    assert script.scenes[0].role == "open"
    assert script.scenes[0].layout is PictureTag.LOWER_THIRD
    assert script.scenes[0].graphic.kicker == "SKYNET IS COMING · PART 2"
    assert "floor" in script.scenes[0].graphic.title
    assert script.scenes[-1].role == "close"
    assert script.scenes[-1].layout is PictureTag.LOWER_THIRD
    assert script.scenes[-1].graphic.kicker == "WORK WITH ME"
    assert "Vendor-agnostic" in script.scenes[-1].graphic.title
    assert compose_mode(script.scenes[0]) == "bookend"
    assert compose_mode(script.scenes[-1]) == "bookend"
    body = [scene for scene in script.scenes if scene.role == "body"]
    assert all(scene.layout is not PictureTag.LOWER_THIRD for scene in body)
    assert all(not scene.micro_events for scene in script.scenes)
    point_one = [scene for scene in body if scene.layout is PictureTag.OVERLAY]
    assert point_one
    assert point_one[0].graphic.kicker == "THE MONEY"
    assert script.scenes[0].graphic.kicker != "THE MONEY"


def test_open_card_comes_from_talk_sheet_not_point_one() -> None:
    settings = Settings(bookend_seconds=10)
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=30,
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="THE MONEY", title="$1.5B in Procurements. That's the Floor."),
            )
        ],
        talk_sheet=TalkSheet(
            open_card=BookendCard(
                kicker="SKYNET IS COMING · PART 2",
                headline="$1.5B is the floor.\nNot the program.",
                icon="bar_chart",
            )
        ),
    )
    script = enforce_pacing(script, 60.0, settings)
    opened = script.scenes[0]
    assert opened.role == "open"
    assert opened.graphic.kicker == "SKYNET IS COMING · PART 2"
    assert "Not the program" in opened.graphic.title
    assert opened.graphic.kicker != "THE MONEY"
    body = [scene for scene in script.scenes if scene.role == "body" and scene.layout is PictureTag.OVERLAY]
    assert body[0].graphic.kicker == "THE MONEY"


def test_empty_open_card_does_not_steal_point_one() -> None:
    settings = Settings(bookend_seconds=10)
    script = EditScript(
        scenes=[
            Scene(
                start=12,
                end=24,
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(kicker="THE MONEY", title="$1.5B is the floor."),
            )
        ]
    )
    script = enforce_pacing(script, 60.0, settings)
    opened = script.scenes[0]
    assert opened.role == "open"
    assert opened.graphic.kicker != "THE MONEY"
    assert opened.graphic.title != "$1.5B is the floor."
    closed = script.scenes[-1]
    assert closed.role == "close"
    assert closed.graphic.kicker == "WORK WITH ME"


def test_empty_cut_is_bookends_plus_nothing() -> None:
    script = enforce_pacing(EditScript.empty(), 120.0, Settings(bookend_seconds=10))
    assert script.scenes[0].role == "open"
    assert script.scenes[-1].role == "close"
    middles = script.scenes[1:-1]
    assert middles
    assert all(scene.layout is PictureTag.NOTHING for scene in middles)


def test_identity_is_config_not_model() -> None:
    identity = HostIdentity()
    assert identity.name == "Scott Mastin"
    assert "SDVOSB" in identity.title_line
    assert "Project Maven" in identity.affiliations
    assert "aieval.org" in identity.find_me
    apply_bookends(EditScript(), 30.0, Settings())
    # model never supplies FIND ME / WRAP
    assert "WRAP" not in identity.find_me_kicker


def test_kit_fixtures_paint_three_chromes(tmp_path: Path) -> None:
    files = render_kit_fixtures(tmp_path)
    for key in ("overlay", "pip", "bookend_open", "bookend_close", "nothing"):
        assert files[key].is_file()
        img = Image.open(files[key])
        assert img.size == (1920, 1080)
        assert not _has_cyan(img)

    overlay = Image.open(files["overlay"])
    ox, oy, ow, _ = overlay_rect()
    plate = overlay.getpixel((ox + 40, oy + 40))
    assert plate[0] < 40 and plate[1] < 40 and plate[2] < 40
    # Gold kicker lives in the plate, not a cyan takeaway pill.
    crop = overlay.crop((ox, oy, ox + ow, oy + 220))
    gold_hits = sum(1 for px in crop.getdata() if _near_gold(px))
    assert gold_hits > 80

    pip = Image.open(files["pip"])
    x, y, box_w, box_h = pip_rect()
    assert (x, y, box_w, box_h) == (1320, 725, 560, 315)
    border = pip.getpixel((x + 2, y + 20))
    assert _near_gold(border)
    # Host window is a scaled frame, not a wipe of the still.
    inside = pip.getpixel((x + box_w // 2, y + box_h // 2))
    still_bg = pip.getpixel((200, 200))
    assert inside != still_bg

    opened = Image.open(files["bookend_open"])
    closed = Image.open(files["bookend_close"])
    lx, ly, lw, lh = lower_third_rect()
    assert lh == 220
    rule = opened.getpixel((lx + 80, ly + 2))
    assert _near_gold(rule)
    # Close has the same identity bar, no WRAP-only kicker bar.
    close_rule = closed.getpixel((lx + 80, ly + 2))
    assert _near_gold(close_rule)
    right = closed.crop((lx + int(lw * 0.62), ly, lx + lw, ly + lh))
    assert any(_near_gold(px) for px in right.getdata())

    icons = [draw_icon(name, 72) for name in ICON_NAMES]
    assert all(icon.size == (72, 72) for icon in icons)
    assert any(_near_gold(px) for px in icons[0].getdata())
