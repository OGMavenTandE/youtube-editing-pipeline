"""The v8 stray-word failure: a partial cut must not pass the splice check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip
from ytp.check import check_render
from ytp.ff import probe_duration
from ytp.plan import EditPlan, NarrationLine, Segment
from ytp.rules import repo_root
from ytp.splice import (
    _tokens_close,
    canonicalize_tokens,
    compare_window,
    narration_word_times,
    tokenize,
)
from ytp.transcribe import Transcript, Word


def _plan() -> EditPlan:
    return EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        fps=30,
        voice=None,
        segments=[
            Segment(id="picture", kind="still", source="frame.png", start=0.0, duration=1.6),
        ],
        narration=[
            NarrationLine(
                id="a",
                text="Stay away and report it.",
                start=0.1,
                end=0.8,
            ),
            NarrationLine(
                id="b",
                text="Here is what testers learn.",
                start=0.85,
                end=1.5,
            ),
        ],
    )


class LeftoverIf:
    """Returns a stray 'if' and records that each call was a short window, not the master."""

    name = "leftover-if"

    def __init__(self) -> None:
        self.durations: list[float] = []

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        duration = probe_duration(media)
        self.durations.append(duration)
        middle = duration / 2
        return Transcript(
            text="if",
            words=[Word("if", middle - 0.03, middle + 0.03)],
        )


class Silent:
    name = "silent"

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        return Transcript(text="", words=[])


def test_stray_if_is_not_in_the_approved_window() -> None:
    expected = [
        Word("it", 0.66, 0.80),
        Word("Here", 0.85, 0.98),
    ]
    actual = [
        Word("it", 0.66, 0.80),
        Word("if", 1.02, 1.12),
        Word("Here", 0.85, 0.98),
    ]
    extras = compare_window(expected, actual)
    assert any(code == "stray_fragment" and word.text == "if" for code, word in extras)


def test_clipped_prefix_is_a_partial_word() -> None:
    extras = compare_window([Word("report", 0.52, 0.70)], [Word("re", 0.52, 0.70)])
    assert extras
    assert extras[0][0] == "partial_word"
    assert extras[0][1].text == "re"


def test_clean_window_has_no_extras() -> None:
    words = [Word("report", 0.5, 0.7), Word("it", 0.7, 0.8)]
    assert compare_window(words, words) == []


def test_partial_word_left_at_cut_fails_the_check(tmp_path: Path, rules_root) -> None:
    """Ep01 v8 left a stray 'if' from a sentence that was only partly removed."""
    require_ffmpeg()
    video = write_clip(tmp_path / "cut.mp4", seconds=1.6, size="320x180")
    plan = _plan()
    hear = LeftoverIf()
    report = check_render(
        video,
        plan,
        series="ai-survival",
        rules_root=rules_root,
        transcriber=hear,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
        episode_dir=tmp_path,
    )
    assert report.passed is False
    splice = next(check for check in report.checks if check.id == "splice_fragments")
    assert splice.passed is False
    leftovers = [finding for finding in splice.findings if finding.word == "if"]
    assert leftovers
    assert leftovers[0].code in {"stray_fragment", "partial_word"}
    assert leftovers[0].timestamp is not None
    assert hear.durations
    assert len(hear.durations) == 1
    markdown = report.to_markdown()
    payload = report.to_dict()
    assert "FAIL" in markdown
    assert "00:" in markdown
    assert payload["passed"] is False
    words = [
        finding.get("word")
        for check in payload["checks"]
        for finding in check["findings"]
    ]
    assert "if" in words


def _removed_line_plan() -> EditPlan:
    """The Ep01 join where 00d_b1 was cut, plus a picture change that is not an audio splice."""
    previous = (
        "In the next four minutes, you'll see what an AI drone actually sees, "
        "and get ten common-sense ways to not look like a target."
    )
    following = "Your guide today is a former Pentagon AI test and evaluation program manager."
    return EditPlan(
        series="ai-survival",
        episode="ep01",
        width=320,
        height=180,
        segments=[
            Segment(id="still-a", kind="still", source="a.png", start=10.0, duration=6.0),
            Segment(id="still-b", kind="still", source="b.png", start=16.0, duration=12.0),
        ],
        narration=[
            NarrationLine(id="00c_b1", text=previous, start=10.065, end=17.245),
            NarrationLine(id="04_b2", text=following, start=18.045, end=27.214),
        ],
    )


def test_v8_flags_only_the_its_fragment_and_v8b_is_clean(tmp_path: Path, rules_root) -> None:
    """v8 hears 'It's' from removed 00d_b1. v8b hears 'You're' for 'Your' and is clean."""
    require_ffmpeg()
    video = write_clip(tmp_path / "mix.mp4", seconds=1.0, size="320x180")
    plan = _removed_line_plan()
    script = narration_word_times(plan)
    v8 = Transcript(
        text="It's",
        words=[*script, Word("It's", 17.48, 17.62, 0.93)],
        word_timestamps=True,
    )
    your = next(word for word in script if word.text == "Your")
    v8b_words = [
        Word("You're", 17.545, 17.72, 0.84) if word is your else word for word in script
    ]
    v8b = Transcript(text="You're", words=v8b_words, word_timestamps=True)
    broken = check_render(
        video,
        plan,
        series="ai-survival",
        rules_root=rules_root,
        asr=v8,
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    clean = check_render(
        video,
        plan,
        series="ai-survival",
        rules_root=rules_root,
        asr=v8b,
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    broken_splice = next(item for item in broken.checks if item.id == "splice_fragments")
    clean_splice = next(item for item in clean.checks if item.id == "splice_fragments")
    assert [item.word for item in broken_splice.findings] == ["It's"]
    assert broken_splice.findings[0].timestamp == pytest.approx(17.48, abs=0.05)
    assert clean_splice.findings == []
    assert clean_splice.passed is True
    former = [
        item
        for item in clean.checks
        if item.id == "banned"
        for item in item.findings
        if item.word == "former"
    ]
    assert former


def test_numbers_and_low_confidence_mishearings_are_not_fragments() -> None:
    expected = [Word("Tip", 0.0, 0.3), Word("one", 0.3, 0.6), Word("picnic", 0.6, 1.0)]
    heard = [
        Word("Tip", 0.0, 0.3, 0.9),
        Word("1", 0.3, 0.55, 0.88),
        Word("pick", 0.6, 0.8, 0.9),
        Word("Sir", 0.85, 0.95, 0.2),
        Word("Toot", 1.0, 1.1, 0.3),
    ]
    assert compare_window(expected, heard) == []


def _timeline_plan() -> EditPlan:
    clips = json.loads((repo_root() / "fixtures" / "ep01" / "timeline-v3d.json").read_text())
    return EditPlan(
        series="ai-survival",
        episode="ep01",
        width=1920,
        height=1080,
        narration=[
            NarrationLine(
                id=str(clip["id"]),
                text=str(clip["text"]),
                start=float(clip["start"]),
                end=float(clip["end"]),
            )
            for clip in clips["clips"]
        ],
        segments=[
            Segment(id="picture", kind="still", source="frames/a.png", start=0.0, duration=250.105)
        ],
    )


def _hits(name: str) -> list[dict[str, object]]:
    path = repo_root() / "fixtures" / "ep01" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _token_key(text: str) -> str:
    tokens = canonicalize_tokens(tokenize(text))
    return tokens[0] if tokens else ""


def _asr_from_script(
    plan: EditPlan,
    hits: list[dict[str, object]],
    *,
    insert_its: bool,
) -> list[Word]:
    """Script words in order. Reported hits stamp the nearest matching word.

    'It's' is not in the kept script. It is inserted only for the v8 list.
    """
    words: list[Word] = []
    # Index where removed line 00d_b1 sat: after the line that ends before 17.4s.
    insert_at: int | None = None
    for line in plan.narration:
        if insert_its and insert_at is None and line.start >= 17.4:
            insert_at = len(words)
        tokens = tokenize(line.text)
        if not tokens:
            continue
        step = max(line.end - line.start, 0.05) / len(tokens)
        for index, token in enumerate(tokens):
            start = line.start + step * index
            words.append(Word(token, start, start + min(step * 0.6, 0.25), 0.95))
    used: set[int] = set()
    for hit in hits:
        surface = str(hit["word"])
        if surface.casefold().strip(".,") == "it's":
            continue
        hit_key = _token_key(surface)
        best: tuple[tuple[int, float], int] | None = None
        for index, word in enumerate(words):
            if index in used:
                continue
            word_key = _token_key(word.text)
            exact = word_key == hit_key
            if not exact and not _tokens_close(hit_key, word_key):
                continue
            distance = abs(((word.start + word.end) / 2) - float(hit["start"]))
            # Keep the script order. An exact token wins over a nearer fuzzy one.
            rank = (0 if exact else 1, distance)
            if best is None or rank < best[0]:
                best = (rank, index)
        if best is None:
            raise AssertionError(f"{surface} is not in the script")
        index = best[1]
        used.add(index)
        words[index] = Word(surface, float(hit["start"]), float(hit["end"]), 0.95)
    if insert_its:
        at = insert_at if insert_at is not None else len(words)
        words.insert(at, Word("It's", 17.445, 17.62, 0.95))
    return words


def test_reported_v8_and_v8b_word_lists(tmp_path: Path, rules_root) -> None:
    """Words copied from the real v8/v8b reports. v8 keeps It's; v8b keeps nothing."""
    require_ffmpeg()
    video = write_clip(tmp_path / "mix.mp4", seconds=1.0, size="320x180")
    plan = _timeline_plan()
    v8_words = _asr_from_script(plan, _hits("splice-hits-v8.json"), insert_its=True)
    v8b_words = _asr_from_script(plan, _hits("splice-hits-v8b.json"), insert_its=False)
    common = dict(
        series="ai-survival",
        rules_root=rules_root,
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    broken = check_render(
        video,
        plan,
        asr=Transcript(text="It's", words=v8_words, word_timestamps=True),
        **common,
    )
    clean = check_render(
        video,
        plan,
        asr=Transcript(text="", words=v8b_words, word_timestamps=True),
        **common,
    )
    broken_hits = [item.word for item in _splice(broken).findings]
    assert broken_hits == ["It's"]
    assert _splice(broken).findings[0].timestamp == pytest.approx(17.445, abs=0.02)
    assert _splice(clean).findings == []
    plan.voice = "audio/voice/voice-v3d.wav"
    again = check_render(
        video,
        plan,
        asr=Transcript(text="It's", words=v8_words, word_timestamps=True),
        **common,
    )
    plan.voice = "audio/mix/mix-v3d.wav"
    mixed = check_render(
        video,
        plan,
        asr=Transcript(text="It's", words=v8_words, word_timestamps=True),
        **common,
    )
    assert [item.word for item in _splice(again).findings] == ["It's"]
    assert [item.word for item in _splice(mixed).findings] == ["It's"]


def _splice(report):
    return next(item for item in report.checks if item.id == "splice_fragments")


def test_clean_windows_pass_the_splice_check(tmp_path: Path, rules_root) -> None:
    require_ffmpeg()
    video = write_clip(tmp_path / "clean.mp4", seconds=1.0, size="320x180")
    plan = EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        segments=[Segment(id="picture", kind="still", source="f.png", start=0.0, duration=1.0)],
        narration=[NarrationLine(id="n", text="one two three", start=0.0, end=1.0)],
        captions=[],
    )
    report = check_render(
        video,
        plan,
        series="ai-survival",
        rules_root=rules_root,
        transcriber=Silent(),
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    splice = next(check for check in report.checks if check.id == "splice_fragments")
    assert splice.passed is True
    assert report.passed is True
