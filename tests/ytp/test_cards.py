"""Talking-head cards: safe placement, end card length, and the open line in the audio."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip, write_png, write_wav
from ytp.cards import card_box
from ytp.check import check_render
from ytp.plan import Card, EditPlan, NarrationLine, Segment
from ytp.render import render_plan
from ytp.rules import load_rules
from ytp.transcribe import Transcript, Word

OPEN = "These are my own views, not the Department's."


def _overlaps_slot(box, slot) -> bool:
    return box[0] < slot[2] and box[2] > slot[0] and box[1] < slot[3] and box[3] > slot[1]


def test_open_line_allows_one_missing_word() -> None:
    from ytp.check import open_line_match

    required = "These are my own views, not the Department's."
    heard = "Intro. These are my views, not the Department. Thanks."
    kind, quote = open_line_match(required, heard)
    assert kind == "fuzzy"
    assert quote == "these are my views not the department"
    assert open_line_match(required, required)[0] == "exact"
    assert open_line_match(required, "hello there")[0] == "miss"


def test_standalone_check_counts_the_end_card_tail(tmp_path: Path, rules_root, monkeypatch) -> None:
    video = write_clip(tmp_path / "talk.mp4", seconds=0.4, size="320x180")
    plan = EditPlan(
        series="talking-head",
        episode="t",
        width=1920,
        height=1080,
        segments=[Segment(id="a", kind="still", source="frames/a.png", start=0.0, duration=60.0)],
        narration=[NarrationLine(id="n", text=OPEN, start=0.0, end=60.0)],
    )
    monkeypatch.setattr("ytp.check.probe_duration", lambda _path: 66.9)
    monkeypatch.setattr("ytp.check.probe_video_size", lambda _path: (1920, 1080))
    report = check_render(
        video,
        plan,
        series="talking-head",
        rules_root=rules_root,
        asr=Transcript(
            text=OPEN,
            word_timestamps=True,
            words=[Word("These", 23.0, 23.2), Word("are", 23.2, 23.3)],
        ),
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    container = next(item for item in report.checks if item.id == "container")
    assert container.passed
    assert not any(item.code == "duration" for item in container.findings)


def test_card_styles_stay_out_of_the_end_screen_slots(rules_root) -> None:
    rules = load_rules("talking-head", rules_root)
    slots = []
    for slot in rules.lint["end_card"]["clear_slots"]:
        slots.append((slot["x1"], slot["y1"], slot["x2"], slot["y2"]))
    for style in ("title", "lower-third", "end-card"):
        box = card_box(style, 1920, 1080)
        assert not any(_overlaps_slot(box, slot) for slot in slots)


def test_card_text_and_slot_overlap(tmp_path: Path, rules_root) -> None:
    video = write_clip(tmp_path / "talk.mp4", seconds=1.0, size="320x180")
    plan = EditPlan(
        series="talking-head",
        episode="t",
        width=1920,
        height=1080,
        segments=[Segment(id="picture", kind="still", source="f.png", start=0.0, duration=17.0)],
        narration=[NarrationLine(id="n", text="Today we talk.", start=0.0, end=10.0)],
        cards=[Card(id="title", text="The truth clause", start=0.5, duration=3.0, style="title")],
    )
    manifest = {
        "cards": [
            {
                "id": "title",
                "text": "Wrong title",
                "style": "title",
                "start": 0.5,
                "duration": 3.0,
                "x1": 944,
                "y1": 170,
                "x2": 1760,
                "y2": 400,
            }
        ]
    }
    (tmp_path / "cards.json").write_text(json.dumps(manifest), encoding="utf-8")
    heard = Transcript(
        text=OPEN,
        word_timestamps=True,
        words=[
            Word(part, index * 0.3, index * 0.3 + 0.25) for index, part in enumerate(OPEN.split())
        ],
    )
    report = check_render(
        video,
        plan,
        series="talking-head",
        rules_root=rules_root,
        asr=heard,
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    cards = next(item for item in report.checks if item.id == "cards")
    assert any(item.code == "card_text" for item in cards.findings)
    assert any(item.code == "end_screen_overlap" for item in cards.findings)
    opened = next(item for item in report.checks if item.id == "required_open")
    assert opened.passed

    heard.words = [Word("hello", 0.2, 0.4)]
    plan.narration[0].text = OPEN
    missed = check_render(
        video,
        plan,
        series="talking-head",
        rules_root=rules_root,
        asr=heard,
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
    )
    assert next(item for item in missed.checks if item.id == "required_open").passed is False


def test_render_appends_the_end_card_and_records_card_text(tmp_path: Path, rules_root) -> None:
    require_ffmpeg()
    episode = tmp_path / "ep"
    write_png(episode / "frames" / "a.png", size="320x180")
    write_png(episode / "assets" / "end-card" / "option-C-clean.png", size="320x180")
    write_wav(episode / "audio" / "voice" / "voice.wav", seconds=1.0)
    plan = EditPlan(
        series="talking-head",
        episode="t",
        width=320,
        height=180,
        voice="audio/voice/voice.wav",
        segments=[Segment(id="a", kind="still", source="frames/a.png", start=0.0, duration=0.5)],
        narration=[NarrationLine(id="n", text=OPEN, start=0.0, end=0.4)],
        cards=[Card(id="title", text="The truth clause", start=0.0, duration=0.4, style="title")],
    )
    out = episode / "renders" / "v1" / "plan.mp4"
    render_plan(plan, out, episode_dir=episode)
    assert any(segment.id == "end-card" for segment in plan.segments)
    manifest = json.loads((out.parent / "cards.json").read_text(encoding="utf-8"))
    assert manifest["cards"][0]["text"] == "The truth clause"
    report = check_render(
        out,
        plan,
        series="talking-head",
        rules_root=rules_root,
        asr=Transcript(
            text=OPEN,
            word_timestamps=True,
            words=[Word(part, i * 0.05, i * 0.05 + 0.04) for i, part in enumerate(OPEN.split())],
        ),
        transcriber=None,
        profile="episode",
        loudness_fn=lambda _path: (-16.0, -2.0),
        episode_dir=episode,
    )
    assert next(item for item in report.checks if item.id == "cards").passed
    end_card = next(item for item in report.checks if item.id == "end_card")
    assert end_card.passed
    assert end_card.measurements["tail_seconds"] == pytest.approx(7.0, abs=0.05)
