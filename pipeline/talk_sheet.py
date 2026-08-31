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
_PLATFORM_LINE = re.compile(
    r"^(?:platform|still(?:\s+query)?|query|hardware)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_CARD_LINE = re.compile(r"^(?:card\s*)(\d)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*]|\d+[.)])\s+(.+)$")
_NOTES_HEAD = re.compile(r"^(?:spoken[- ]?exec(?:utive)?[- ]?notes|spoken notes|notes)\s*[:\-]\s*(.*)$", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9]+")

KNOWN_MARKDOWN_SHAPE = """\
# SKYNET IS COMING · PART 2

## Overview
$1.5B is the floor.
Not the program.

Spoken notes:
Title plus the two-line thesis. Not painted.

## Point 1
Platform: MQ-9 Reaper
- $1.5B in procurements. That's the floor.
- I think that's even low.
- Programs are stacking, not replacing.

## Point 2
Platform: M1 Abrams
- The vehicle is the named still.
- Overlay copy stays a spoken sentence.

## Point 3
Platform: Patriot
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

        title_match = _TITLE_LINE.match(stripped)
        if title_match:
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
        for card_i in range(TALK_CARDS_PER_POINT):
            if src.card_locked(card_i) or (src.cards[card_i].strip() and not dest.card_locked(card_i)):
                if src.cards[card_i].strip():
                    dest.cards[card_i] = src.cards[card_i].strip()
                    dest.card_sources[card_i] = src.card_sources[card_i]
    _lock_close(out)
    return out


def attach_talk_sheet(script: EditScript, sheet: TalkSheet) -> EditScript:
    """Put user-locked open copy on the script. Close stays the locked CTA."""
    current = script.talk_sheet.model_copy(deep=True)
    script.talk_sheet = merge_talk_sheet(current, sheet)
    _lock_close(script.talk_sheet)
    return script


def apply_user_point_locks(script: EditScript, sheet: TalkSheet) -> EditScript:
    """Stamp user cards and user stills onto body beats. Do not invent a still."""
    windows = point_windows(script)
    for index, (point, (w0, w1)) in enumerate(zip(sheet.points, windows)):
        if w1 <= w0:
            continue
        user_cards = [
            (card_i, point.cards[card_i].strip())
            for card_i in range(TALK_CARDS_PER_POINT)
            if point.card_locked(card_i)
        ]
        need_pip = point.still_locked()
        if need_pip:
            pip_scene = _pick_pip_scene(script, w0, w1)
            pip_scene.layout = PictureTag.PIP
            pip_scene.graphic.asset_path = point.still_path.strip()
            pip_scene.asset_ref = point.still_path.strip()
            if point.platform.strip():
                pip_scene.graphic.still_query = point.platform.strip()
            if not pip_scene.graphic.kicker.strip():
                pip_scene.graphic.kicker = point.platform.strip() or f"{index + 1}"
        elif point.platform.strip():
            _stamp_platform_query(script, w0, w1, point.platform.strip())

        if not user_cards:
            continue
        overlays = [
            scene
            for scene in _scenes_in_window(script, w0, w1)
            if scene.layout is PictureTag.OVERLAY
        ]
        for (card_i, text), scene in zip(user_cards, overlays):
            _stamp_overlay(scene, text, point, index)
        missing = user_cards[len(overlays) :]
        if missing:
            extras = _ensure_overlay_slots(script, w0, w1, len(missing), skip_pip=need_pip)
            for (card_i, text), scene in zip(missing, extras):
                _stamp_overlay(scene, text, point, index)
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

    for point, (w0, w1) in zip(sheet.points, windows):
        overlays = [
            scene
            for scene in _scenes_in_window(script, w0, w1)
            if scene.layout is PictureTag.OVERLAY and scene.graphic.title.strip()
        ]
        for card_i in range(TALK_CARDS_PER_POINT):
            if point.card_locked(card_i):
                continue
            if card_i < len(overlays):
                point.cards[card_i] = overlays[card_i].graphic.title.strip()
                point.card_sources[card_i] = "auto"
        if point.still_locked():
            continue
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
    _lock_close(sheet)
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
        for card_i in range(TALK_CARDS_PER_POINT):
            if not point.card_locked(card_i):
                point.cards[card_i] = ""
                point.card_sources[card_i] = "empty"


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
    current = _scenes_in_window(script, start, end)
    for scene in current:
        if scene.layout is PictureTag.PIP:
            return scene
    for scene in current:
        if scene.layout is PictureTag.NOTHING:
            return scene
    if current:
        return current[0]
    return _insert_nothing(script, start, end)


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
    longest = max(split_from, key=lambda scene: scene.end - scene.start)
    extra = needed - len(available) + 1
    parts = _split_scene(longest, max(extra, 2))
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


def _stamp_overlay(scene: Scene, text: str, point: TalkPoint, point_index: int) -> None:
    scene.layout = PictureTag.OVERLAY
    scene.graphic.title = text
    if not scene.graphic.kicker.strip():
        scene.graphic.kicker = (point.platform.strip() or f"POINT {point_index + 1}").upper()
    if not scene.graphic.icon.strip():
        scene.graphic.icon = "bar_chart"


def _first_still_query(script: EditScript, start: float, end: float) -> str:
    for scene in _scenes_in_window(script, start, end):
        query = scene.graphic.still_query.strip()
        if query:
            return query
    return ""


def _safe_stem(stem: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)
    return cleaned.strip("_")[:40]
