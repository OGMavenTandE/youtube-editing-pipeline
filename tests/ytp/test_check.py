from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.ytp.mediautil import require_ffmpeg, write_clip, write_wav
from ytp.check import (
    check_render,
    load_asr,
    measure_loudness,
    music_under_db,
    resolve_episode_path,
    speaking_wpm,
    write_report,
)
from ytp.cli import main
from ytp.plan import CaptionCue, EditPlan, MusicCue, NarrationLine, Segment, save_plan
from ytp.rules import repo_root
from ytp.transcribe import Transcript, Word


class Silent:
    name = "silent"

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        return Transcript(text="", words=[])


def _video(tmp_path: Path, name: str = "cut.mp4") -> Path:
    require_ffmpeg()
    return write_clip(tmp_path / name, seconds=1.0, size="320x180")


def _base_plan(**overrides) -> EditPlan:
    plan = EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        segments=[Segment(id="picture", kind="still", source="f.png", start=0.0, duration=1.0)],
        narration=[NarrationLine(id="n", text="one two three", start=0.0, end=1.0)],
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def _check(video: Path, plan: EditPlan, rules_root, **kwargs):
    defaults = dict(
        series="ai-survival",
        rules_root=rules_root,
        transcriber=Silent(),
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    defaults.update(kwargs)
    return check_render(video, plan, **defaults)


def test_vendor_names_and_former_fail(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path)
    plan = _base_plan(
        narration=[
            NarrationLine(
                id="n",
                text="Ask ChatGPT, then say some vendors, and never a former title.",
                start=0.0,
                end=1.0,
            )
        ]
    )
    report = _check(video, plan, rules_root)
    banned = next(check for check in report.checks if check.id == "banned")
    words = {finding.word for finding in banned.findings}
    assert "ChatGPT" in words
    assert "former" in words
    assert "some vendors" not in words
    assert report.passed is False


def test_buzz_in_the_sentence_is_not_a_banned_sound(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "buzz.mp4")
    plan = _base_plan(
        narration=[NarrationLine(id="n", text="You hear a buzz up high.", start=0.0, end=1.0)],
        music=[MusicCue(id="hit", sfx="buzz-style", at=0.2, gain_db=-25)],
    )
    report = _check(video, plan, rules_root)
    banned = next(check for check in report.checks if check.id == "banned")
    assert any(finding.code == "banned_sound" for finding in banned.findings)
    assert not any(
        finding.code == "banned_sound" and finding.word and "hear" in finding.word
        for finding in banned.findings
    )


def test_wpm_outside_the_band_warns_from_the_transcript(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "fast.mp4")
    plan = _base_plan(
        narration=[NarrationLine(id="n", text="one two three", start=0.0, end=10.0)]
    )
    heard = Transcript(
        text="one two three",
        words=[Word("one", 2.0, 2.1), Word("two", 2.1, 2.25), Word("three", 2.25, 2.4)],
        word_timestamps=True,
    )
    report = _check(video, plan, rules_root, asr=heard)
    wpm = next(check for check in report.checks if check.id == "wpm")
    assert wpm.measurements["overall_wpm"] == pytest.approx(450, abs=1)
    assert wpm.measurements["overall_wpm"] != pytest.approx(speaking_wpm(plan), abs=1)
    assert any(item.severity == "warn" and item.code == "overall_wpm" for item in wpm.findings)
    assert wpm.passed is True
    assert report.passed is True


def test_caption_that_leads_speech_fails(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "cap.mp4")
    plan = _base_plan(
        captions=[CaptionCue(start=0.0, end=1.0, text="one")],
        narration=[NarrationLine(id="n", text="one two three", start=0.0, end=1.0)],
    )
    heard = Transcript(
        text="one two three",
        words=[Word("one", 0.5, 0.7), Word("two", 0.7, 0.85), Word("three", 0.85, 1.0)],
        word_timestamps=True,
    )
    report = _check(video, plan, rules_root, asr=heard)
    captions = next(check for check in report.checks if check.id == "captions")
    assert any(finding.code == "caption_early" for finding in captions.findings)


def test_whisper_holes_inside_a_line_are_not_pauses(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "holes.mp4")
    words = []
    cursor = 0.0
    for index in range(8):
        words.append(Word(f"w{index}", cursor, cursor + 0.25))
        cursor += 0.25 + 0.32
    cursor -= 0.32
    text = " ".join(word.text for word in words)
    plan = _base_plan(
        narration=[NarrationLine(id="n", text=text, start=words[0].start, end=cursor)]
    )
    report = _check(
        video,
        plan,
        rules_root,
        asr=Transcript(text=text, words=words, word_timestamps=True),
    )
    wpm = next(item for item in report.checks if item.id == "wpm")
    assert wpm.measurements["articulation_wpm"] == pytest.approx(
        wpm.measurements["overall_wpm"], abs=0.05
    )


def test_bracket_transcript_skips_splice_and_articulation(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "bracket.mp4")
    plan = _base_plan()
    asr_path = tmp_path / "asr.txt"
    asr_path.write_text(
        "[   0.00-   5.00] You're at a picnic when you hear a buzz\n"
        "[   5.00-  10.00] In this story the machines have turned\n",
        encoding="utf-8",
    )
    report = _check(video, plan, rules_root, asr=load_asr(asr_path), transcriber=None)
    splice = next(item for item in report.checks if item.id == "splice_fragments")
    assert "not measured" in splice.summary
    assert any(item.code == "not_measured" for item in splice.findings)
    assert not any(item.word == "You're" for item in splice.findings)
    wpm = next(item for item in report.checks if item.id == "wpm")
    assert "articulation_wpm" not in wpm.measurements
    assert "not measured" in wpm.summary


def test_whisper_transcript_is_scanned_for_banned_words(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "vendor.mp4")
    plan = _base_plan(
        narration=[NarrationLine(id="n", text="Hello there friend.", start=0.0, end=1.0)]
    )

    class Vendor:
        name = "vendor"

        def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
            return Transcript(
                text="ChatGPT",
                words=[Word("ChatGPT", 0.2, 0.6, 0.96)],
                word_timestamps=True,
            )

    report = _check(video, plan, rules_root, transcriber=Vendor(), asr=None)
    banned = next(item for item in report.checks if item.id == "banned")
    assert any(item.word == "ChatGPT" for item in banned.findings)


def test_missing_score_files_are_reported(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "mix.mp4")
    plan = _base_plan(
        voice="audio/voice/voice.wav",
        music=[
            MusicCue(
                id="bed",
                sfx="picnic-ambience",
                at=0.0,
                gain_db=-34,
                source="audio/score/picnic-ambience.wav",
            )
        ],
    )
    report = _check(video, plan, rules_root, episode_dir=tmp_path)
    loudness = next(item for item in report.checks if item.id == "loudness")
    assert "not measured" in loudness.summary
    assert "audio/score" in loudness.summary


def test_music_under_voice_math() -> None:
    assert music_under_db(-16, -16, -17) == 17
    assert abs(music_under_db(-16, -16, 0) - 17) > 3


def test_measure_loudness_reads_ffmpeg(tmp_path: Path) -> None:
    require_ffmpeg()
    wav = write_wav(tmp_path / "tone.wav", seconds=1.0)
    integrated, true_peak = measure_loudness(wav)
    assert integrated < 0
    assert true_peak < 0


def test_asr_bracket_fixture_has_no_invented_word_times() -> None:
    asr = load_asr(repo_root() / "fixtures" / "ep01" / "asr-mix-v3d.txt")
    assert asr.word_timestamps is False
    assert asr.words == []
    assert asr.segments[0][2].startswith("You're")
    assert asr.segments[0][0] == 0
    assert "picnic" in asr.text


def test_check_cli_writes_a_failing_report(tmp_path: Path, rules_root) -> None:
    require_ffmpeg()
    episode = tmp_path / "ep01"
    video = write_clip(episode / "renders" / "cut.mp4", seconds=1.0, size="320x180")
    plan = _base_plan()
    plan_path = episode / "edit" / "plan.yaml"
    save_plan(plan, plan_path)
    asr = episode / "words.json"
    asr.write_text(
        json.dumps({"words": [{"word": "if", "start": 0.4, "end": 0.55}]}),
        encoding="utf-8",
    )
    code = main(
        [
            "check",
            str(video),
            "--plan",
            str(plan_path),
            "--series",
            "ai-survival",
            "--rules-root",
            str(rules_root),
            "--asr",
            str(asr),
            "--profile",
            "episode",
        ]
    )
    assert code == 1
    markdown = (episode / "qc" / "check-report.md").read_text(encoding="utf-8")
    payload = json.loads((episode / "qc" / "check-report.json").read_text(encoding="utf-8"))
    assert "FAIL" in markdown
    assert "if" in markdown
    assert payload["passed"] is False
    stamps = [
        item.get("timestamp_text", "")
        for check in payload["checks"]
        for item in check["findings"]
    ]
    assert any(stamp.startswith("00:") for stamp in stamps)


def _words_for_rates(count: int, overall: float, articulation: float) -> tuple[float, list[Word]]:
    total = count / overall * 60
    speech = count / articulation * 60
    pause = total - speech
    word_dur = speech / count
    words: list[Word] = []
    cursor = 0.0
    for index in range(count):
        if index == count // 2:
            cursor += pause
        words.append(Word(f"w{index}", cursor, cursor + word_dur))
        cursor += word_dur
    return total, words


def test_ep01_like_pace_warns_nothing(tmp_path: Path, rules_root) -> None:
    """166 overall and articulation inside 175-185 is the approved shape. No warning.

    The pause is a gap between narration lines, not a hole Whisper left
    between words inside a line.
    """
    video = _video(tmp_path, "pace.mp4")
    total, words = _words_for_rates(10, 166, 180)
    pause_at = words[5].start
    line_break = words[4].end
    text_a = " ".join(word.text for word in words[:5])
    text_b = " ".join(word.text for word in words[5:])
    plan = _base_plan(
        narration=[
            NarrationLine(id="a", text=text_a, start=0.0, end=line_break),
            NarrationLine(id="b", text=text_b, start=pause_at, end=total),
        ],
        segments=[Segment(id="picture", kind="still", source="f.png", start=0.0, duration=total)],
    )
    heard = Transcript(text=f"{text_a} {text_b}", words=words)
    report = _check(video, plan, rules_root, asr=heard)
    wpm = next(check for check in report.checks if check.id == "wpm")
    assert wpm.findings == []
    assert wpm.measurements["overall_wpm"] == pytest.approx(166, abs=0.05)
    assert wpm.measurements["articulation_wpm"] == pytest.approx(180, abs=0.05)
    assert "WARN" not in wpm.summary


def test_desk_pace_is_warn_even_if_the_band_says_fail(tmp_path: Path, rules_root) -> None:
    path = rules_root / "rules" / "ai-survival.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["lint"]["wpm"]["source_type"] = "desk"
    data["lint"]["wpm"]["desk"]["overall"]["severity"] = "fail"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    video = _video(tmp_path, "desk.mp4")
    plan = _base_plan(narration=[NarrationLine(id="n", text="one two three", start=0.0, end=0.4)])
    plan.meta["source_type"] = "desk"
    heard = Transcript(
        text="one two three",
        words=[Word("one", 0.0, 0.1), Word("two", 0.1, 0.2), Word("three", 0.2, 0.4)],
        word_timestamps=True,
    )
    report = _check(video, plan, rules_root, asr=heard)
    wpm = next(check for check in report.checks if check.id == "wpm")
    assert wpm.findings
    assert all(item.severity == "warn" for item in wpm.findings)
    assert wpm.passed is True
    assert report.passed is True


def test_closers_fail_the_cut(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "closer.mp4")
    plan = _base_plan(
        narration=[
            NarrationLine(
                id="n",
                text="Thanks for watching. Please like and subscribe.",
                start=0.0,
                end=1.0,
            )
        ]
    )
    report = _check(video, plan, rules_root)
    closers = next(check for check in report.checks if check.id == "closers")
    phrases = {item.word for item in closers.findings}
    assert "thanks for watching" in phrases
    assert "like and subscribe" in phrases
    assert closers.passed is False
    assert report.passed is False


def test_talking_head_open_line_end_screen_and_description(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "talk.mp4")
    open_line = "These are my own views, not the Department's."
    plan = EditPlan(
        series="talking-head",
        episode="truth",
        width=1920,
        height=1080,
        segments=[Segment(id="picture", kind="still", source="f.png", start=0.0, duration=17.0)],
        narration=[NarrationLine(id="n", text=open_line, start=0.0, end=10.0)],
        meta={
            "description": "Notes at https://linkedin.com/in/scott and https://x.com/scott_mastin",
            "end_card": {
                "elements": [{"id": "name", "x1": 80, "y1": 80, "x2": 420, "y2": 160}],
            },
        },
    )
    clean = _check(video, plan, rules_root, series="talking-head")
    assert next(item for item in clean.checks if item.id == "required_open").passed
    assert next(item for item in clean.checks if item.id == "description").passed
    end_card = next(item for item in clean.checks if item.id == "end_card")
    assert end_card.passed
    assert not any(item.code == "end_screen_overlap" for item in end_card.findings)
    banned = next(item for item in clean.checks if item.id == "banned")
    assert not any(item.word == "ChatGPT" for item in banned.findings)

    plan.meta["description"] += " The article names ChatGPT."
    named = _check(video, plan, rules_root, series="talking-head")
    banned = next(item for item in named.checks if item.id == "banned")
    assert not any(item.word == "ChatGPT" for item in banned.findings)

    plan.narration[0].text = "Today we talk about the clause."
    missing = _check(video, plan, rules_root, series="talking-head")
    opened = next(item for item in missing.checks if item.id == "required_open")
    assert opened.passed is False

    plan.narration[0].text = open_line
    plan.meta["end_card"]["elements"] = [
        {"id": "bug", "x1": 944, "y1": 170, "x2": 1760, "y2": 629}
    ]
    overlap = _check(video, plan, rules_root, series="talking-head")
    end_card = next(item for item in overlap.checks if item.id == "end_card")
    overlap_hits = [item for item in end_card.findings if item.code == "end_screen_overlap"]
    assert any(item.word == "bug" for item in overlap_hits)
    assert end_card.passed is False

    plan.segments[0].duration = 12.0
    plan.meta["end_card"]["elements"] = [{"id": "name", "x1": 80, "y1": 80, "x2": 420, "y2": 160}]
    short_tail = _check(video, plan, rules_root, series="talking-head")
    end_card = next(item for item in short_tail.checks if item.id == "end_card")
    assert any(item.code == "end_card_duration" for item in end_card.findings)


def test_windows_drive_path_is_not_prefixed_with_the_episode(tmp_path: Path) -> None:
    episode = tmp_path / "ep01"
    asset = resolve_episode_path(episode, "assets/end-card/option-C-clean.png")
    assert asset == episode / "assets" / "end-card" / "option-C-clean.png"
    drive = resolve_episode_path(episode, r"C:\AI Eval\YouTube\take.mp4")
    assert str(drive) == r"C:\AI Eval\YouTube\take.mp4"
    assert episode.as_posix() not in drive.as_posix()


def test_write_report_round_trip(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "ok.mp4")
    report = _check(video, _base_plan(), rules_root)
    markdown, payload = write_report(report, tmp_path / "qc")
    assert markdown.exists() and payload.exists()
    assert report.passed is True
