from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip, write_wav
from ytp.check import (
    check_render,
    load_asr,
    measure_loudness,
    music_under_db,
    speaking_wpm,
    write_report,
)
from ytp.cli import main
from ytp.plan import CaptionCue, EditPlan, MusicCue, NarrationLine, Segment, save_plan
from ytp.rules import repo_root
from ytp.transcribe import Transcript


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


def test_wpm_outside_the_band_fails(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "fast.mp4")
    plan = _base_plan(
        narration=[NarrationLine(id="n", text="one two three", start=0.0, end=0.4)]
    )
    assert speaking_wpm(plan) == pytest.approx(450)
    report = _check(video, plan, rules_root)
    wpm = next(check for check in report.checks if check.id == "wpm")
    assert wpm.passed is False
    assert "450.0" in wpm.summary or "450" in wpm.summary


def test_caption_that_leads_speech_fails(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "cap.mp4")
    plan = _base_plan(
        captions=[CaptionCue(start=0.0, end=1.0, text="one")],
        narration=[NarrationLine(id="n", text="one two three", start=0.5, end=1.0)],
    )
    report = _check(video, plan, rules_root)
    captions = next(check for check in report.checks if check.id == "captions")
    assert any(finding.code == "caption_early" for finding in captions.findings)


def test_music_under_voice_math() -> None:
    assert music_under_db(-16, -16, -17) == 17
    assert abs(music_under_db(-16, -16, 0) - 17) > 3


def test_measure_loudness_reads_ffmpeg(tmp_path: Path) -> None:
    require_ffmpeg()
    wav = write_wav(tmp_path / "tone.wav", seconds=1.0)
    integrated, true_peak = measure_loudness(wav)
    assert integrated < 0
    assert true_peak < 0


def test_asr_bracket_fixture_matches_ep01() -> None:
    asr = load_asr(repo_root() / "fixtures" / "ep01" / "asr-mix-v3d.txt")
    assert asr.words[0].text == "You're"
    assert asr.words[0].start == 0
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


def test_write_report_round_trip(tmp_path: Path, rules_root) -> None:
    video = _video(tmp_path, "ok.mp4")
    report = _check(video, _base_plan(), rules_root)
    markdown, payload = write_report(report, tmp_path / "qc")
    assert markdown.exists() and payload.exists()
    assert report.passed is True
