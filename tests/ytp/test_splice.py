"""The v8 stray-word failure: a partial cut must not pass the splice check."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip
from ytp.check import check_render
from ytp.plan import EditPlan, NarrationLine, Segment
from ytp.splice import (
    compare_window,
    fragments_at_join,
    narration_word_times,
    window_is_hallucination,
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
    """Whole-file transcript is the script. A join window hears the stray 'if'."""

    name = "leftover-if"

    def __init__(self, whole: list[Word]) -> None:
        self.whole = whole
        self.calls: list[tuple[float | None, float | None]] = []

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        del media
        self.calls.append((start, end))
        if start is None and end is None:
            return Transcript(text="script", words=list(self.whole), word_timestamps=True)
        if start is not None and end is not None and start <= 0.82 <= end:
            return Transcript(
                text="if",
                words=[Word("if", 0.82, 0.90, 0.9)],
                word_timestamps=True,
            )
        return Transcript(text="", words=[], word_timestamps=True)


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
    hear = LeftoverIf(narration_word_times(plan))
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
    assert leftovers[0].timestamp == pytest.approx(0.82, abs=0.02)
    assert any(start is None and end is None for start, end in hear.calls)
    assert any(start is not None for start, end in hear.calls)
    assert not any(word.text == "if" for word in hear.whole)
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


class _JoinHearing:
    """The full pass misses the fragment. Only the short clip around 17.5s has it."""

    def __init__(self, whole: list[Word], fragment: Word | None) -> None:
        self.whole = whole
        self.fragment = fragment
        self.calls: list[tuple[float | None, float | None]] = []

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        del media
        self.calls.append((start, end))
        if start is None and end is None:
            return Transcript(text="script", words=list(self.whole), word_timestamps=True)
        fragment = self.fragment
        if fragment is not None and start is not None and end is not None:
            if start <= fragment.start <= end:
                return Transcript(text=fragment.text, words=[fragment], word_timestamps=True)
        return Transcript(text="", words=[], word_timestamps=True)


def test_window_its_is_a_fragment_and_your_is_not() -> None:
    """'It's' matches neither edge. 'Your', 'target.', and a quiet mishearing do."""
    plan = _removed_line_plan()
    before, after = plan.narration
    heard = [
        Word("target.", 16.80, 17.12, 0.95),
        Word("It's", 17.48, 17.55, 0.93),
        Word("Your", 17.76, 17.95, 0.95),
        Word("blur", 17.50, 17.58, 0.2),
    ]
    hits = fragments_at_join(before, after, heard, 17.245, near=0.5, edge_seconds=1.5)
    assert [word.text for _code, word in hits] == ["It's"]
    seven = NarrationLine(id="tip", text="Tip seven. Listen first.", start=185.39, end=187.46)
    number = fragments_at_join(
        None,
        seven,
        [Word("7", 185.45, 185.62, 0.9)],
        185.39,
        near=0.5,
        edge_seconds=1.5,
    )
    assert number == []


def test_v8_flags_only_the_its_fragment_and_v8b_is_clean(tmp_path: Path, rules_root) -> None:
    """The whole-file transcript has no 'It's'. The join clip does, and v8b does not."""
    require_ffmpeg()
    video = write_clip(tmp_path / "mix.mp4", seconds=1.0, size="320x180")
    plan = _removed_line_plan()
    script = narration_word_times(plan)
    assert not any(word.text.casefold().startswith("it") for word in script)
    common = dict(
        series="ai-survival",
        rules_root=rules_root,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    v8 = _JoinHearing(script, Word("It's", 17.48, 17.55, 0.93))
    broken = check_render(video, plan, transcriber=v8, **common)
    v8b = _JoinHearing(script, Word("You're", 17.545, 17.72, 0.9))
    clean = check_render(video, plan, transcriber=v8b, **common)
    assert [item.word for item in _splice(broken).findings] == ["It's"]
    assert _splice(broken).findings[0].timestamp == pytest.approx(17.48, abs=0.05)
    assert _splice(clean).findings == []
    assert any(start is None for start, _end in v8.calls)
    assert any(start is not None and start <= 17.48 for start, _end in v8.calls)
    former = [
        item
        for item in clean.checks
        if item.id == "banned"
        for item in item.findings
        if item.word == "former"
    ]
    assert former
    plan.voice = "audio/voice/voice-v3d.wav"
    dry = _JoinHearing(script, Word("It's", 17.48, 17.55, 0.93))
    again = check_render(video, plan, transcriber=dry, **common)
    plan.voice = "audio/mix/mix-v3d.wav"
    mixed_hear = _JoinHearing(script, Word("It's", 17.48, 17.55, 0.93))
    mixed = check_render(video, plan, transcriber=mixed_hear, **common)
    assert [item.word for item in _splice(again).findings] == ["It's"]
    assert [item.word for item in _splice(mixed).findings] == ["It's"]


def test_a_repeated_window_is_dropped_and_the_open_is_not_a_cut(tmp_path: Path, rules_root) -> None:
    """Loops are not fragments. 'of' at the episode open is not a cut."""
    parts = ("I don't know what I'm saying " * 11).split()
    phrase = [
        Word(part, 213.3 + index * 0.1, 213.4 + index * 0.1, 0.9)
        for index, part in enumerate(parts)
    ]
    assert window_is_hallucination(phrase)
    assert not window_is_hallucination([Word("It's", 17.48, 17.55, 0.93)])
    require_ffmpeg()
    video = write_clip(tmp_path / "loop.mp4", seconds=1.0, size="320x180")
    plan = EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        segments=[Segment(id="p", kind="still", source="f.png", start=0.0, duration=8.0)],
        narration=[
            NarrationLine(
                id="open",
                text="You're at a picnic when you hear a buzz.",
                start=1.0,
                end=4.0,
            ),
            NarrationLine(
                id="next",
                text="In this story the machines have turned.",
                start=5.2,
                end=8.0,
            ),
        ],
    )

    class Heard(Silent):
        def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
            del media
            if start is None and end is None:
                words = narration_word_times(plan)
                return Transcript(text="script", words=words, word_timestamps=True)
            if start is not None and end is not None and start <= 1.24 <= end:
                return Transcript(
                    text="of",
                    words=[Word("of", 1.24, 1.34, 0.9)],
                    word_timestamps=True,
                )
            return Transcript(
                text="it's hot",
                words=[Word("it's", 4.05 + i * 0.1, 4.12 + i * 0.1, 0.9) for i in range(16)]
                + [Word("hot", 4.08 + i * 0.1, 4.14 + i * 0.1, 0.9) for i in range(16)],
                word_timestamps=True,
            )

    report = check_render(
        video,
        plan,
        series="ai-survival",
        rules_root=rules_root,
        transcriber=Heard(),
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    assert _splice(report).findings == []


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
