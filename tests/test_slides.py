from pathlib import Path

from PIL import Image

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
from pipeline.layouts import LayoutKind
from pipeline.models import EditScript, GraphicCard, Scene


def test_empty_full_frame_is_not_a_slide() -> None:
    graphic = GraphicCard()
    assert slide_variant(LayoutKind.FULL_FRAME, graphic) is None


def test_pip_variant_follows_bullet_count() -> None:
    claim = GraphicCard(title="One idea")
    listed = GraphicCard(title="List", bullets=["A", "B"])
    assert slide_variant(LayoutKind.PIP_BOTTOM_RIGHT, claim) is SlideVariant.PIP_CLAIM
    assert slide_variant(LayoutKind.PIP_BOTTOM_RIGHT, listed) is SlideVariant.PIP_LIST
    assert slide_variant(LayoutKind.SPLIT_TOP, claim) is SlideVariant.SPLIT


def test_collect_jobs_dedupes_slide_id_and_adds_lower_third() -> None:
    card = GraphicCard(
        title="Keep the hook",
        bullets=["Say it once", "Repeat the number"],
        slide_id="slide_hook",
        lower_third_title="Scott",
        lower_third_subtitle="Host",
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=12, layout=LayoutKind.PIP_BOTTOM_RIGHT, graphic=card),
            Scene(start=12, end=24, layout=LayoutKind.PIP_BOTTOM_RIGHT, graphic=card.model_copy()),
            Scene(start=24, end=36, layout=LayoutKind.FULL_FRAME, graphic=GraphicCard()),
        ]
    )
    jobs = collect_slide_jobs(script, Path("/tmp/slides"))
    variants = {job.variant for job in jobs}
    assert variants == {SlideVariant.PIP_LIST, SlideVariant.LOWER_THIRD}
    assert len(jobs) == 2
    stamp_slide_paths(script, Path("/tmp/slides"))
    assert script.scenes[0].graphic.asset_path.endswith("slide_hook_pip.png")
    assert script.scenes[0].graphic.lower_third_path.endswith("slide_hook_lt.png")
    assert script.scenes[2].graphic.asset_path == ""


def test_stable_id_from_copy() -> None:
    a = GraphicCard(title="Same", bullets=["One"])
    b = GraphicCard(title="Same", bullets=["One"])
    assert ensure_slide_id(a, LayoutKind.SPLIT_TOP) == ensure_slide_id(b, LayoutKind.SPLIT_TOP)
    assert ensure_slide_id(a, LayoutKind.SPLIT_TOP) != ensure_slide_id(a, LayoutKind.PIP_BOTTOM_RIGHT)


def test_html_escapes_copy() -> None:
    html_doc = build_slide_html(
        BrollSpec(
            title='Hook <em>now</em>',
            bullets=['Use "quotes"'],
            variant=SlideVariant.PIP_LIST,
        )
    )
    assert "<em>" not in html_doc
    assert "&lt;em&gt;now&lt;/em&gt;" in html_doc
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


def test_playwright_renders_1920x1080_pngs(tmp_path: Path | None = None) -> None:
    dest = Path("/tmp/yt-pipe-slide-test")
    dest.mkdir(parents=True, exist_ok=True)
    for item in dest.glob("*.png"):
        item.unlink()
    settings = Settings(slides_dir=dest)
    shared = GraphicCard(
        title="Cut the pause, keep the point",
        bullets=["Drop gaps over 0.7s", "Leave a 0.15s pad", "Stay on the idea"],
        slide_id="demo_list",
        lower_third_title="Scott Mastin",
        lower_third_subtitle="YouTube edit",
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=12, layout=LayoutKind.PIP_BOTTOM_RIGHT, graphic=shared),
            Scene(start=12, end=24, layout=LayoutKind.PIP_BOTTOM_RIGHT, graphic=shared.model_copy()),
            Scene(
                start=24,
                end=36,
                layout=LayoutKind.SPLIT_TOP,
                graphic=GraphicCard(title="One number matters", slide_id="demo_split"),
            ),
            Scene(start=36, end=48, layout=LayoutKind.FULL_FRAME, graphic=GraphicCard()),
        ]
    )
    assets = render_slides(script, settings)
    assert len(assets) == 3
    pip = Image.open(dest / "demo_list_pip.png")
    split = Image.open(dest / "demo_split_split.png")
    lower = Image.open(dest / "demo_list_lt.png")
    assert pip.size == (1920, 1080)
    assert split.size == (1920, 1080)
    assert lower.size == (1920, 1080)
    assert pip.mode in {"RGB", "RGBA"}
    pip_px = pip.convert("RGBA").getpixel((80, 80))
    assert pip_px[3] == 255
    top = split.convert("RGBA").getpixel((960, 80))
    assert top[3] == 0
    band = split.convert("RGBA").getpixel((200, 900))
    assert band[3] == 255
    lt_empty = lower.convert("RGBA").getpixel((1800, 80))
    assert lt_empty[3] == 0
    assert script.scenes[0].graphic.asset_path.endswith("demo_list_pip.png")
    assert script.scenes[1].graphic.asset_path == script.scenes[0].graphic.asset_path
    assert script.scenes[3].graphic.asset_path == ""
