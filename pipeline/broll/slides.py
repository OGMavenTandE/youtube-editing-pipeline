"""Playwright HTML slides: 1920x1080 stills for PIP, split, and lower-thirds."""

from __future__ import annotations

import hashlib
import html
import logging
from dataclasses import dataclass
from pathlib import Path

from pipeline.broll.base import BrollAsset, BrollKind, BrollSpec, SlideVariant
from pipeline.config import Settings
from pipeline.layouts import LayoutKind
from pipeline.models import EditScript, GraphicCard, Scene
from pipeline.shotlist import scene_shows_slide

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "slide.html"
CANVAS = (1920, 1080)


class PlaywrightNotFoundError(RuntimeError):
    """Chromium is missing or Playwright cannot launch it."""


@dataclass(frozen=True)
class SlideJob:
    slide_id: str
    variant: SlideVariant
    spec: BrollSpec
    dest: Path


def ensure_slide_id(graphic: GraphicCard, layout: LayoutKind) -> str:
    if graphic.slide_id.strip():
        return _safe_id(graphic.slide_id.strip())
    raw = "|".join(
        [
            layout.value,
            graphic.kicker.strip(),
            graphic.title.strip(),
            *graphic.bullets,
            graphic.lower_third_title.strip(),
            graphic.lower_third_subtitle.strip(),
        ]
    )
    return "slide_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def slide_variant(layout: LayoutKind, graphic: GraphicCard) -> SlideVariant | None:
    """Chromium slides are retired. The locked kit paints chrome in-process."""
    del layout, graphic
    return None


def needs_lower_third(graphic: GraphicCard) -> bool:
    return bool(graphic.lower_third_title.strip())


def slide_filename(slide_id: str, variant: SlideVariant) -> str:
    if variant is SlideVariant.LOWER_THIRD:
        return f"{slide_id}_lt.png"
    if variant is SlideVariant.SPLIT:
        return f"{slide_id}_split.png"
    return f"{slide_id}_pip.png"


def collect_slide_jobs(script: EditScript, dest_dir: Path) -> list[SlideJob]:
    """No Chromium slide jobs. Picture-kit PNGs are painted by the compositor."""
    del dest_dir
    for scene in script.scenes:
        _assign_id(scene)
    return []


def stamp_slide_paths(script: EditScript, dest_dir: Path) -> None:
    for scene in script.scenes:
        _assign_id(scene)
        if scene_shows_slide(scene):
            variant = slide_variant(scene.layout, scene.graphic)
            if variant is not None:
                scene.graphic.asset_path = str(
                    dest_dir / slide_filename(scene.graphic.slide_id, variant)
                )
        if needs_lower_third(scene.graphic):
            scene.graphic.lower_third_path = str(
                dest_dir / slide_filename(scene.graphic.slide_id, SlideVariant.LOWER_THIRD)
            )


def build_slide_html(spec: BrollSpec) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    kicker = html.escape(spec.kicker.strip())
    kicker_block = f'<p class="kicker">{kicker}</p>' if kicker else ""
    title = html.escape(spec.title.strip())
    title_block = f"<h1>{title}</h1>" if title else ""
    if spec.variant is SlideVariant.PIP_CLAIM and len(spec.bullets) == 1:
        body = f'<p class="lede">{html.escape(spec.bullets[0])}</p>'
    elif spec.bullets:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in spec.bullets[:5])
        body = f"<ul>{items}</ul>"
    else:
        body = ""
    return (
        template.replace("{{variant}}", spec.variant.value)
        .replace("{{kicker_block}}", kicker_block)
        .replace("{{title_block}}", title_block)
        .replace("{{body}}", body)
        .replace("{{lt_title}}", html.escape(spec.lower_third_title.strip()))
        .replace("{{lt_sub}}", html.escape(spec.lower_third_subtitle.strip()))
    )


def render_slides(script: EditScript, settings: Settings) -> list[BrollAsset]:
    """Render unique cards to settings.slides_dir and stamp paths onto scenes."""
    settings.ensure_dirs()
    dest_dir = settings.slides_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs = collect_slide_jobs(script, dest_dir)
    stamp_slide_paths(script, dest_dir)
    if not jobs:
        logger.info("No slides to render")
        return []
    assets = SlideProvider().render_jobs(jobs)
    logger.info("Rendered %s slide asset(s) to %s", len(assets), dest_dir)
    return assets


class SlideProvider:
    """BrollProvider that screenshots HTML templates in one Chromium session."""

    def render(self, spec: BrollSpec) -> BrollAsset:
        dest = spec.asset_path
        if dest is None:
            raise ValueError("BrollSpec.asset_path is required for slide render")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        job = SlideJob(
            slide_id=spec.slide_id or dest.stem,
            variant=spec.variant,
            spec=spec,
            dest=dest,
        )
        assets = self.render_jobs([job])
        return assets[0]

    def render_jobs(self, jobs: list[SlideJob]) -> list[BrollAsset]:
        if not jobs:
            return []
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        assets: list[BrollAsset] = []
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except PlaywrightError as exc:
                    raise PlaywrightNotFoundError(_chromium_help(exc)) from exc
                try:
                    context = browser.new_context(
                        viewport={"width": CANVAS[0], "height": CANVAS[1]},
                        device_scale_factor=1,
                    )
                    page = context.new_page()
                    for job in jobs:
                        page.set_content(build_slide_html(job.spec), wait_until="load")
                        try:
                            page.evaluate("() => document.fonts.ready")
                        except PlaywrightError:
                            pass
                        omit_bg = job.variant in {SlideVariant.SPLIT, SlideVariant.LOWER_THIRD}
                        page.screenshot(
                            path=str(job.dest),
                            type="png",
                            omit_background=omit_bg,
                            animations="disabled",
                        )
                        assets.append(
                            BrollAsset(kind=BrollKind.SLIDE, path=job.dest, duration=None)
                        )
                    context.close()
                finally:
                    browser.close()
        except PlaywrightNotFoundError:
            raise
        except PlaywrightError as exc:
            raise PlaywrightNotFoundError(_chromium_help(exc)) from exc
        return assets


def _assign_id(scene: Scene) -> None:
    scene.graphic.slide_id = ensure_slide_id(scene.graphic, scene.layout)


def _add_job(
    jobs: list[SlideJob],
    seen: set[tuple[str, SlideVariant]],
    scene: Scene,
    variant: SlideVariant,
    dest_dir: Path,
) -> None:
    slide_id = scene.graphic.slide_id
    key = (slide_id, variant)
    if key in seen:
        return
    seen.add(key)
    dest = dest_dir / slide_filename(slide_id, variant)
    spec = BrollSpec(
        kind=BrollKind.SLIDE,
        kicker=scene.graphic.kicker,
        title=scene.graphic.title,
        bullets=list(scene.graphic.bullets),
        layout=scene.layout,
        slide_id=slide_id,
        lower_third_title=scene.graphic.lower_third_title,
        lower_third_subtitle=scene.graphic.lower_third_subtitle,
        variant=variant,
        asset_path=dest,
    )
    jobs.append(SlideJob(slide_id=slide_id, variant=variant, spec=spec, dest=dest))


def _safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned.strip("_") or "slide"


def _chromium_help(_exc: Exception) -> str:
    return (
        "Playwright Chromium is not installed.\n"
        "Open Settings and click Install Chromium, then Recheck.\n"
        "From a source checkout, run: playwright install chromium"
    )
