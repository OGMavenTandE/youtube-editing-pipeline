"""The v8 stray-word failure: a partial cut must not pass the splice check."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip
from ytp.check import check_render
from ytp.ff import probe_duration
from ytp.plan import EditPlan, NarrationLine, Segment
from ytp.splice import compare_window, narration_word_times
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
    assert len(hear.durations) >= 2
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
    v8b = Transcript(
        text="You're",
        words=[*script, Word("You're", 17.545, 17.72, 0.84)],
        word_timestamps=True,
    )
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
