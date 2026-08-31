"""Lock Point 1–3 picture windows to the trimmed-cut transcript.

Equal-thirds body splits miss Point 2 when the talk is uneven (Scott's 2:35
case). Windows come from a user "Starts when I say" cue, or the first solid
hit of that point's card title / body / platform. A filled cue that is not
in the transcript skips that point's picture instead of borrowing Point 1.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pipeline.layouts import PictureTag
from pipeline.models import (
    TALK_CARDS_PER_POINT,
    TALK_POINT_COUNT,
    EditScript,
    GraphicCard,
    TalkPoint,
    TalkSheet,
    TimedTranscript,
    TranscriptCue,
)
logger = logging.getLogger(__name__)

_DOD_PHRASE = re.compile(r"\bdepartment of defense\b", re.IGNORECASE)
_DOD_ABBR = re.compile(r"\bdod\b", re.IGNORECASE)

# Minimum ratio for a fuzzy window of the same token length as the needle.
FUZZY_RATIO = 0.72
# Distinctive single-token hits (platform names) must be at least this long.
SOLID_TOKEN_LEN = 5

_FILLER = frozenset({"uh", "um", "uhh", "umm", "ah", "er"})
_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("department of defense", "department of war"),
    ("dept of defense", "department of war"),
    ("department of war", "department of war"),
    ("d o d", "dow"),
    ("dod", "dow"),
    ("dow", "dow"),
    ("mq 9", "mq9"),
    ("m q 9", "mq9"),
    ("mq-9", "mq9"),
)
_TOKEN = re.compile(r"[a-z0-9$]+")
_NON_ALNUM = re.compile(r"[^a-z0-9$]+")


@dataclass
class PointAlignment:
    """Resolved Point 1–3 windows on the trimmed timeline."""

    windows: list[tuple[float, float]]
    skipped: list[int] = field(default_factory=list)
    skip_ranges: list[tuple[float, float]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def timed_transcript_from_script(script: EditScript) -> TimedTranscript:
    """Prefer persisted cues. Fall back to scene.said so older scripts still align."""
    cues = [cue.model_copy(deep=True) for cue in script.transcript_cues if cue.text.strip()]
    if not cues:
        for scene in script.scenes:
            said = (scene.said or "").strip()
            if not said or scene.end <= scene.start:
                continue
            cues.append(TranscriptCue(start=scene.start, end=scene.end, text=said))
    duration = 0.0
    if script.scenes:
        duration = max(scene.end for scene in script.scenes)
    return TimedTranscript(duration=duration, full_text=script.transcript, cues=cues)


def _house_style(text: str) -> str:
    """DoD / Department of Defense → DOW / Department of War for matching."""
    if not text:
        return ""
    out = _DOD_PHRASE.sub("Department of War", text)

    def _abbr(match: re.Match[str]) -> str:
        raw = match.group(0)
        return "DOW" if raw.isupper() or raw[:1].isupper() else "dow"

    return _DOD_ABBR.sub(_abbr, out)


def normalize_match_text(text: str) -> str:
    """Lowercase, house-style, glossary, drop filler. Used for fuzzy hits."""
    blob = _house_style(text or "")
    blob = blob.casefold()
    blob = blob.replace("mq-9", "mq9").replace("m.q.9", "mq9")
    blob = _NON_ALNUM.sub(" ", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    for src, dest in _GLOSSARY:
        blob = re.sub(rf"\b{re.escape(src)}\b", dest, blob)
    tokens = [token for token in _TOKEN.findall(blob) if token not in _FILLER]
    return " ".join(tokens)


def _tokens(text: str) -> list[str]:
    return normalize_match_text(text).split()


def _is_solid_needle(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if len(tokens) >= 2:
        return True
    return len(tokens[0]) >= SOLID_TOKEN_LEN


def find_transcript_hit(
    transcript: TimedTranscript,
    phrase: str,
    *,
    after: float = 0.0,
    before: float | None = None,
) -> float | None:
    """First fuzzy/glossary-tolerant hit of phrase on the trimmed transcript.

    Returns the cue start of the matching window, or None.
    """
    needle = _tokens(phrase)
    if not _is_solid_needle(needle):
        return None
    limit = before if before is not None else 1e9
    words = _word_timeline(transcript, after=after, before=limit)
    if not words:
        return None
    n = len(needle)
    needle_text = " ".join(needle)
    best_time: float | None = None
    best_score = 0.0

    for index in range(len(words)):
        start_t = words[index][1]
        if start_t + 1e-6 < after or start_t >= limit:
            continue
        # Exact-length window, then a slightly longer window for extra words.
        for width in (n, n + 1, n + 2, max(n, 3)):
            window = words[index : index + width]
            if len(window) < min(n, 2) and n >= 2:
                continue
            if not window:
                continue
            hay = " ".join(item[0] for item in window)
            score = _phrase_score(needle, needle_text, hay)
            hit_time = window[0][3]
            if hit_time + 1e-6 < after:
                hit_time = window[0][1]
            if score > best_score:
                best_score = score
                best_time = hit_time
            if score >= 0.999:
                return hit_time

    if best_time is not None and best_score >= FUZZY_RATIO:
        return best_time
    return None


def first_card_line_hit(
    transcript: TimedTranscript,
    point: TalkPoint,
    *,
    after: float = 0.0,
    before: float | None = None,
) -> float | None:
    """Earliest solid hit of this point's title, body, or platform."""
    hits: list[float] = []
    for phrase in _point_card_phrases(point):
        hit = find_transcript_hit(transcript, phrase, after=after, before=before)
        if hit is not None:
            hits.append(hit)
    return min(hits) if hits else None


def card_line_hit(
    transcript: TimedTranscript,
    point: TalkPoint,
    card_i: int,
    *,
    after: float = 0.0,
    before: float | None = None,
) -> float | None:
    """Later hit of Title[i][j] or body[i][j]. Title and body stay paired."""
    phrases: list[str] = []
    if 0 <= card_i < TALK_CARDS_PER_POINT:
        if point.titles[card_i].strip():
            phrases.append(point.titles[card_i])
        if point.cards[card_i].strip():
            phrases.append(point.cards[card_i])
    hits: list[float] = []
    for phrase in phrases:
        hit = find_transcript_hit(transcript, phrase, after=after, before=before)
        if hit is not None:
            hits.append(hit)
    return min(hits) if hits else None


def resolve_point_alignment(
    script: EditScript,
    sheet: TalkSheet | None = None,
    transcript: TimedTranscript | None = None,
) -> PointAlignment:
    """Transcript-locked Point 1–3 windows. Open bookend is not Point 1."""
    sheet = sheet or script.talk_sheet
    timed = transcript or timed_transcript_from_script(script)
    body_start, body_end = _body_span(script)
    open_end = _open_end(script, body_start)
    reasons = ["thirds"] * TALK_POINT_COUNT
    starts: list[float | None] = [None] * TALK_POINT_COUNT
    skipped: list[int] = []

    search_after = open_end
    for index, point in enumerate(sheet.points[:TALK_POINT_COUNT]):
        cue = (point.start_cue or "").strip()
        if cue:
            hit = find_transcript_hit(timed, cue, after=search_after)
            if hit is None:
                logger.warning(
                    "Point %s start cue not found in transcript; skipping picture: %r",
                    index + 1,
                    cue,
                )
                print(
                    f"Point {index + 1} start cue not found in transcript; "
                    "skipping picture (full-frame host).",
                    flush=True,
                )
                starts[index] = None
                reasons[index] = "missing-cue"
                skipped.append(index)
                continue
            starts[index] = hit
            reasons[index] = "cue"
            search_after = hit + 0.05
            continue
        hit = first_card_line_hit(timed, point, after=search_after)
        if hit is not None:
            starts[index] = hit
            reasons[index] = "card"
            search_after = hit + 0.05
            continue
        starts[index] = None
        reasons[index] = "thirds"

    if all(start is None and reason == "thirds" for start, reason in zip(starts, reasons)):
        return PointAlignment(
            windows=_equal_third_windows(body_start, body_end),
            reasons=reasons,
        )

    later_signals = _later_point_signals(sheet, timed, open_end)
    thirds = _equal_third_windows(body_start, body_end)
    windows: list[tuple[float, float]] = []
    skip_ranges: list[tuple[float, float]] = []
    for index, start in enumerate(starts):
        if start is None and index in skipped:
            zone = _skipped_zone(index, starts, later_signals, body_end, timed, sheet.points[index])
            if zone is not None:
                skip_ranges.append(zone)
            windows.append((0.0, 0.0))
            continue
        if start is None:
            # No cue, no card-line hit: keep a thirds slice so empty-form autofill works.
            windows.append(thirds[index])
            continue
        end = _point_end(index, start, starts, later_signals, body_end)
        for later in range(index + 1, TALK_POINT_COUNT):
            if starts[later] is None and reasons[later] == "thirds":
                third_start = thirds[later][0]
                if third_start > start + 0.05:
                    end = min(end, third_start)
                break
        windows.append((start, max(end, start)))

    return PointAlignment(
        windows=windows,
        skipped=skipped,
        skip_ranges=skip_ranges,
        reasons=reasons,
    )


def point_windows(
    script: EditScript,
    sheet: TalkSheet | None = None,
    transcript: TimedTranscript | None = None,
) -> list[tuple[float, float]]:
    """Public window list. Transcript-locked when cues or card lines exist."""
    return resolve_point_alignment(script, sheet, transcript).windows


def split_body_at_times(script: EditScript, times: list[float]) -> None:
    """Split body scenes at point boundaries so a long Point 1 pip cannot cover Point 2."""
    marks = sorted({time for time in times if time > 0})
    for mark in marks:
        _split_body_at(script, mark)


def clear_picture_range(script: EditScript, start: float, end: float) -> None:
    """Full-frame host in this range. Do not keep the wrong point's cards."""
    if end <= start:
        return
    for scene in list(script.scenes):
        if scene.role != "body":
            continue
        if scene.end <= start or scene.start >= end:
            continue
        if scene.start < start < scene.end:
            _split_body_at(script, start)
        if scene.start < end < scene.end:
            _split_body_at(script, end)
    for scene in script.scenes:
        if scene.role != "body":
            continue
        if scene.end <= start or scene.start >= end:
            continue
        if scene.layout is PictureTag.NOTHING:
            continue
        scene.layout = PictureTag.NOTHING
        scene.graphic = GraphicCard()
        scene.asset_ref = None
        scene.asset_kind = "none"
        scene.reason = "skipped-point-cue"


def _word_timeline(
    transcript: TimedTranscript,
    *,
    after: float,
    before: float,
) -> list[tuple[str, float, float, float]]:
    words: list[tuple[str, float, float, float]] = []
    cues = transcript.cues or []
    if not cues and transcript.text.strip():
        cues = [TranscriptCue(start=0.0, end=max(transcript.duration, 0.0), text=transcript.text)]
    for cue in cues:
        if cue.end <= after or cue.start >= before:
            continue
        tokens = _tokens(cue.text)
        if not tokens:
            continue
        start = max(cue.start, after)
        end = min(cue.end, before) if before < 1e8 else cue.end
        if end <= start:
            continue
        step = (end - start) / max(1, len(tokens))
        cue_start = cue.start
        for offset, token in enumerate(tokens):
            words.append((token, start + offset * step, start + (offset + 1) * step, cue_start))
    return words


def _phrase_score(needle: list[str], needle_text: str, hay: str) -> float:
    hay_tokens = hay.split()
    if not hay_tokens:
        return 0.0
    if hay == needle_text:
        return 1.0
    if _tokens_in_order(needle, hay_tokens):
        extra = max(0, len(hay_tokens) - len(needle))
        return max(FUZZY_RATIO + 0.05, 1.0 - 0.08 * extra)
    return SequenceMatcher(None, needle_text, hay).ratio()


def _tokens_in_order(needle: list[str], hay: list[str]) -> bool:
    pos = 0
    for token in needle:
        try:
            found = hay.index(token, pos)
        except ValueError:
            return False
        pos = found + 1
    return True


def _point_card_phrases(point: TalkPoint) -> list[str]:
    phrases: list[str] = []
    for card_i in range(TALK_CARDS_PER_POINT):
        if point.titles[card_i].strip():
            phrases.append(point.titles[card_i])
        if point.cards[card_i].strip():
            phrases.append(point.cards[card_i])
    if point.platform.strip():
        phrases.append(point.platform)
    return phrases


def _later_point_signals(
    sheet: TalkSheet,
    timed: TimedTranscript,
    open_end: float,
) -> list[float | None]:
    """Earliest transcript signal of each point, including skipped-cue card lines."""
    signals: list[float | None] = [None] * TALK_POINT_COUNT
    after = open_end
    for index, point in enumerate(sheet.points[:TALK_POINT_COUNT]):
        candidates: list[str] = []
        if point.start_cue.strip():
            candidates.append(point.start_cue)
        candidates.extend(_point_card_phrases(point))
        hit: float | None = None
        for phrase in candidates:
            found = find_transcript_hit(timed, phrase, after=after)
            if found is not None and (hit is None or found < hit):
                hit = found
        signals[index] = hit
        if hit is not None:
            after = hit + 0.05
    return signals


def _point_end(
    index: int,
    start: float,
    starts: list[float | None],
    later_signals: list[float | None],
    body_end: float,
) -> float:
    ends: list[float] = []
    for later in range(index + 1, TALK_POINT_COUNT):
        if starts[later] is not None:
            ends.append(starts[later])
        if later_signals[later] is not None:
            ends.append(later_signals[later])
    later = [time for time in ends if time > start + 0.05]
    return min(later) if later else body_end


def _skipped_zone(
    index: int,
    starts: list[float | None],
    later_signals: list[float | None],
    body_end: float,
    timed: TimedTranscript,
    point: TalkPoint,
) -> tuple[float, float] | None:
    """Region that would have been this skipped point. Full-frame host only."""
    zone_start = later_signals[index]
    if zone_start is None:
        zone_start = first_card_line_hit(timed, point, after=0.0)
    if zone_start is None:
        prev = None
        for earlier in range(index - 1, -1, -1):
            if starts[earlier] is not None:
                prev = starts[earlier]
                break
        if prev is None:
            return None
        zone_start = prev
    zone_end = body_end
    for later in range(index + 1, TALK_POINT_COUNT):
        if starts[later] is not None:
            zone_end = starts[later]
            break
        if later_signals[later] is not None:
            zone_end = later_signals[later]
            break
    if zone_end <= zone_start:
        return None
    return (zone_start, zone_end)


def _body_span(script: EditScript) -> tuple[float, float]:
    body = [scene for scene in script.scenes if scene.role == "body" and scene.end > scene.start]
    if not body:
        return (0.0, 0.0)
    return (min(scene.start for scene in body), max(scene.end for scene in body))


def _open_end(script: EditScript, body_start: float) -> float:
    opens = [scene for scene in script.scenes if scene.role == "open" and scene.end > scene.start]
    if opens:
        return max(scene.end for scene in opens)
    return body_start


def _equal_third_windows(start: float, end: float) -> list[tuple[float, float]]:
    span = max(0.0, end - start)
    if span <= 0:
        return [(start, end)] * TALK_POINT_COUNT
    third = span / float(TALK_POINT_COUNT)
    return [(start + i * third, start + (i + 1) * third) for i in range(TALK_POINT_COUNT)]


def _split_body_at(script: EditScript, mark: float) -> None:
    for scene in list(script.scenes):
        if scene.role != "body":
            continue
        if scene.start + 0.05 < mark < scene.end - 0.05:
            left = scene.model_copy(deep=True)
            right = scene.model_copy(deep=True)
            left.end = mark
            right.start = mark
            idx = script.scenes.index(scene)
            script.scenes[idx : idx + 1] = [left, right]
    script.scenes.sort(key=lambda item: (item.start, item.end))
