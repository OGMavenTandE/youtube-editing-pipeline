from pathlib import Path

from pipeline.broll.base import BrollSpec, SlideVariant
from pipeline.broll.slides import (
    _chromium_help,
    build_slide_html,
    collect_slide_jobs,
    ensure_slide_id,
    render_slides,
    slide_filename,
    slide_variant,
    stamp_slide_paths,
)
from pipeline.config import Settings
from pipeline.layouts import PictureTag
from pipeline.models import EditScript, GraphicCard, Scene


def test_kit_tags_are_not_chromium_slides() -> None:
    graphic = GraphicCard(kicker="THE MONEY", title="$1.5B is the floor.")
    assert slide_variant(PictureTag.OVERLAY, graphic) is None
    assert slide_variant(PictureTag.PIP, graphic) is None
    assert slide_variant(PictureTag.NOTHING, graphic) is None
    assert slide_variant(PictureTag.LOWER_THIRD, graphic) is None


def test_collect_jobs_is_empty_for_the_kit() -> None:
    card = GraphicCard(
        kicker="THE MONEY",
        title="$1.5B is the floor.",
        icon="bar_chart",
        slide_id="slide_hook",
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=12, layout=PictureTag.OVERLAY, graphic=card),
            Scene(start=12, end=24, layout=PictureTag.PIP, graphic=card.model_copy()),
            Scene(start=24, end=36, layout=PictureTag.NOTHING, graphic=GraphicCard()),
        ]
    )
    jobs = collect_slide_jobs(script, Path("/tmp/slides"))
    assert jobs == []
    stamp_slide_paths(script, Path("/tmp/slides"))
    assert script.scenes[0].graphic.asset_path == ""


def test_stable_id_from_copy() -> None:
    a = GraphicCard(title="Same", bullets=["One"])
    b = GraphicCard(title="Same", bullets=["One"])
    assert ensure_slide_id(a, PictureTag.OVERLAY) == ensure_slide_id(b, PictureTag.OVERLAY)


def test_html_escapes_copy() -> None:
    html_doc = build_slide_html(
        BrollSpec(
            kicker="Watch <out>",
            title='Hook <em>now</em>',
            bullets=['Use "quotes"'],
            variant=SlideVariant.PIP_LIST,
        )
    )
    assert "<em>" not in html_doc
    assert "&lt;em&gt;now&lt;/em&gt;" in html_doc
    assert "class=\"kicker\"" in html_doc
    assert "pip_list" in html_doc


def test_filename_suffixes() -> None:
    assert slide_filename("slide_a", SlideVariant.PIP_CLAIM) == "slide_a_pip.png"
    assert slide_filename("slide_a", SlideVariant.SPLIT) == "slide_a_split.png"
    assert slide_filename("slide_a", SlideVariant.LOWER_THIRD) == "slide_a_lt.png"


def test_chromium_help_points_at_settings_not_banner() -> None:
    banner = (
        "Playwright Chromium was not available.\n"
        "╔════════════════════════════════════════════╗\n"
        "║     playwright install                     ║\n"
        "╚════════════════════════════════════════════╝\n"
        "Executable doesn't exist at C:\\\\bundled\\\\chrome-headless-shell.exe"
    )
    message = _chromium_help(Exception(banner))
    assert "Open Settings and click Install Chromium" in message
    assert "Recheck" in message
    assert "╔" not in message
    assert "Executable doesn't exist" not in message
    assert banner not in message


def test_playwright_launches_are_headless() -> None:
    slides = Path(__file__).resolve().parent.parent / "pipeline" / "broll" / "slides.py"
    studio = Path(__file__).resolve().parent.parent / "pipeline" / "studio.py"
    check = Path(__file__).resolve().parent.parent / "desktop" / "ffmpeg_check.py"
    for path in (slides, studio, check):
        text = path.read_text(encoding="utf-8")
        assert "chromium.launch(headless=True)" in text
        assert "chromium.launch()" not in text.replace("chromium.launch(headless=True)", "")


def test_render_slides_is_a_noop_for_the_kit(tmp_path: Path) -> None:
    settings = Settings(slides_dir=tmp_path)
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=12,
                layout=PictureTag.OVERLAY,
                graphic=GraphicCard(
                    kicker="THE MONEY",
                    title="$1.5B is the floor.",
                    icon="bar_chart",
                    slide_id="demo_list",
                ),
            )
        ]
    )
    assets = render_slides(script, settings)
    assert assets == []
    assert script.scenes[0].graphic.asset_path == ""
