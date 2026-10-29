"""Loudnorm mastering: captured JSON, a quiet render, and the skip/denoise flags."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_png, write_wav
from ytp.check import Report, _check_loudness, write_report
from ytp.cli import _build_parser
from ytp.ff import probe_audio_rate, probe_duration
from ytp.master import (
    MasterResult,
    denoise_filter,
    dynamic_note,
    linear_loudnorm_filter,
    loudness_settings,
    master_true_peak,
    parse_loudnorm,
)
from ytp.plan import EditPlan, NarrationLine, Segment
from ytp.render import render_plan
from ytp.rules import load_rules, repo_root

_FIXTURES = Path(__file__).parent / "fixtures"


def _captured(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _noise(path: Path, *, seconds: float = 3.0, amplitude: float = 0.08) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            (
                "anoisesrc=color=white:sample_rate=48000:"
                f"amplitude={amplitude}:duration={seconds}"
            ),
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return path


def _quiet_plan(episode: Path, *, seconds: float = 3.0) -> EditPlan:
    write_png(episode / "frames" / "a.png", size="320x180")
    _noise(episode / "audio" / "voice" / "voice.wav", seconds=seconds)
    return EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        fps=30,
        voice="audio/voice/voice.wav",
        segments=[
            Segment(id="a", kind="still", source="frames/a.png", start=0.0, duration=seconds)
        ],
        narration=[NarrationLine(id="n", text="Stay low.", start=0.0, end=min(0.4, seconds))],
    )


def _joined(commands: list[list[str]]) -> str:
    return "\n".join(" ".join(cmd) for cmd in commands)


def test_two_pass_loudnorm_json_parses_captured_sample() -> None:
    measured = parse_loudnorm(_captured("loudnorm-pass1.json"))
    assert measured["input_i"] == "-23.76"
    assert measured["input_tp"] == "-16.54"
    assert measured["target_offset"] == "0.01"
    filt = linear_loudnorm_filter(measured, target_i=-16, target_tp=-1.5, target_lra=11)
    assert "measured_I=-23.76" in filt
    assert "measured_TP=-16.54" in filt
    assert "measured_LRA=0.00" in filt
    assert "measured_thresh=-33.76" in filt
    assert "offset=0.01" in filt
    assert "linear=true" in filt
    assert "I=-16:" in filt
    assert "TP=-1.5:" in filt
    assert "LRA=11:" in filt
    applied = parse_loudnorm(_captured("loudnorm-pass2.json"))
    assert applied["output_i"] == "-16.01"
    assert applied["output_tp"] == "-8.78"
    assert dynamic_note(-23.76, -16.54, -16) == (
        "loudnorm used dynamic mode: not enough peak headroom for linear gain "
        "(would peak at -8.8 dBTP)"
    )
    assert "would peak at +4.2 dBTP" in dynamic_note(-24.36, -4.16, -16)
    linear = parse_loudnorm(_captured("loudnorm-linear.json"))
    assert linear["normalization_type"] == "linear"


def test_targets_and_denoise_strength_come_from_series_rules() -> None:
    settings = loudness_settings("ai-survival")
    assert settings["voice_lufs"] == -16
    assert settings["true_peak_dbtp"] == -1.5
    assert settings["master_tp_margin_db"] == 0.5
    assert master_true_peak(settings) == -2
    assert settings["lra"] == 11
    assert denoise_filter(settings) == "highpass=f=80,afftdn=nr=10:nf=-50:tn=1"
    head = loudness_settings("talking-head")
    assert head["voice_lufs"] == -16
    assert head["lra"] == 11
    assert denoise_filter(head) == denoise_filter(settings)


def test_dynamic_mode_is_an_info_note_not_a_warning(tmp_path: Path) -> None:
    video = tmp_path / "plan.mp4"
    video.write_bytes(b"")
    sidecar = {
        "mastered": True,
        "input_i": -23.76,
        "input_tp": -16.54,
        "output_i": -16.01,
        "output_tp": -8.78,
        "normalization_type": "dynamic",
    }
    video.with_suffix(".loudness.json").write_text(
        json.dumps(sidecar) + "\n",
        encoding="utf-8",
    )
    rules = load_rules("ai-survival")
    plan = EditPlan(
        series="ai-survival",
        width=320,
        height=180,
        segments=[Segment(id="a", kind="still", source="a.png", start=0, duration=1)],
    )
    result = _check_loudness(video, plan, rules, False, lambda _path: (-16.0, -2.0), None)
    assert result.passed
    assert not any(item.severity in {"warn", "fail"} for item in result.findings)
    report = Report("v", "p", "ai-survival", True, [result])
    markdown, _payload = write_report(report, tmp_path / "qc")
    text = markdown.read_text(encoding="utf-8")
    assert "## loudness — PASS" in text
    expected = (
        "mastered from input -23.76 LUFS / -16.54 dBTP "
        "to output -16.01 LUFS / -8.78 dBTP"
    )
    assert expected in text
    assert "loudnorm used dynamic mode" in text
    assert "would peak at -8.8 dBTP" in text
    assert "WARN" not in text


def test_quiet_render_lands_on_the_loudness_target(tmp_path: Path) -> None:
    require_ffmpeg()
    from ytp.check import check_render, measure_loudness

    episode = tmp_path / "ep"
    plan = _quiet_plan(episode)
    source_i, _source_tp = measure_loudness(episode / "audio" / "voice" / "voice.wav")
    assert -30 < source_i < -20
    out = episode / "renders" / "v1" / "plan.mp4"
    result = render_plan(plan, out, episode_dir=episode, preset="ultrafast")
    integrated, true_peak = measure_loudness(result.video)
    assert integrated == pytest.approx(-16, abs=1)
    assert true_peak <= -1.5
    assert probe_audio_rate(result.video) == 48000
    log = (out.parent / "render.log").read_text(encoding="utf-8")
    assert "input I=" in log
    assert "output I=" in log
    assert "af measure: " in log
    assert "af apply: " in log
    assert "TP=-2:" in log
    sidecar = json.loads(out.with_suffix(".loudness.json").read_text(encoding="utf-8"))
    assert sidecar["mastered"] is True
    assert sidecar["input_i"] == pytest.approx(source_i, abs=1.5)
    report = check_render(
        result.video,
        plan,
        series="ai-survival",
        rules_root=repo_root(),
        transcriber=None,
        episode_dir=episode,
        loudness_fn=measure_loudness,
    )
    loudness = next(item for item in report.checks if item.id == "loudness")
    assert loudness.passed
    assert loudness.measurements["integrated_lufs"] == pytest.approx(-16, abs=1)
    assert loudness.measurements["true_peak_dbtp"] <= -1.5
    assert "mastered from input" in loudness.summary
    text = report.to_markdown()
    assert "mastered from input" in text
    assert "## loudness — PASS" in text
    assert not any(item.severity == "warn" for item in loudness.findings)
    if sidecar["normalization_type"] == "dynamic":
        assert "loudnorm used dynamic mode" in loudness.summary
        assert "would peak at" in log
        assert "WARN:" not in log


def test_no_master_leaves_the_level_alone(tmp_path: Path) -> None:
    require_ffmpeg()
    from ytp.check import measure_loudness

    episode = tmp_path / "ep"
    plan = _quiet_plan(episode)
    source_i, _peak = measure_loudness(episode / "audio" / "voice" / "voice.wav")
    out = episode / "renders" / "v1" / "plan.mp4"
    result = render_plan(plan, out, episode_dir=episode, preset="ultrafast", master=False)
    integrated, _true_peak = measure_loudness(result.video)
    assert integrated < -20
    assert integrated == pytest.approx(source_i, abs=1.5)
    assert "loudnorm" not in _joined(result.commands)
    assert "mastering skipped" in (out.parent / "render.log").read_text(encoding="utf-8")
    assert not out.with_suffix(".loudness.json").exists()


def test_denoise_adds_highpass_and_afftdn(tmp_path: Path) -> None:
    require_ffmpeg()
    episode = tmp_path / "ep"
    plan = _quiet_plan(episode, seconds=1.0)
    out = episode / "renders" / "v1" / "plan.mp4"
    result = render_plan(plan, out, episode_dir=episode, preset="ultrafast", denoise=True)
    chain = _joined(result.commands)
    assert "highpass=f=80,afftdn=nr=10:nf=-50:tn=1,loudnorm=" in chain
    assert "linear=true" in chain
    log = (out.parent / "render.log").read_text(encoding="utf-8")
    assert "af measure: highpass=f=80,afftdn=nr=10:nf=-50:tn=1,loudnorm=" in log
    assert "af apply: highpass=f=80,afftdn=nr=10:nf=-50:tn=1,loudnorm=" in log


def test_master_sees_the_mix_through_the_end_card(tmp_path: Path, monkeypatch) -> None:
    require_ffmpeg()
    episode = tmp_path / "ep"
    write_png(episode / "frames" / "a.png", size="320x180")
    write_wav(episode / "audio" / "voice" / "voice.wav", seconds=1.0)
    plan = EditPlan(
        series="talking-head",
        episode="t",
        width=320,
        height=180,
        voice="audio/voice/voice.wav",
        segments=[Segment(id="a", kind="still", source="frames/a.png", start=0.0, duration=0.5)],
        narration=[NarrationLine(id="n", text="Hello from the desk.", start=0.0, end=0.4)],
    )
    seen: dict[str, float] = {}

    def spy(source, dest, **kwargs):
        del kwargs
        seen["duration"] = probe_duration(source)
        shutil.copyfile(source, dest)
        return MasterResult(path=dest, mastered=False, dynamic=False)

    monkeypatch.setattr("ytp.render.master_audio", spy)
    out = episode / "renders" / "v1" / "plan.mp4"
    render_plan(plan, out, episode_dir=episode, preset="ultrafast")
    assert any(segment.id == "end-card" for segment in plan.segments)
    assert seen["duration"] == pytest.approx(plan.picture_duration(), abs=0.2)
    assert plan.picture_duration() >= 7


def _hot_quiet_wav(path: Path, *, seconds: float = 5.0) -> Path:
    """About -24 LUFS, with peaks hot enough that linear gain would clip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    expr = (
        "aevalsrc='0.07*sin(2*PI*220*t)"
        "+0.36*sin(2*PI*1800*t)*lt(mod(t\\,0.40),0.008)'"
        f":s=48000:d={seconds}"
    )
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            expr,
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return path


def test_mastered_aac_passes_the_loudness_check(tmp_path: Path) -> None:
    require_ffmpeg()
    from ytp.check import check_render, measure_loudness

    episode = tmp_path / "ep"
    seconds = 5.0
    write_png(episode / "frames" / "a.png", size="320x180")
    _hot_quiet_wav(episode / "audio" / "voice" / "voice.wav", seconds=seconds)
    source_i, _source_tp = measure_loudness(episode / "audio" / "voice" / "voice.wav")
    assert -28 < source_i < -20
    plan = EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        fps=30,
        voice="audio/voice/voice.wav",
        segments=[
            Segment(
                id="a",
                kind="still",
                source="frames/a.png",
                start=0.0,
                duration=seconds,
            )
        ],
        narration=[NarrationLine(id="n", text="Stay low.", start=0.0, end=0.4)],
    )
    out = episode / "renders" / "v1" / "plan.mp4"
    result = render_plan(plan, out, episode_dir=episode, preset="ultrafast")
    assert "TP=-2:" in _joined(result.commands)
    assert "TP=-1.5:" not in _joined(result.commands)
    assert probe_audio_rate(result.video) == 48000
    report = check_render(
        result.video,
        plan,
        series="ai-survival",
        rules_root=repo_root(),
        transcriber=None,
        episode_dir=episode,
        loudness_fn=measure_loudness,
    )
    loudness = next(item for item in report.checks if item.id == "loudness")
    assert loudness.passed
    assert loudness.measurements["integrated_lufs"] == pytest.approx(-16, abs=1)
    assert loudness.measurements["true_peak_dbtp"] <= -1.5
    assert "## loudness — PASS" in report.to_markdown()


def test_render_and_finish_accept_master_flags() -> None:
    parser = _build_parser()
    render = parser.parse_args(["render", "edit/plan.yaml", "--no-master", "--denoise"])
    assert render.no_master is True
    assert render.denoise is True
    plain = parser.parse_args(["render", "edit/plan.yaml"])
    assert plain.no_master is False
    assert plain.denoise is False
    finish = parser.parse_args(["finish", "desk", "--no-master"])
    assert finish.no_master is True
    assert finish.denoise is False
    both = parser.parse_args(["finish", "--denoise"])
    assert both.denoise is True
    assert both.no_master is False
