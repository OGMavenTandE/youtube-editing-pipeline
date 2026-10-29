"""The v8 stray-word failure: a partial cut must not pass the splice check."""

from __future__ import annotations

from pathlib import Path

from tests.ytp.mediautil import require_ffmpeg, write_clip
from ytp.check import check_render
from ytp.ff import probe_duration
from ytp.plan import EditPlan, NarrationLine, Segment
from ytp.splice import compare_window
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
    full = probe_duration(video)
    assert hear.durations
    assert len(hear.durations) >= 2
    assert max(hear.durations) < full - 0.2
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
