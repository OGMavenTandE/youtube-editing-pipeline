"""YouTube Studio paste package: local folder, not an API upload."""

from __future__ import annotations

import base64
import html
import os
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.broll.slides import PlaywrightNotFoundError, _chromium_help
from pipeline.config import Settings
from pipeline.media import extract_frame, probe_duration
from pipeline.models import ChapterMarker, YouTubeMetadata

TITLE_CHAR_LIMIT = 100
DESCRIPTION_CHAR_LIMIT = 5000
THUMB_WIDTH = 1280
THUMB_HEIGHT = 720
THUMB_MAX_BYTES = 2 * 1024 * 1024
MIN_CHAPTER_SECONDS = 10.0
MIN_CHAPTER_COUNT = 3
MIN_PAD_DURATION = 30.0

TEMPLATE_PATH = Path(__file__).resolve().parent / "broll" / "templates" / "thumbnail.html"

_CHAPTER_LINE = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s+\S+")
_CHAPTER_HEADER = re.compile(
    r"^\s*(chapters?|timestamps?|table of contents|chapters below)\s*:?\s*$",
    re.IGNORECASE,
)


class StudioPackage(BaseModel):
    """Paths and paste-ready copy written to output/<stem>_studio/."""

    directory: Path
    video_path: Path
    titles_path: Path
    description_path: Path
    tags_path: Path
    thumbnail_path: Path
    titles: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    chapters: list[ChapterMarker] = Field(default_factory=list)


def format_chapter_timestamp(seconds: float) -> str:
    """YouTube chapter clock: m:ss under an hour, h:mm:ss at or over an hour."""
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def sanitize_chapters(
    chapters: list[ChapterMarker],
    duration: float,
) -> list[ChapterMarker]:
    """Make a chapter list Studio will actually turn into chapters.

    First mark is 0:00. Gaps are at least 10 seconds. At least three chapters
    when the cut is long enough. Sub-10s marks are dropped.
    """
    duration = max(0.0, float(duration))
    cleaned = [
        ChapterMarker(start=max(0.0, float(chapter.start)), title=chapter.title.strip())
        for chapter in chapters
        if chapter.title.strip()
    ]
    cleaned.sort(key=lambda item: item.start)
    if not cleaned:
        cleaned = [ChapterMarker(start=0.0, title="Intro")]
    else:
        cleaned[0] = ChapterMarker(start=0.0, title=cleaned[0].title)

    merged = [cleaned[0]]
    for chapter in cleaned[1:]:
        if chapter.start - merged[-1].start < MIN_CHAPTER_SECONDS:
            continue
        if duration > 0 and chapter.start >= duration:
            continue
        if duration > 0 and duration - chapter.start < MIN_CHAPTER_SECONDS:
            continue
        merged.append(chapter)

    if len(merged) < MIN_CHAPTER_COUNT and duration >= MIN_PAD_DURATION:
        labels = [chapter.title for chapter in merged]
        while len(labels) < MIN_CHAPTER_COUNT:
            labels.append(f"Part {len(labels) + 1}")
        third = duration / 3.0
        starts = [
            0.0,
            max(MIN_CHAPTER_SECONDS, third),
            max(20.0, min(duration - MIN_CHAPTER_SECONDS, third * 2)),
        ]
        if starts[1] - starts[0] < MIN_CHAPTER_SECONDS:
            starts[1] = MIN_CHAPTER_SECONDS
        if starts[2] - starts[1] < MIN_CHAPTER_SECONDS:
            starts[2] = starts[1] + MIN_CHAPTER_SECONDS
        if duration - starts[2] < MIN_CHAPTER_SECONDS:
            starts[2] = duration - MIN_CHAPTER_SECONDS
        merged = [
            ChapterMarker(start=starts[0], title=labels[0]),
            ChapterMarker(start=starts[1], title=labels[1]),
            ChapterMarker(start=starts[2], title=labels[2]),
        ]
    return merged


def format_chapter_block(chapters: list[ChapterMarker]) -> str:
    lines = []
    for chapter in chapters:
        title = " ".join(chapter.title.split())
        lines.append(f"{format_chapter_timestamp(chapter.start)} {title}")
    return "\n".join(lines)


def strip_existing_chapters(text: str) -> str:
    """Drop Gemini's inline timestamp list so we can append a legal block."""
    kept: list[str] = []
    for line in text.splitlines():
        if _CHAPTER_LINE.match(line) or _CHAPTER_HEADER.match(line):
            continue
        kept.append(line)
    body = "\n".join(kept)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


def clip_title(title: str, limit: int = TITLE_CHAR_LIMIT) -> str:
    cleaned = " ".join(title.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip()


def ensure_titles(titles: list[str], fallback: str) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for title in titles:
        cleaned = " ".join(title.split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    base = " ".join(fallback.split()) or "Untitled"
    while len(unique) < 5:
        suffix = f" ({len(unique) + 1})" if unique else ""
        candidate = f"{base}{suffix}".strip() or f"Untitled ({len(unique) + 1})"
        if candidate.casefold() in seen:
            candidate = f"{base} ({len(unique) + 1})"
        unique.append(candidate)
        seen.add(candidate.casefold())
    return unique[:5]


def format_titles_file(titles: list[str]) -> str:
    return "\n".join(clip_title(title) for title in titles) + "\n"


def format_tags_file(tags: list[str]) -> str:
    cleaned = [tag.strip() for tag in tags if tag.strip()]
    return ", ".join(cleaned) + ("\n" if cleaned else "")


def assemble_description(
    body: str,
    chapters: list[ChapterMarker],
    *,
    fallback: str = "",
    max_chars: int = DESCRIPTION_CHAR_LIMIT,
) -> str:
    """SEO body, then a YouTube-legal chapter block. Stays under 5000 chars."""
    text = strip_existing_chapters(body or "")
    if not text:
        text = " ".join((fallback or "").split())
    chapter_block = format_chapter_block(chapters)
    reserve = len(chapter_block) + (2 if chapter_block else 0)
    max_body = max(0, max_chars - reserve)
    if len(text) > max_body:
        text = text[:max_body].rstrip()
    if text and chapter_block:
        assembled = f"{text}\n\n{chapter_block}"
    else:
        assembled = text or chapter_block
    return assembled[:max_chars]


def build_studio_texts(
    metadata: YouTubeMetadata,
    duration: float,
    *,
    fallback_title: str = "",
) -> tuple[list[str], str, list[str], list[ChapterMarker]]:
    titles = ensure_titles(metadata.titles, fallback_title)
    chapters = sanitize_chapters(metadata.chapters, duration)
    description = assemble_description(
        metadata.description,
        chapters,
        fallback=titles[0],
    )
    tags = [tag.strip() for tag in metadata.tags if tag.strip()]
    return titles, description, tags, chapters


def build_thumbnail_html(title: str, frame_src: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("{{title}}", html.escape(clip_title(title))).replace(
        "{{frame_src}}", frame_src
    )


def _frame_data_uri(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def pick_frame_time(duration: float) -> float:
    if duration <= 0:
        return 0.0
    return min(max(duration * 0.25, 0.1), max(0.0, duration - 0.05))


def render_studio_thumbnail(
    title: str,
    webcam_path: Path,
    dest: Path,
    settings: Settings,
    *,
    duration: float | None = None,
) -> Path:
    """1280x720 Playwright card: title option 1 plus a webcam frame."""
    dest = dest.with_suffix(".jpg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if duration is None:
        duration = probe_duration(webcam_path, settings)
    frame_path = settings.work_dir / f"{webcam_path.stem}_thumb_frame.jpg"
    extract_frame(
        webcam_path,
        frame_path,
        settings,
        at_seconds=pick_frame_time(duration),
    )
    html_doc = build_thumbnail_html(title, _frame_data_uri(frame_path))

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except PlaywrightError as exc:
                raise PlaywrightNotFoundError(_chromium_help(exc)) from exc
            try:
                context = browser.new_context(
                    viewport={"width": THUMB_WIDTH, "height": THUMB_HEIGHT},
                    device_scale_factor=1,
                )
                page = context.new_page()
                page.set_content(html_doc, wait_until="load")
                try:
                    page.evaluate("() => document.fonts.ready")
                except PlaywrightError:
                    pass
                page.screenshot(
                    path=str(dest),
                    type="jpeg",
                    quality=82,
                    animations="disabled",
                )
                context.close()
            finally:
                browser.close()
    except PlaywrightNotFoundError:
        raise
    except PlaywrightError as exc:
        raise PlaywrightNotFoundError(_chromium_help(exc)) from exc

    _enforce_thumb_budget(dest)
    return dest


def _enforce_thumb_budget(path: Path) -> None:
    from PIL import Image

    image = Image.open(path)
    if image.size != (THUMB_WIDTH, THUMB_HEIGHT):
        image = image.convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT))
        image.save(path, format="JPEG", quality=80, optimize=True)
    quality = 80
    while path.stat().st_size > THUMB_MAX_BYTES and quality >= 40:
        quality -= 10
        image = Image.open(path).convert("RGB")
        image.save(path, format="JPEG", quality=quality, optimize=True)
    if path.stat().st_size > THUMB_MAX_BYTES:
        raise RuntimeError(
            f"Studio thumbnail is {path.stat().st_size} bytes; YouTube limit is 2MB."
        )


def place_video(src: Path, dest: Path) -> Path:
    """Hardlink the finished MP4, or copy if the filesystem cannot link."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = src.resolve()
    dest = dest.resolve()
    if dest.exists():
        dest.unlink()
    if src == dest:
        return dest
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
    return dest


def write_studio_package(
    *,
    video_path: Path,
    webcam_path: Path,
    metadata: YouTubeMetadata,
    dest_dir: Path,
    settings: Settings,
    fallback_title: str = "",
    duration: float | None = None,
) -> StudioPackage:
    """Write the drag-into-Studio folder next to the pipeline JSON."""
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Final video not found: {video_path}")
    webcam_path = webcam_path.resolve()
    if not webcam_path.is_file():
        raise FileNotFoundError(f"Webcam video not found: {webcam_path}")

    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    settings.ensure_dirs()

    if duration is None:
        duration = probe_duration(video_path, settings)
    titles, description, tags, chapters = build_studio_texts(
        metadata,
        duration,
        fallback_title=fallback_title or video_path.stem.replace("_", " "),
    )

    packaged_video = place_video(video_path, dest_dir / video_path.name)
    titles_path = dest_dir / "titles.txt"
    description_path = dest_dir / "description.txt"
    tags_path = dest_dir / "tags.txt"
    titles_path.write_text(format_titles_file(titles), encoding="utf-8")
    description_path.write_text(description + "\n", encoding="utf-8")
    tags_path.write_text(format_tags_file(tags), encoding="utf-8")

    thumbnail_path = render_studio_thumbnail(
        titles[0],
        webcam_path,
        dest_dir / "thumbnail.jpg",
        settings,
        duration=probe_duration(webcam_path, settings),
    )
    return StudioPackage(
        directory=dest_dir,
        video_path=packaged_video,
        titles_path=titles_path,
        description_path=description_path,
        tags_path=tags_path,
        thumbnail_path=thumbnail_path,
        titles=titles,
        description=description,
        tags=tags,
        chapters=chapters,
    )
