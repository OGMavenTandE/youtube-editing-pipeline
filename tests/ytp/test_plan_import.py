from __future__ import annotations

import json
from pathlib import Path

from ytp.cli import main
from ytp.import_ep01 import import_episode, parse_concat, parse_cue_time, parse_score_cues
from ytp.plan import load_plan, plan_to_dict, save_plan, validate_plan
from ytp.rules import repo_root

FIXTURES = repo_root() / "fixtures" / "ep01"


def test_cue_times_match_the_score_sheet() -> None:
    assert parse_cue_time("0:00.00") == 0
    assert parse_cue_time("0:02.30") == 2.3
    assert parse_cue_time("1:12.01") == 72.01
    assert parse_cue_time("4:12.70") == 252.70


def test_concat_drops_the_repeated_end_card() -> None:
    entries = parse_concat((FIXTURES / "rc8b-concat.txt").read_text(encoding="utf-8"))
    durations = [item[1] for item in entries]
    assert all(item is not None for item in durations)
    assert entries[0][0].endswith("ep01-v3-000-coldopen.png")
    assert entries[0][1] == 5.1
    assert entries[-1][0].endswith("ep01-v3-13-endcard-noname.png")
    assert entries[-1][1] == 5.5
    assert sum(1 for path, _ in entries if path.endswith("endcard-noname.png")) == 1


def test_import_ep01_matches_the_shipped_artifacts(tmp_path: Path) -> None:
    plan = import_episode(
        timeline_path=FIXTURES / "timeline-v3d.json",
        concat_path=FIXTURES / "rc8b-concat.txt",
        score_path=FIXTURES / "score-cues-v3d.md",
        lines_path=FIXTURES / "lines.json",
    )
    timeline = json.loads((FIXTURES / "timeline-v3d.json").read_text(encoding="utf-8"))
    assert len(plan.narration) == len(timeline["clips"])
    assert plan.narration[0].id == "00e_b1"
    assert plan.narration[0].text.startswith("You're at a picnic")
    assert "00d_b1" not in {line.id for line in plan.narration}
    assert plan.meta["script_ids_not_in_timeline"] == ["00d_b1"]
    assert plan.voice == "voice-v3d.wav"
    assert plan.lead_in == 1.0
    assert plan.meta["timeline_total"] == 250.105
    assert plan.captions[0].text == plan.narration[0].text
    assert plan.shorts[0].id == "A"
    assert plan.shorts[0].title == "Glass Is a Wall to Thermal"
    assert plan.shorts[0].width == 1080
    assert plan.shorts[0].height == 1920
    assert plan.beats[0].title == "Cold open"
    assert plan.palettes[0].name == "DARK"

    cues = parse_score_cues((FIXTURES / "score-cues-v3d.md").read_text(encoding="utf-8"))
    assert len(plan.music) == len(cues) == 38
    assert plan.music[0].sfx.startswith("picnic-ambience")
    assert plan.music[0].at == 0
    assert plan.music[0].gain_db == -34
    assert plan.music[0].duck_under_voice is True
    power_down = plan.music[-1]
    assert power_down.sfx == "power-down"
    assert power_down.at == 252.70
    assert validate_plan(plan) == []

    out = tmp_path / "plan.yaml"
    save_plan(plan, out)
    loaded = load_plan(out)
    assert plan_to_dict(loaded) == plan_to_dict(plan)
    json_out = tmp_path / "plan.json"
    save_plan(plan, json_out)
    assert load_plan(json_out).narration[0].id == "00e_b1"


def test_plan_import_cli(tmp_path: Path) -> None:
    out = tmp_path / "edit" / "plan.yaml"
    code = main(
        [
            "plan",
            "import",
            "--timeline",
            str(FIXTURES / "timeline-v3d.json"),
            "--concat",
            str(FIXTURES / "rc8b-concat.txt"),
            "--score",
            str(FIXTURES / "score-cues-v3d.md"),
            "--lines",
            str(FIXTURES / "lines.json"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert load_plan(out).episode == "ep01"
    assert main(["plan", "validate", str(out)]) == 0


def test_validate_flags_a_partial_narration_window() -> None:
    plan = import_episode(
        timeline_path=FIXTURES / "timeline-v3d.json",
        concat_path=FIXTURES / "rc8b-concat.txt",
        score_path=FIXTURES / "score-cues-v3d.md",
    )
    plan.narration[0].end = plan.narration[0].start
    errors = validate_plan(plan)
    assert any("00e_b1" in error for error in errors)


def test_schema_lists_the_documented_fields() -> None:
    schema_path = repo_root() / "docs" / "edit-plan.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    plan = import_episode(
        timeline_path=FIXTURES / "timeline-v3d.json",
        concat_path=FIXTURES / "rc8b-concat.txt",
        score_path=FIXTURES / "score-cues-v3d.md",
    )
    assert set(schema["properties"]) == set(plan_to_dict(plan))
    assert schema["properties"]["version"]["const"] == 1
