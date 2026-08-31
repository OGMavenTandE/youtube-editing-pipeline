from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from pipeline.layouts import (
    GOLD,
    OVERLAY_PAD,
    OVERLAY_W,
    PictureTag,
    lower_third_rect,
    overlay_rect,
    pip_rect,
)
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
from pipeline.picture_kit import (
    ICON_NAMES,
    KitScale,
    _fit_headline,
    draw_icon,
    load_inter,
    render_kit_fixtures,
    render_overlay,
    render_pip_type,
)
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
    still_bg = pip.getpixel((1000, 400))
    assert inside != still_bg
    px, py, _, _ = overlay_rect()
    plate = pip.getpixel((px + 40, py + 40))
    assert plate[0] < 40 and plate[1] < 40 and plate[2] < 40

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


def _composite_chrome(still: Image.Image, chrome: np.ndarray) -> Image.Image:
    base = still.convert("RGBA")
    base.alpha_composite(Image.fromarray(chrome))
    return base.convert("RGB")


def _opaque_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    assert xs.size and ys.size
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _rects_intersect(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> bool:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    return not (ax0 + aw <= bx0 or bx0 + bw <= ax0 or ay0 + ah <= by0 or by0 + bh <= ay0)


def test_long_pip_title_sits_on_dark_plate() -> None:
    title = "Look at this drone laying cable to keep comms with it"
    still = Image.new("RGB", (1920, 1080), (236, 238, 242))
    draw = ImageDraw.Draw(still)
    draw.ellipse((420, 80, 980, 520), fill=(255, 255, 255))
    draw.ellipse((1100, 40, 1680, 460), fill=(250, 250, 252))
    chrome = render_pip_type((1920, 1080), kicker=title, sub="", quote="")
    alpha = chrome[:, :, 3]
    plate = alpha > 80
    x0, y0, x1, y1 = _opaque_bbox(plate)
    pip_box = pip_rect()
    assert 0 <= x0 <= x1 < 1920
    assert 0 <= y0 <= y1 < 1080
    assert not _rects_intersect((x0, y0, x1 - x0 + 1, y1 - y0 + 1), pip_box)
    frame = _composite_chrome(still, chrome)
    plate_crop = np.asarray(frame.crop((x0, y0, x1 + 1, y1 + 1)))
    mean = plate_crop.mean(axis=(0, 1))
    assert float(mean.mean()) < 90
    gold_hits = sum(1 for px in frame.crop((x0, y0, x1 + 1, y1 + 1)).getdata() if _near_gold(px))
    assert gold_hits > 40
    bare = frame.getpixel((1600, 200))
    assert bare[0] > 220 and bare[1] > 220 and bare[2] > 220


def test_short_pip_title_is_left_plate_gold() -> None:
    chrome = render_pip_type((1920, 1080), kicker="REAPER", sub="", quote="")
    ox, oy, ow, _ = overlay_rect()
    arr = chrome
    plate = arr[oy : oy + 180, ox : ox + ow]
    assert plate[:, :, 3].max() > 160
    gold_hits = sum(1 for px in Image.fromarray(plate).getdata() if _near_gold(px[:3]))
    assert gold_hits > 20
    right = arr[oy : oy + 180, 1200:1900, 3]
    assert int(right.max()) < 20


def test_overlay_kicker_fits_inside_plate() -> None:
    long_title = "LOOK AT THIS DRONE LAYING CABLE TO KEEP COMMS WITH IT'S PILOT. ADORABLE."
    chrome = render_overlay(
        (1920, 1080),
        kicker=long_title,
        headline="Humans must be in the loop for an attack, DoW Directive 3000.09",
        icon="bar_chart",
    )
    ox, oy, ow, _ = overlay_rect()
    gold_xs: list[int] = []
    gold_ys: list[int] = []
    rgb = chrome[:, :, :3]
    for y in range(oy, min(oy + 260, 1080)):
        for x in range(0, 1920):
            if _near_gold(tuple(int(v) for v in rgb[y, x])):
                gold_xs.append(x)
                gold_ys.append(y)
    assert gold_xs
    assert max(gold_xs) < ox + ow - 8
    assert min(gold_xs) >= ox
    assert max(gold_ys) < 700
    pip_box = pip_rect()
    assert max(gold_xs) < pip_box[0]
    assert max(gold_ys) < pip_box[1]


DRONE_CABLE = "Look at this drone laying cable to keep comms with it's pilot. Adorable."


def test_empty_still_title_renders_white_content_only() -> None:
    chrome = render_pip_type((1920, 1080), kicker="", sub=DRONE_CABLE)
    expected = render_overlay(
        (1920, 1080),
        kicker="",
        headline=DRONE_CABLE,
        icon="bar_chart",
        max_headline_lines=2,
    )
    assert np.array_equal(chrome, expected)
    split = render_overlay(
        (1920, 1080),
        kicker="Look at this drone laying cable to keep comms with it's pilot.",
        headline="Adorable.",
        icon="bar_chart",
    )
    assert not np.array_equal(chrome, split)
    ox, oy, ow, _ = overlay_rect()
    kicker_band = chrome[oy : oy + 36, ox : ox + ow]
    gold_hits = sum(1 for px in Image.fromarray(kicker_band).getdata() if _near_gold(px[:3]))
    assert gold_hits < 8


def test_filled_still_title_keeps_full_white_content() -> None:
    chrome = render_pip_type((1920, 1080), kicker="CABLE DRONE", sub=DRONE_CABLE)
    expected = render_overlay(
        (1920, 1080),
        kicker="CABLE DRONE",
        headline=DRONE_CABLE,
        icon="bar_chart",
        max_headline_lines=2,
    )
    assert np.array_equal(chrome, expected)
    split_content = render_overlay(
        (1920, 1080),
        kicker="CABLE DRONE",
        headline="Adorable.",
        icon="bar_chart",
    )
    assert not np.array_equal(chrome, split_content)
    ox, oy, ow, _ = overlay_rect()
    plate = chrome[oy : oy + 220, ox : ox + ow]
    gold_hits = sum(1 for px in Image.fromarray(plate).getdata() if _near_gold(px[:3]))
    assert gold_hits > 20
    pip_box = pip_rect()
    alpha = chrome[:, :, 3]
    ys, xs = np.where(alpha > 80)
    assert int(xs.max()) < pip_box[0]
    assert int(ys.max()) < pip_box[1]


HUMAN_REQUIRED_BODY = (
    "Attack drones are still directed by pilots using joysticks and a video feed."
)


def test_human_required_body_wraps_to_plate_width() -> None:
    """Large type used to drop leftover words at 'using' and leave the plate empty."""
    inner_w = OVERLAY_W - 2 * OVERLAY_PAD
    font, lines, size = _fit_headline(
        HUMAN_REQUIRED_BODY,
        KitScale(1920, 1080),
        inner_w,
        max_lines=2,
    )
    joined = " ".join(lines)
    assert "using" in joined
    assert "joysticks" in joined
    assert "feed" in joined
    assert joined.rstrip(".").endswith("feed") or "video feed" in joined
    assert size < 40
    draw = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    line0_w = draw.textlength(lines[0], font=font)
    assert line0_w >= inner_w * 0.85
    chrome = render_overlay(
        (1920, 1080),
        kicker="HUMAN REQUIRED",
        headline=HUMAN_REQUIRED_BODY,
        icon="bar_chart",
    )
    ox, oy, ow, _ = overlay_rect()
    rgb = chrome[:, :, :3]
    white_xs = [
        x
        for y in range(oy + 28, min(oy + 140, 1080))
        for x in range(ox, ox + ow)
        if int(rgb[y, x, 0]) > 230 and int(rgb[y, x, 1]) > 230 and int(rgb[y, x, 2]) > 230
    ]
    assert white_xs
    assert max(white_xs) - min(white_xs) >= int(inner_w * 0.80)
