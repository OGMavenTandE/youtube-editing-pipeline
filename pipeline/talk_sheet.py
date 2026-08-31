"""Talk-sheet persistence, markdown import, user locks, and still naming."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from pipeline.layouts import PictureTag
from pipeline.models import (
    TALK_CARDS_PER_POINT,
    TALK_POINT_COUNT,
    EditScript,
    FieldSource,
    GraphicCard,
    Scene,
    TalkPoint,
    TalkSheet,
    field_is_locked,
)
from pipeline.stills import IMAGE_SUFFIXES, match_local_still

DOCUMENTS_PIPELINE = Path("Documents") / "Youtube Pipeline"
DEFAULT_TALK_SHEET_MD = "talk_sheet.md"
LAST_USED_NAME = "talk_sheet.json"

_POINT_HEAD = re.compile(r"^(?:#{1,3}\s*)?point\s*(\d)\b", re.IGNORECASE)
_CLOSE_HEAD = re.compile(
    r"^(?:#{1,3}\s*)?(close|identity|find me|work with me|host)\b",
    re.IGNORECASE,
)
_OVERVIEW_HEAD = re.compile(
    r"^(?:#{1,3}\s*)?(overview|exec(?:utive)?(?:\s+summary)?|thesis)\b",
    re.IGNORECASE,
)
_TITLE_LINE = re.compile(r"^(?:#{1,3}\s*)?(?:title|kicker)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_CARD_TITLE_LINE = re.compile(
    r"^(?:card\s*)?(\d)\s*(?:title|kicker)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_TITLE_INDEX_LINE = re.compile(r"^(?:title|kicker)\s*(\d)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_IMAGE_TITLE_LINE = re.compile(
    r"^(?:image[- ]?title|still[- ]?kicker|pip[- ]?kicker)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_IMAGE_TEXT_LINE = re.compile(
    r"^(?:image[- ]?text|still[- ]?title|pip[- ]?title|on[- ]?still)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_PLATFORM_LINE = re.compile(
    r"^(?:platform|still(?:\s+query)?|query|hardware)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_CARD_LINE = re.compile(r"^(?:card\s*)(\d)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*]|\d+[.)])\s+(.+)$")
_NOTES_HEAD = re.compile(r"^(?:spoken[- ]?exec(?:utive)?[- ]?notes|spoken notes|notes)\s*[:\-]\s*(.*)$", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9]+")
_DOD_PHRASE = re.compile(r"\bdepartment of defense\b", re.IGNORECASE)
_DOD_ABBR = re.compile(r"\bdod\b", re.IGNORECASE)
_CITATION = re.compile(
    r"""(?ix)
    \b(?:directive|instruction|statute|regulation|executive\s+order|\beo\b|public\s+law|\bpl\b)
    (?:\s+[\d.]+)?
    |
    \b\d+\s*u\.?s\.?c\.?\b
    |
    \b\d{2,4}\.\d{2,}\b
    """
)
_KICKER_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "is",
        "are",
        "was",
        "be",
        "do",
        "does",
        "not",
        "with",
        "each",
        "other",
        "that",
        "this",
        "it",
        "as",
        "from",
        "by",
        "they",
    }
)
PIP_HOLD_SECONDS = 8.0

KNOWN_MARKDOWN_SHAPE = """\
# SKYNET IS COMING · PART 2

## Overview
$1.5B is the floor.
Not the program.

Spoken notes:
Title plus the two-line thesis. Not painted.

## Point 1
Platform: MQ-9 Reaper
Image title: MQ-9 REAPER
Image text: Reaper on station
Title 1: THE MONEY
- $1.5B in procurements. That's the floor.
Title 2: EVEN LOW
- I think that's even low.
Title 3: STACKING
- Programs are stacking, not replacing.

## Point 2
Platform: M1 Abrams
Image title: M1 ABRAMS
Image text: The vehicle is the named still.
Title 1: NAMED STILL
- The vehicle is the named still.
- Overlay copy stays a spoken sentence.

## Point 3
Platform: Patriot
Image title: PATRIOT
Image text: Battery on the pad.
Title 1: LAST POINT
- Last point, first card.
- Last point, second card.
- Last point, third card.

## Close
WORK WITH ME
Independent AI T&E.
Vendor-agnostic.
"""


def documents_pipeline_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / DOCUMENTS_PIPELINE


def default_talk_sheet_md_path(home: Path | None = None) -> Path:
    return documents_pipeline_dir(home) / DEFAULT_TALK_SHEET_MD


def default_stills_dir(*, home: Path | None = None, work_dir: Path | None = None) -> Path:
    """Documents/Youtube Pipeline/stills, or work/stills if Documents is not writable."""
    preferred = documents_pipeline_dir(home) / "stills"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = (work_dir or Path.cwd() / "work") / "stills"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def last_used_talk_sheet_path(user_data_dir: Path) -> Path:
    return Path(user_data_dir) / LAST_USED_NAME


def job_talk_sheet_path(video_path: Path) -> Path:
    """JSON next to the video stem: <stem>_talk_sheet.json."""
    path = Path(video_path)
    return path.with_name(f"{path.stem}_talk_sheet.json")


def output_talk_sheet_path(stem: str, output_dir: Path) -> Path:
    return Path(output_dir) / f"{stem}_talk_sheet.json"


def point_still_filename(point_index: int, platform: str, suffix: str, *, stem: str = "") -> str:
    tokens = [token for token in _TOKEN.findall((platform or "").casefold())]
    slug = "-".join(tokens[:8]) if tokens else "still"
    ext = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
    if ext not in IMAGE_SUFFIXES:
        ext = ".jpg"
    prefix = f"{_safe_stem(stem)}_" if stem else ""
    return f"{prefix}point{point_index}_{slug}{ext}"


def copy_point_still(
    src: Path,
    dest_dir: Path,
    point_index: int,
    platform: str,
    *,
    stem: str = "",
) -> Path:
    """Copy a browsed still so match_local_still can find point index + platform tokens."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"Still not found: {src}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / point_still_filename(point_index, platform, src.suffix, stem=stem)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest.resolve()


def materialize_job_stills(sheet: TalkSheet, dest_dir: Path, *, stem: str = "") -> TalkSheet:
    """Copy any local stills into dest_dir and rewrite still_path to the copy."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for index, point in enumerate(sheet.points, start=1):
        raw = (point.still_path or "").strip()
        if not raw:
            continue
        src = Path(raw)
        if not src.is_file():
            continue
        copied = copy_point_still(src, dest_dir, index, point.platform, stem=stem)
        point.still_path = str(copied)
        if point.still_source == "empty":
            point.still_source = "user"
    return sheet


def load_talk_sheet(path: Path) -> TalkSheet:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return TalkSheet()
    return TalkSheet.model_validate(raw)


def save_talk_sheet(sheet: TalkSheet, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(sheet.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def discover_talk_sheet(
    *,
    explicit: Path | None = None,
    video_path: Path | None = None,
    output_dir: Path | None = None,
    last_used: Path | None = None,
) -> tuple[TalkSheet, Path | None]:
    """Load the first existing JSON. Explicit path wins."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    if video_path is not None:
        candidates.append(job_talk_sheet_path(video_path))
    if video_path is not None and output_dir is not None:
        candidates.append(output_talk_sheet_path(Path(video_path).stem, output_dir))
    if last_used is not None:
        candidates.append(Path(last_used))
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return load_talk_sheet(path), path
    return TalkSheet(), None


def persist_talk_sheet(
    sheet: TalkSheet,
    *,
    video_path: Path | None = None,
    output_dir: Path | None = None,
    last_used: Path | None = None,
) -> list[Path]:
    written: list[Path] = []
    if video_path is not None:
        written.append(save_talk_sheet(sheet, job_talk_sheet_path(video_path)))
    if video_path is not None and output_dir is not None:
        written.append(save_talk_sheet(sheet, output_talk_sheet_path(Path(video_path).stem, output_dir)))
    if last_used is not None:
        written.append(save_talk_sheet(sheet, last_used))
    return written


def rewrite_house_style(text: str) -> str:
    """Auto copy says Department of War / DOW, never DoD."""
    if not text:
        return ""
    out = _DOD_PHRASE.sub("Department of War", text)

    def _abbr(match: re.Match[str]) -> str:
        raw = match.group(0)
        return "DOW" if raw.isupper() or raw[:1].isupper() else "dow"

    return _DOD_ABBR.sub(_abbr, out)


def has_banned_defense_name(text: str) -> bool:
    return bool(_DOD_PHRASE.search(text or "") or _DOD_ABBR.search(text or ""))


def looks_like_citation(text: str) -> bool:
    return bool(_CITATION.search(text or ""))


def citation_allowed(text: str, source: str) -> bool:
    """True when each citation-like span already appears in user sheet or transcript."""
    haystack = (source or "").casefold()
    if not haystack:
        return False
    for match in _CITATION.finditer(text or ""):
        span = re.sub(r"\s+", " ", match.group(0)).strip().casefold()
        if len(span) < 3:
            continue
        if span not in haystack:
            return False
    return True


def is_invented_citation(text: str, source: str) -> bool:
    return looks_like_citation(text) and not citation_allowed(text, source)


def derive_kicker(headline: str, *, said: str = "", platform: str = "") -> str:
    """Short gold label from the spoken beat. Never a statute the source did not say."""
    blob = rewrite_house_style((headline or said or platform or "").strip())
    if re.search(r"department of war", blob, re.IGNORECASE) and not (headline or said).strip():
        return "DEPARTMENT OF WAR"
    words = [re.sub(r"^[^\w$]+|[^\w$]+$", "", word) for word in blob.replace("\n", " ").split()]
    words = [word for word in words if word]
    keep = [
        word
        for word in words
        if word.casefold() not in _KICKER_STOP and not re.fullmatch(r"\d+(\.\d+)?", word)
    ]
    picked = keep[:4] or words[:3] or ([platform.strip()] if platform.strip() else [])
    if not picked:
        return "POINT"
    label = rewrite_house_style(" ".join(picked[:4])).strip()
    return re.sub(r"\s+", " ", label).upper()[:40].strip() or "POINT"


def resolve_auto_kicker(
    candidate: str,
    *,
    headline: str = "",
    said: str = "",
    platform: str = "",
    allowed: str = "",
) -> str:
    """Keep a short source-derived label. Drop invented cites and DoD auto copy."""
    text = rewrite_house_style((candidate or "").strip())
    if text and not is_invented_citation(text, allowed) and not has_banned_defense_name(text):
        return re.sub(r"\s+", " ", text).upper()[:40].strip()
    if text and looks_like_citation(text) and citation_allowed(text, allowed):
        cleaned = rewrite_house_style(text)
        if not has_banned_defense_name(cleaned):
            return re.sub(r"\s+", " ", cleaned).upper()[:40].strip()
    return derive_kicker(headline, said=said, platform=platform)


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _is_point_image_text(text: str, point: TalkPoint) -> bool:
    """True when overlay gold is still copy (title or text), including a longer typed variant."""
    label = _norm_label(text)
    if not label:
        return False
    for image in (_norm_label(point.image_title), _norm_label(point.image_text)):
        if not image:
            continue
        if label == image:
            return True
        if len(image) < 16 and len(image.split()) < 5:
            continue
        if image in label or label in image:
            return True
    return False


def still_plate_copy(point: TalkPoint) -> tuple[str, str]:
    """Gold kicker, white content. Empty title means content-only. Never split image_text."""
    return (point.image_title or "").strip(), (point.image_text or "").strip()


def resolve_auto_image_text(
    *,
    cards: list[str] | None = None,
    said: str = "",
    platform: str = "",
    pip_title: str = "",
    allowed: str = "",
) -> str:
    """White still content from the point. Not a short kicker. Never split on period."""
    for raw in (*(cards or []), pip_title, said, platform):
        text = rewrite_house_style((raw or "").strip())
        if not text:
            continue
        if is_invented_citation(text, allowed):
            continue
        if has_banned_defense_name(text):
            continue
        return text
    return rewrite_house_style((platform or "").strip())


def _still_basename_label(path: str) -> str:
    stem = Path(path or "").stem
    if not stem:
        return ""
    cleaned = re.sub(r"^(?:[\w-]+_)?point\d+_", "", stem, flags=re.I)
    cleaned = cleaned.replace("-", " ").replace("_", " ")
    tokens = [token for token in cleaned.split() if token]
    return " ".join(tokens[:6])


def _overlay_kicker_for_card(point: TalkPoint, card_i: int, scene: Scene, allowed: str) -> str:
    """Card Title is the gold kicker. Image text never fills this slot."""
    headline = point.cards[card_i].strip() or scene.graphic.title
    if point.title_locked(card_i) and not _is_point_image_text(point.titles[card_i], point):
        return point.titles[card_i]
    candidate = point.titles[card_i].strip()
    if candidate and _is_point_image_text(candidate, point):
        candidate = ""
    return resolve_auto_kicker(
        candidate,
        headline=headline,
        said=scene.said,
        platform=point.platform,
        allowed=allowed,
    )


def _strip_image_text_from_overlay(scene: Scene, sheet: TalkSheet) -> None:
    """Hard wall: still image text cannot be the overlay gold line."""
    if scene.layout is not PictureTag.OVERLAY or scene.role != "body":
        return
    for point in sheet.points:
        if not _is_point_image_text(scene.graphic.kicker, point):
            continue
        replacement = ""
        for card_i, title in enumerate(point.titles):
            if point.title_locked(card_i) and title.strip() and not _is_point_image_text(title, point):
                replacement = title
                break
        scene.graphic.kicker = replacement or derive_kicker(
            point.cards[0].strip() or scene.graphic.title,
            said=scene.said,
            platform=point.platform,
        )
        return


def sheet_source_text(sheet: TalkSheet, transcript: str = "") -> str:
    parts = [sheet.title, sheet.exec_headline, sheet.exec_notes, transcript]
    for point in sheet.points:
        parts.append(point.platform)
        parts.append(point.image_title)
        parts.append(point.image_text)
        parts.extend(point.titles)
        parts.extend(point.cards)
    return "\n".join(part for part in parts if part and str(part).strip())


def talk_sheet_to_markdown(sheet: TalkSheet) -> str:
    """Export the Gem-fillable shape, including per-card titles and image text."""
    line1, line2 = sheet.headline_lines()
    lines = [
        f"# {sheet.title.strip() or 'Talk sheet'}",
        "",
        "## Overview",
    ]
    if line1:
        lines.append(line1)
    if line2:
        lines.append(line2)
    if sheet.exec_notes.strip():
        lines.append("")
        lines.append(f"Spoken notes: {sheet.exec_notes.strip()}")
    for index, point in enumerate(sheet.points, start=1):
        lines.extend(["", f"## Point {index}", f"Platform: {point.platform}".rstrip()])
        lines.append(f"Image title: {point.image_title}".rstrip())
        lines.append(f"Image text: {point.image_text}".rstrip())
        for card_i in range(TALK_CARDS_PER_POINT):
            lines.append(f"Title {card_i + 1}: {point.titles[card_i]}".rstrip())
            lines.append(f"Card {card_i + 1}: {point.cards[card_i]}".rstrip())
    lines.extend(
        [
            "",
            "## Close",
            "WORK WITH ME",
            "Independent AI T&E.",
            "Vendor-agnostic.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_talk_sheet_markdown(text: str, *, base: TalkSheet | None = None) -> TalkSheet:
    """Map Title, Overview headline, Point platform, and Point cards.

    Close / identity / HOST_* / WORK WITH ME are ignored so paste cannot
    overwrite the locked close bookend.
    """
    sheet = (base or TalkSheet()).model_copy(deep=True)
    _reset_user_import_slots(sheet)
    section = "title"
    point_index = 0
    overview_lines: list[str] = []
    notes_lines: list[str] = []
    in_notes = False

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_notes:
                notes_lines.append("")
            continue

        if _CLOSE_HEAD.match(stripped):
            section = "close"
            in_notes = False
            continue
        point_match = _POINT_HEAD.match(stripped)
        if point_match:
            section = "point"
            in_notes = False
            point_index = max(1, min(TALK_POINT_COUNT, int(point_match.group(1))))
            rest = stripped[point_match.end() :].lstrip(" :#-")
            if rest:
                _set_platform(sheet, point_index, rest)
            continue
        if _OVERVIEW_HEAD.match(stripped):
            section = "overview"
            in_notes = False
            continue
        if re.match(r"^#{1,3}\s+title\b", stripped, re.IGNORECASE):
            section = "title"
            in_notes = False
            rest = re.sub(r"^#{1,3}\s+title\b\s*[:\-]?\s*", "", stripped, flags=re.IGNORECASE)
            if rest:
                _set_title(sheet, rest)
            continue

        notes_match = _NOTES_HEAD.match(stripped)
        if notes_match:
            in_notes = True
            leftover = notes_match.group(1).strip()
            if leftover:
                notes_lines.append(leftover)
            continue

        if section == "close":
            continue

        card_title_match = _CARD_TITLE_LINE.match(stripped) or _TITLE_INDEX_LINE.match(stripped)
        if card_title_match and section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
            _set_card_title(
                sheet,
                point_index,
                int(card_title_match.group(1)),
                card_title_match.group(2),
            )
            continue

        image_title_match = _IMAGE_TITLE_LINE.match(stripped)
        if image_title_match and section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
            _set_image_title(sheet, point_index, image_title_match.group(1))
            continue

        image_match = _IMAGE_TEXT_LINE.match(stripped)
        if image_match and section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
            _set_image_text(sheet, point_index, image_match.group(1))
            continue

        title_match = _TITLE_LINE.match(stripped)
        if title_match:
            if section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
                _set_image_text(sheet, point_index, title_match.group(1))
            else:
                _set_title(sheet, title_match.group(1))
            continue

        platform_match = _PLATFORM_LINE.match(stripped)
        if platform_match and section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
            _set_platform(sheet, point_index, platform_match.group(1))
            continue

        card_match = _CARD_LINE.match(stripped)
        if card_match and section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
            _set_card(sheet, point_index, int(card_match.group(1)), card_match.group(2))
            continue

        bullet = _BULLET.match(stripped)
        payload = bullet.group(1).strip() if bullet else stripped
        if payload.startswith("#"):
            payload = payload.lstrip("#").strip()

        if in_notes or section == "overview" and notes_lines and in_notes:
            notes_lines.append(payload)
            continue
        if section == "overview":
            if len(overview_lines) < 2:
                overview_lines.append(payload)
            else:
                notes_lines.append(payload)
            continue
        if section == "title":
            if not sheet.title.strip():
                _set_title(sheet, payload)
            elif not overview_lines:
                section = "overview"
                overview_lines.append(payload)
            continue
        if section == "point" and 1 <= point_index <= TALK_POINT_COUNT:
            _append_card(sheet, point_index, payload)

    if overview_lines:
        sheet.set_headline_lines(
            overview_lines[0] if overview_lines else "",
            overview_lines[1] if len(overview_lines) > 1 else "",
            source="user",
        )
    notes = "\n".join(notes_lines).strip()
    if notes:
        sheet.exec_notes = notes
    _lock_close(sheet)
    return sheet


def apply_env_talk_aliases(sheet: TalkSheet) -> TalkSheet:
    """TALK_TITLE / TALK_EXEC_HEADLINE fill empty open fields only."""
    title = os.getenv("TALK_TITLE", "").strip()
    thesis = os.getenv("TALK_EXEC_HEADLINE", "").strip()
    if title and not sheet.title_locked() and not sheet.title.strip():
        sheet.title = title
        sheet.title_source = "auto"
        sheet.open_card.kicker = title
    if thesis and not sheet.headline_locked() and not sheet.exec_headline.strip():
        sheet.exec_headline = thesis
        sheet.exec_headline_source = "auto"
        sheet.open_card.headline = thesis
    return sheet


def merge_talk_sheet(base: TalkSheet, incoming: TalkSheet) -> TalkSheet:
    """Incoming locked/user fields win. Empty incoming leaves base."""
    out = base.model_copy(deep=True)
    if incoming.title_locked() or (incoming.title.strip() and not out.title_locked()):
        if incoming.title.strip():
            out.title = incoming.title.strip()
            out.title_source = incoming.title_source if incoming.title.strip() else out.title_source
            out.open_card.kicker = out.title
    if incoming.headline_locked() or (incoming.exec_headline.strip() and not out.headline_locked()):
        if incoming.exec_headline.strip():
            out.exec_headline = incoming.exec_headline
            out.exec_headline_source = incoming.exec_headline_source
            out.open_card.headline = incoming.exec_headline
    if incoming.exec_notes.strip():
        out.exec_notes = incoming.exec_notes
    for index, src in enumerate(incoming.points):
        dest = out.points[index]
        if src.platform_locked() or (src.platform.strip() and not dest.platform_locked()):
            if src.platform.strip():
                dest.platform = src.platform.strip()
                dest.platform_source = src.platform_source
        if src.still_locked() or (src.still_path.strip() and not dest.still_locked()):
            if src.still_path.strip():
                dest.still_path = src.still_path.strip()
                dest.still_source = src.still_source
        if src.image_title_locked() or (src.image_title.strip() and not dest.image_title_locked()):
            if src.image_title.strip():
                dest.image_title = src.image_title.strip()
                dest.image_title_source = src.image_title_source
        if src.image_text_locked() or (src.image_text.strip() and not dest.image_text_locked()):
            if src.image_text.strip():
                dest.image_text = src.image_text.strip()
                dest.image_text_source = src.image_text_source
        for card_i in range(TALK_CARDS_PER_POINT):
            if src.card_locked(card_i) or (src.cards[card_i].strip() and not dest.card_locked(card_i)):
                if src.cards[card_i].strip():
                    dest.cards[card_i] = src.cards[card_i].strip()
                    dest.card_sources[card_i] = src.card_sources[card_i]
            if src.title_locked(card_i) or (src.titles[card_i].strip() and not dest.title_locked(card_i)):
                if src.titles[card_i].strip():
                    dest.titles[card_i] = src.titles[card_i].strip()
                    dest.title_sources[card_i] = src.title_sources[card_i]
    _lock_close(out)
    return out


def attach_talk_sheet(script: EditScript, sheet: TalkSheet) -> EditScript:
    """Put user-locked open copy on the script. Close stays the locked CTA."""
    current = script.talk_sheet.model_copy(deep=True)
    script.talk_sheet = merge_talk_sheet(current, sheet)
    _lock_close(script.talk_sheet)
    return script


def apply_user_point_locks(script: EditScript, sheet: TalkSheet) -> EditScript:
    """Stamp user cards, titles, image text, and user stills onto body beats."""
    windows = point_windows(script)
    allowed = sheet_source_text(sheet, script.transcript)
    for index, (point, (w0, w1)) in enumerate(zip(sheet.points, windows)):
        if w1 <= w0:
            continue
        user_cards = [
            card_i
            for card_i in range(TALK_CARDS_PER_POINT)
            if point.card_locked(card_i) or point.title_locked(card_i)
        ]
        need_pip = point.still_locked()
        if need_pip:
            pip_scene = _ensure_pip_slot(script, w0, w1, PIP_HOLD_SECONDS)
            pip_scene.layout = PictureTag.PIP
            pip_scene.graphic.asset_path = point.still_path.strip()
            pip_scene.asset_ref = point.still_path.strip()
            if point.platform.strip():
                pip_scene.graphic.still_query = point.platform.strip()
            _stamp_image_text(pip_scene, point, allowed)
        elif point.platform.strip():
            _stamp_platform_query(script, w0, w1, point.platform.strip())

        if not user_cards:
            continue
        overlays = [
            scene
            for scene in _scenes_in_window(script, w0, w1)
            if scene.layout is PictureTag.OVERLAY
        ]
        for card_i, scene in zip(user_cards, overlays):
            _stamp_overlay(scene, point, index, card_i, allowed)
        missing = user_cards[len(overlays) :]
        if missing:
            extras = _ensure_overlay_slots(script, w0, w1, len(missing), skip_pip=need_pip)
            for card_i, scene in zip(missing, extras):
                _stamp_overlay(scene, point, index, card_i, allowed)
    sanitize_script_kickers(script, sheet)
    enforce_pip_holds(script, PIP_HOLD_SECONDS)
    return script


def autofill_talk_sheet(
    sheet: TalkSheet,
    script: EditScript,
    stills_dir: Path | None = None,
) -> TalkSheet:
    """Fill empty slots from the director / local still matcher. Skip user locks."""
    windows = point_windows(script)
    folder = Path(stills_dir) if stills_dir is not None else None
    if not sheet.title_locked() and not sheet.title.strip():
        kicker = script.talk_sheet.open_card.kicker.strip()
        if kicker:
            sheet.title = kicker
            sheet.title_source = "auto"
            sheet.open_card.kicker = kicker
    if not sheet.headline_locked() and not sheet.exec_headline.strip():
        headline = script.talk_sheet.open_card.headline.strip()
        if headline:
            sheet.exec_headline = headline
            sheet.exec_headline_source = "auto"
            sheet.open_card.headline = headline

    allowed = sheet_source_text(sheet, script.transcript)
    for point, (w0, w1) in zip(sheet.points, windows):
        overlays = [
            scene
            for scene in _scenes_in_window(script, w0, w1)
            if scene.layout is PictureTag.OVERLAY and scene.graphic.title.strip()
        ]
        for card_i in range(TALK_CARDS_PER_POINT):
            scene = overlays[card_i] if card_i < len(overlays) else None
            if not point.card_locked(card_i) and scene is not None:
                point.cards[card_i] = scene.graphic.title.strip()
                point.card_sources[card_i] = "auto"
            if not point.title_locked(card_i):
                headline = point.cards[card_i] or (scene.graphic.title if scene else "")
                said = scene.said if scene is not None else ""
                candidate = point.titles[card_i].strip()
                if candidate and _is_point_image_text(candidate, point):
                    candidate = ""
                if headline.strip() or candidate.strip():
                    point.titles[card_i] = resolve_auto_kicker(
                        candidate,
                        headline=headline,
                        said=said,
                        platform=point.platform,
                        allowed=allowed,
                    )
                    point.title_sources[card_i] = "auto"
        pips = [
            scene
            for scene in _scenes_in_window(script, w0, w1)
            if scene.layout is PictureTag.PIP
        ]
        if not point.still_locked():
            if point.platform.strip() and folder is not None:
                matched = match_local_still(point.platform, folder)
                if matched is not None:
                    point.still_path = str(matched.resolve())
                    point.still_source = "auto"
            if not point.platform_locked() and not point.platform.strip():
                query = _first_still_query(script, w0, w1)
                if query:
                    point.platform = query
                    point.platform_source = "auto"
        pip = pips[0] if pips else None
        has_still = bool(point.still_path.strip() or pips or point.platform.strip())
        if not point.image_title_locked() and has_still and not point.image_title.strip():
            candidate = _still_basename_label(point.still_path) or point.platform
            point.image_title = resolve_auto_kicker(
                candidate,
                headline=point.platform,
                said="",
                platform=point.platform,
                allowed=allowed,
            )
            point.image_title_source = "auto"
        if not point.image_text_locked() and has_still and not point.image_text.strip():
            point.image_text = resolve_auto_image_text(
                cards=point.cards,
                said=pip.said if pip is not None else "",
                platform=point.platform,
                pip_title=pip.graphic.title if pip is not None else "",
                allowed=allowed,
            )
            point.image_text_source = "auto"
    _lock_close(sheet)
    sanitize_script_kickers(script, sheet)
    enforce_pip_holds(script, PIP_HOLD_SECONDS)
    return sheet


def collect_form_text(
    typed: str,
    previous: str,
    previous_source: FieldSource,
) -> tuple[str, FieldSource]:
    """Keep auto/user source when the form text did not change."""
    text = (typed or "").strip()
    if not text:
        return "", "empty"
    if text == (previous or "").strip() and previous_source in {"user", "auto"}:
        return text, previous_source
    return text, "user"


def point_windows(script: EditScript) -> list[tuple[float, float]]:
    body = [scene for scene in script.scenes if scene.role == "body" and scene.end > scene.start]
    if not body:
        return [(0.0, 0.0)] * TALK_POINT_COUNT
    start = min(scene.start for scene in body)
    end = max(scene.end for scene in body)
    span = max(0.0, end - start)
    if span <= 0:
        return [(start, end)] * TALK_POINT_COUNT
    third = span / float(TALK_POINT_COUNT)
    return [(start + i * third, start + (i + 1) * third) for i in range(TALK_POINT_COUNT)]


def _reset_user_import_slots(sheet: TalkSheet) -> None:
    """Clear unlocked body/open fields so a paste replaces prior import, not close."""
    if not sheet.title_locked():
        sheet.title = ""
        sheet.title_source = "empty"
        sheet.open_card.kicker = ""
    if not sheet.headline_locked():
        sheet.exec_headline = ""
        sheet.exec_headline_source = "empty"
        sheet.open_card.headline = ""
    sheet.exec_notes = ""
    for point in sheet.points:
        if not point.platform_locked():
            point.platform = ""
            point.platform_source = "empty"
        if not point.still_locked():
            point.still_path = ""
            point.still_source = "empty"
        if not point.image_title_locked():
            point.image_title = ""
            point.image_title_source = "empty"
        if not point.image_text_locked():
            point.image_text = ""
            point.image_text_source = "empty"
        for card_i in range(TALK_CARDS_PER_POINT):
            if not point.card_locked(card_i):
                point.cards[card_i] = ""
                point.card_sources[card_i] = "empty"
            if not point.title_locked(card_i):
                point.titles[card_i] = ""
                point.title_sources[card_i] = "empty"


def _set_title(sheet: TalkSheet, value: str) -> None:
    text = value.strip()
    if not text or sheet.title_locked():
        return
    sheet.title = text
    sheet.title_source = "user"
    sheet.open_card.kicker = text


def _set_platform(sheet: TalkSheet, point_index: int, value: str) -> None:
    point = sheet.points[point_index - 1]
    text = value.strip()
    if not text or point.platform_locked():
        return
    point.platform = text
    point.platform_source = "user"


def _set_card(sheet: TalkSheet, point_index: int, card_index: int, value: str) -> None:
    if card_index < 1 or card_index > TALK_CARDS_PER_POINT:
        return
    point = sheet.points[point_index - 1]
    text = value.strip()
    if not text or point.card_locked(card_index - 1):
        return
    point.cards[card_index - 1] = text
    point.card_sources[card_index - 1] = "user"


def _set_card_title(sheet: TalkSheet, point_index: int, card_index: int, value: str) -> None:
    if card_index < 1 or card_index > TALK_CARDS_PER_POINT:
        return
    point = sheet.points[point_index - 1]
    text = value.strip()
    if not text or point.title_locked(card_index - 1):
        return
    point.titles[card_index - 1] = text
    point.title_sources[card_index - 1] = "user"


def _set_image_title(sheet: TalkSheet, point_index: int, value: str) -> None:
    point = sheet.points[point_index - 1]
    text = value.strip()
    if not text or point.image_title_locked():
        return
    point.image_title = text
    point.image_title_source = "user"


def _set_image_text(sheet: TalkSheet, point_index: int, value: str) -> None:
    point = sheet.points[point_index - 1]
    text = value.strip()
    if not text or point.image_text_locked():
        return
    point.image_text = text
    point.image_text_source = "user"


def _append_card(sheet: TalkSheet, point_index: int, value: str) -> None:
    point = sheet.points[point_index - 1]
    text = value.strip()
    if not text:
        return
    for card_i in range(TALK_CARDS_PER_POINT):
        if not point.cards[card_i].strip() and not point.card_locked(card_i):
            point.cards[card_i] = text
            point.card_sources[card_i] = "user"
            return


def _lock_close(sheet: TalkSheet) -> None:
    sheet.close_card.kicker = "WORK WITH ME"
    sheet.close_card.headline = "Independent AI T&E.\nVendor-agnostic."
    sheet.close_card.icon = "share"
    sheet.close_kicker = sheet.close_card.kicker
    sheet.close_headline = sheet.close_card.headline
    sheet.close_icon = "share"


def _scenes_in_window(script: EditScript, start: float, end: float) -> list[Scene]:
    found: list[Scene] = []
    for scene in script.scenes:
        if scene.role != "body":
            continue
        if scene.end <= start or scene.start >= end:
            continue
        found.append(scene)
    return found


def _pick_pip_scene(script: EditScript, start: float, end: float) -> Scene:
    return _ensure_pip_slot(script, start, end, PIP_HOLD_SECONDS)


def _ensure_pip_slot(script: EditScript, start: float, end: float, min_hold: float) -> Scene:
    """A PiP beat inside the window. Split from nothing. Do not consume overlays."""
    current = _scenes_in_window(script, start, end)
    for scene in current:
        if scene.layout is PictureTag.PIP:
            _grow_pip_into_nothing(script, scene, min_hold, window=(start, end))
            _cap_pip_at_hold(script, scene, min_hold)
            return scene
    nothings = [scene for scene in current if scene.layout is PictureTag.NOTHING]
    host = nothings[0] if nothings else None
    if host is None:
        inserted = _insert_nothing(script, start, min(end, start + max(min_hold, 0.4)))
        inserted.layout = PictureTag.PIP
        return inserted
    pip_start = max(host.start, start)
    available = max(0.0, min(host.end, end) - pip_start)
    pip_end = pip_start + min(max(min_hold, 0.4), available if available > 0 else max(min_hold, 0.4))
    pip_end = min(pip_end, host.end)
    if pip_end - pip_start < 0.05:
        if host.end - host.start <= min_hold + 0.05:
            host.layout = PictureTag.PIP
            return host
        pip_start = max(host.start, start)
        pip_end = min(host.end, pip_start + max(min_hold, 0.4))
        if pip_end - pip_start < 0.05:
            host.layout = PictureTag.PIP
            return host
    parts: list[Scene] = []
    if pip_start - host.start >= 0.05:
        left = host.model_copy(deep=True)
        left.end = pip_start
        parts.append(left)
    pip = host.model_copy(deep=True)
    pip.start = pip_start
    pip.end = pip_end
    pip.layout = PictureTag.PIP
    parts.append(pip)
    if host.end - pip_end >= 0.05:
        right = host.model_copy(deep=True)
        right.start = pip_end
        parts.append(right)
    idx = script.scenes.index(host)
    script.scenes[idx : idx + 1] = parts
    script.scenes.sort(key=lambda item: (item.start, item.end))
    return pip


def _grow_pip_into_nothing(
    script: EditScript,
    pip: Scene,
    min_hold: float,
    *,
    window: tuple[float, float] | None = None,
) -> None:
    """Lengthen a short PiP using adjacent nothing only, never past the hold cap."""
    needed = min_hold - (pip.end - pip.start)
    if needed <= 1e-6:
        return
    w0, w1 = window if window is not None else (0.0, 1e9)
    max_end = pip.start + min_hold
    min_start = pip.end - min_hold
    scenes = script.scenes
    try:
        index = scenes.index(pip)
    except ValueError:
        return
    if index + 1 < len(scenes):
        nxt = scenes[index + 1]
        if (
            nxt.role == "body"
            and nxt.layout is PictureTag.NOTHING
            and abs(nxt.start - pip.end) < 0.06
        ):
            take = min(needed, max(0.0, min(nxt.end, w1, max_end) - max(nxt.start, pip.end)))
            if take > 0.04:
                pip.end += take
                nxt.start += take
                needed -= take
                if nxt.end - nxt.start < 0.05:
                    scenes.pop(index + 1)
    if needed <= 1e-6:
        return
    if index > 0:
        prev = scenes[index - 1]
        if (
            prev.role == "body"
            and prev.layout is PictureTag.NOTHING
            and abs(prev.end - pip.start) < 0.06
        ):
            take = min(needed, max(0.0, min(pip.start, prev.end) - max(prev.start, w0, min_start)))
            if take > 0.04:
                pip.start -= take
                prev.end -= take
                if prev.end - prev.start < 0.05:
                    scenes.pop(index - 1)


def _cap_pip_at_hold(script: EditScript, pip: Scene, hold: float) -> None:
    """Cut a long still back to the hold. Remainder becomes full-frame host."""
    if pip.end - pip.start <= hold + 1e-6:
        return
    cut = pip.start + hold
    try:
        index = script.scenes.index(pip)
    except ValueError:
        return
    if index + 1 < len(script.scenes):
        nxt = script.scenes[index + 1]
        if (
            nxt.role == "body"
            and nxt.layout is PictureTag.NOTHING
            and abs(nxt.start - pip.end) < 0.06
        ):
            pip.end = cut
            nxt.start = cut
            return
    tail = pip.model_copy(deep=True)
    pip.end = cut
    tail.start = cut
    tail.layout = PictureTag.NOTHING
    tail.graphic = GraphicCard()
    tail.asset_ref = None
    tail.asset_kind = "none"
    script.scenes.insert(index + 1, tail)


def enforce_pip_holds(script: EditScript, min_hold: float = PIP_HOLD_SECONDS) -> EditScript:
    """Still hold is readable twice, then a hard cut back to full-frame host."""
    hold = max(4.0, float(min_hold or PIP_HOLD_SECONDS))
    for scene in list(script.scenes):
        if scene.role != "body" or scene.layout is not PictureTag.PIP:
            continue
        duration = scene.end - scene.start
        if duration > hold + 1e-6:
            _cap_pip_at_hold(script, scene, hold)
            continue
        if duration + 1e-6 < hold:
            _grow_pip_into_nothing(script, scene, hold)
            if scene.end - scene.start > hold + 1e-6:
                _cap_pip_at_hold(script, scene, hold)
    script.scenes.sort(key=lambda item: (item.start, item.end))
    return script


def sanitize_script_kickers(script: EditScript, sheet: TalkSheet) -> EditScript:
    """User titles stay exact. Auto overlay/PiP kickers cannot invent DoD or statutes."""
    allowed = sheet_source_text(sheet, script.transcript)
    windows = point_windows(script)
    locked_ids: set[int] = set()
    for point, (w0, w1) in zip(sheet.points, windows):
        overlays = [
            scene
            for scene in _scenes_in_window(script, w0, w1)
            if scene.layout is PictureTag.OVERLAY
        ]
        for card_i, scene in zip(range(TALK_CARDS_PER_POINT), overlays):
            scene.graphic.kicker = _overlay_kicker_for_card(point, card_i, scene, allowed)
            if point.title_locked(card_i) and not _is_point_image_text(point.titles[card_i], point):
                scene.graphic.kicker = point.titles[card_i]
            elif point.titles[card_i].strip() and point.title_sources[card_i] == "auto":
                if not _is_point_image_text(scene.graphic.kicker, point):
                    point.titles[card_i] = scene.graphic.kicker
            locked_ids.add(id(scene))
        for scene in _scenes_in_window(script, w0, w1):
            if scene.layout is not PictureTag.PIP:
                continue
            _stamp_image_text(scene, point, allowed)
            locked_ids.add(id(scene))
    for scene in script.scenes:
        if scene.role != "body" or id(scene) in locked_ids:
            continue
        if scene.layout is PictureTag.OVERLAY:
            if any(_is_point_image_text(scene.graphic.kicker, point) for point in sheet.points):
                _strip_image_text_from_overlay(scene, sheet)
            else:
                scene.graphic.kicker = resolve_auto_kicker(
                    scene.graphic.kicker,
                    headline=scene.graphic.title,
                    said=scene.said,
                    allowed=allowed,
                )
        elif scene.layout is PictureTag.PIP:
            if any(_is_point_image_text(scene.graphic.kicker, point) for point in sheet.points):
                scene.graphic.kicker = ""
            elif scene.graphic.kicker.strip():
                scene.graphic.kicker = resolve_auto_kicker(
                    scene.graphic.kicker,
                    headline=scene.graphic.still_query,
                    said=scene.said,
                    platform=scene.graphic.still_query,
                    allowed=allowed,
                )
    for scene in script.scenes:
        if scene.role == "body" and scene.layout is PictureTag.OVERLAY:
            _strip_image_text_from_overlay(scene, sheet)
    return script


def _stamp_platform_query(script: EditScript, start: float, end: float, platform: str) -> None:
    current = _scenes_in_window(script, start, end)
    for scene in current:
        if scene.layout is PictureTag.PIP:
            if not scene.graphic.still_query.strip():
                scene.graphic.still_query = platform
            return
    target = next((scene for scene in current if scene.layout is PictureTag.NOTHING), None)
    if target is None and current:
        target = current[0]
    if target is None:
        target = _insert_nothing(script, start, end)
    if target.layout is PictureTag.NOTHING:
        target.layout = PictureTag.PIP
    if not target.graphic.still_query.strip():
        target.graphic.still_query = platform


def _ensure_overlay_slots(
    script: EditScript,
    start: float,
    end: float,
    needed: int,
    *,
    skip_pip: bool,
) -> list[Scene]:
    current = [
        scene
        for scene in _scenes_in_window(script, start, end)
        if scene.layout is not PictureTag.PIP or not skip_pip
    ]
    if skip_pip:
        current = [scene for scene in current if scene.layout is not PictureTag.PIP]
    available = [scene for scene in current if scene.layout is PictureTag.NOTHING]
    if len(available) >= needed:
        return available[:needed]
    if not current:
        scene = _insert_nothing(script, start, end)
        current = [scene]
        available = [scene]
    split_from = available or [
        scene for scene in current if scene.layout is not PictureTag.OVERLAY
    ] or current
    longest = max(split_from, key=lambda scene: min(scene.end, end) - max(scene.start, start))
    longest = _clip_scene_to_window(script, longest, start, end)
    available = [
        scene
        for scene in _scenes_in_window(script, start, end)
        if scene.layout is PictureTag.NOTHING and (not skip_pip or scene.layout is not PictureTag.PIP)
    ]
    if len(available) >= needed:
        return available[:needed]
    parts = _split_scene(longest, max(needed, 2))
    idx = script.scenes.index(longest)
    script.scenes[idx : idx + 1] = parts
    script.scenes.sort(key=lambda scene: (scene.start, scene.end))
    refreshed = [
        scene
        for scene in _scenes_in_window(script, start, end)
        if scene.layout is PictureTag.NOTHING or scene.layout is PictureTag.OVERLAY
    ]
    if skip_pip:
        refreshed = [scene for scene in refreshed if scene.layout is not PictureTag.PIP]
    return [scene for scene in refreshed if scene.layout is not PictureTag.OVERLAY][:needed]


def _clip_scene_to_window(script: EditScript, scene: Scene, start: float, end: float) -> Scene:
    """Keep overlay/PiP slot surgery inside the point window. Leave the rest as nothing."""
    mid_start = max(scene.start, start)
    mid_end = min(scene.end, end)
    if mid_end - mid_start < 0.05:
        return scene
    if abs(scene.start - mid_start) < 0.02 and abs(scene.end - mid_end) < 0.02:
        return scene
    parts: list[Scene] = []
    if mid_start - scene.start >= 0.05:
        left = scene.model_copy(deep=True)
        left.end = mid_start
        parts.append(left)
    mid = scene.model_copy(deep=True)
    mid.start = mid_start
    mid.end = mid_end
    parts.append(mid)
    if scene.end - mid_end >= 0.05:
        right = scene.model_copy(deep=True)
        right.start = mid_end
        parts.append(right)
    idx = script.scenes.index(scene)
    script.scenes[idx : idx + 1] = parts
    script.scenes.sort(key=lambda item: (item.start, item.end))
    return mid


def _insert_nothing(script: EditScript, start: float, end: float) -> Scene:
    scene = Scene(
        start=start,
        end=max(end, start + 0.4),
        layout=PictureTag.NOTHING,
        role="body",
        reason="talk-sheet-slot",
    )
    script.scenes.append(scene)
    script.scenes.sort(key=lambda item: (item.start, item.end))
    return scene


def _split_scene(scene: Scene, pieces: int) -> list[Scene]:
    pieces = max(1, pieces)
    if pieces == 1:
        return [scene]
    duration = max(0.4, scene.end - scene.start)
    step = duration / pieces
    out: list[Scene] = []
    for index in range(pieces):
        child = scene.model_copy(deep=True)
        child.start = scene.start + index * step
        child.end = scene.start + (index + 1) * step
        child.layout = PictureTag.NOTHING
        child.graphic = GraphicCard()
        child.asset_ref = None
        out.append(child)
    return out


def _stamp_overlay(
    scene: Scene,
    point: TalkPoint,
    point_index: int,
    card_i: int,
    allowed: str,
) -> None:
    scene.layout = PictureTag.OVERLAY
    headline = point.cards[card_i].strip()
    if headline:
        scene.graphic.title = headline
    scene.graphic.kicker = _overlay_kicker_for_card(point, card_i, scene, allowed)
    if not scene.graphic.kicker.strip():
        scene.graphic.kicker = derive_kicker(
            headline or scene.graphic.title,
            said=scene.said,
            platform=point.platform or f"POINT {point_index + 1}",
        )
    if not scene.graphic.icon.strip():
        scene.graphic.icon = "bar_chart"


def _stamp_image_text(scene: Scene, point: TalkPoint, allowed: str) -> None:
    """Still plate: image_title is gold, image_text is white. Never split image_text."""
    del allowed
    kicker, headline = still_plate_copy(point)
    scene.graphic.kicker = kicker
    if headline:
        scene.graphic.title = headline


def _first_still_query(script: EditScript, start: float, end: float) -> str:
    for scene in _scenes_in_window(script, start, end):
        query = scene.graphic.still_query.strip()
        if query:
            return query
    return ""


def _safe_stem(stem: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)
    return cleaned.strip("_")[:40]
