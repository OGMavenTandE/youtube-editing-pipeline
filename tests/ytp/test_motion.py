"""ytp motion: HyperFrames motion clips, tested against a mocked hyperframes CLI."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

pytest.importorskip("PIL", reason="ytp motion measures type with Pillow")

from tests.ytp.mediautil import require_ffmpeg  # noqa: E402
from ytp import motion  # noqa: E402
from ytp.cli import main  # noqa: E402
from ytp.motion import (  # noqa: E402
    FACES,
    ROLE_MIN,
    SAFE_SLICE,
    FontBook,
    MotionJob,
    MotionLintError,
    hyperframes_command,
    list_templates,
    load_manifest,
    plan_motion,
    run_motion,
    stage_for,
)
from ytp.plan import EditPlan, NarrationLine, Segment, load_plan, save_plan, validate_plan  # noqa: E402
from ytp.rules import repo_root  # noqa: E402

TEST_FONT = repo_root() / "pipeline" / "fonts" / "Inter-Bold.ttf"
MOCK = Path(__file__).parent / "fixtures" / "mock_hyperframes.py"

# Ep02 copy from the Galley (motion-copy-galley-v1.md).
COUNT_UP = {
    "label": "ONE TASK, BEFORE AND AFTER",
    "title": "Tokens for one task, *before* and after",
    "before_label": "BEFORE: THE MODEL READS ALL 10,000 ROWS",
    "after_label": "AFTER: CODE FILTERS, THE MODEL READS 5",
    "before": 150000,
    "after": 2000,
    "unit": "tokens",
    "tag": "98.7% fewer tokens",
    "source": "Example from a major AI lab, Nov 2025",
}
KINETIC = {
    "label": "THE PLAN",
    "lines": [
        "Find the waste.",
        "Trim what it reads.",
        "Move right-answer work into code.",
        "Right-size the model.",
    ],
    "amber_word": "code",
    "subline": "Then test it before it replaces anything.",
}
CHAPTER = {"title": "Step one: *Find* the waste"}
RECEIPT = {
    "label": "THE BILL",
    "title": "One order, *every* page",
    "lines": [
        {"label": "REREAD THE POLICY BINDER", "amount": 96000},
        {"label": "REREAD THE CHAT", "amount": 52000},
        {"label": "THE ORDER", "amount": 2000},
    ],
    "total_label": "TOTAL",
    "total": 150000,
    "unit": "tokens",
    "source": "Illustration, one order",
}
COPY = {
    "count-up": COUNT_UP,
    "kinetic": KINETIC,
    "chapter-card": CHAPTER,
    "receipt-unroll": RECEIPT,
}


@pytest.fixture
def fonts(tmp_path, monkeypatch) -> FontBook:
    folder = tmp_path / "fonts"
    folder.mkdir()
    for _family, name, _italic in FACES.values():
        shutil.copy(TEST_FONT, folder / name)
    monkeypatch.setenv("YTP_MOTION_FONTS", str(folder))
    return FontBook(folder)


@pytest.fixture
def mock_hf(tmp_path, monkeypatch, fonts) -> Path:
    log = tmp_path / "hf-calls.jsonl"
    gsap = tmp_path / "gsap.min.js"
    gsap.write_text("/* gsap stub */\n", encoding="utf-8")
    monkeypatch.setenv("YTP_HYPERFRAMES", str(MOCK))
    monkeypatch.setenv("YTP_GSAP", str(gsap))
    monkeypatch.setenv("MOCK_HF_LOG", str(log))
    for key in ("MOCK_HF_FAIL_GPU", "MOCK_HF_FAIL_LINT", "MOCK_HF_MODE"):
        monkeypatch.delenv(key, raising=False)
    # A caller with telemetry on must not leak it into HyperFrames.
    monkeypatch.setenv("HYPERFRAMES_NO_TELEMETRY", "0")
    motion.hyperframes_version.cache_clear()
    return log


def _job(template: str, **extra) -> MotionJob:
    values = dict(COPY[template])
    values.update(extra.pop("vars", {}))
    return MotionJob(
        id=extra.pop("id", "m1"),
        template=template,
        vars=values,
        series="AI Survival",
        episode=2,
        **extra,
    )


def _calls(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _spec(frames: list[dict], **top) -> dict:
    data = {"version": 1, "series": "AI Survival", "episode": 2, "fps": 30, "frames": frames}
    data.update(top)
    return data


# --------------------------------------------------------------------------- templates


def test_four_templates_ship():
    assert set(list_templates()) == {"count-up", "kinetic", "chapter-card", "receipt-unroll"}


@pytest.mark.parametrize("name", ["count-up", "kinetic", "chapter-card", "receipt-unroll"])
def test_templates_carry_no_copy_or_cdn(name):
    folder = repo_root() / "templates" / "motion" / name
    html = (folder / "index.html").read_text(encoding="utf-8")
    body = html.split("<body>", 1)[1]
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    text = re.sub(r"<[^>]+>|\{\{[A-Z_]+\}\}", "", body)
    assert text.strip() == ""
    manifest = load_manifest(name)
    assert all("default" not in rule for rule in manifest["vars"].values())
    for path in [*folder.iterdir(), *(folder.parent / "_shared").iterdir()]:
        source = path.read_text(encoding="utf-8")
        assert "http://" not in source.replace("http://www.w3.org", "")
        assert "https://" not in source
        assert not motion.VENDOR_PATTERN.search(source), path


@pytest.mark.parametrize("name", ["count-up", "kinetic", "chapter-card", "receipt-unroll"])
def test_template_type_meets_minimums(name):
    for role in ("label", "source", "subline", "body", "headline"):
        assert ROLE_MIN[role] >= {"label": 26, "source": 24, "subline": 40, "body": 40}.get(role, 0)
    for spec in load_manifest(name)["elements"].values():
        size = spec["size"]
        low = size if isinstance(size, (int, float)) else size[0]
        assert low >= ROLE_MIN[spec["role"]], spec
        if spec["role"] == "label":
            assert spec["tracking"] > 0
        if spec["role"] == "source":
            assert spec["opacity"] >= 0.7


# --------------------------------------------------------------------------- planning and lint


@pytest.mark.parametrize("name", ["count-up", "kinetic", "chapter-card", "receipt-unroll"])
@pytest.mark.parametrize("aspect", ["16x9", "9x16"])
def test_galley_copy_plans_clean(fonts, name, aspect):
    plan = plan_motion(_job(name), aspect, fonts)
    assert plan.errors == []
    stage = plan.stage
    assert (stage.width, stage.height) == ((1920, 1080) if aspect == "16x9" else (1080, 1920))
    scale = 1.4 if aspect == "9x16" else 1.0
    for element in plan.payload["el"].values():
        assert element["size"] >= ROLE_MIN[element["role"]] * scale - 1e-6
        assert element["width"] <= stage.w
    assert plan.frames == round(plan.duration * 30)


def test_safe_layout_stays_in_the_shorts_slice():
    safe = stage_for("16x9", "safe")
    assert SAFE_SLICE[0] <= safe.x and safe.x + safe.w <= SAFE_SLICE[1]
    wide = stage_for("16x9", "wide")
    assert wide.w > safe.w
    portrait = stage_for("9x16")
    assert (portrait.width, portrait.height, portrait.scale) == (1080, 1920, 1.4)


def test_portrait_type_is_bigger_not_a_crop(fonts):
    land = plan_motion(_job("chapter-card"), "16x9", fonts).payload["el"]
    tall = plan_motion(_job("chapter-card"), "9x16", fonts).payload["el"]
    assert tall["label"]["size"] >= land["label"]["size"] * 1.39
    assert tall["title"]["size"] > land["title"]["size"]


def test_count_up_after_row_waits_for_the_before_number(fonts):
    plan = plan_motion(_job("count-up"), "16x9", fonts)
    cues = plan.payload["cues"]
    assert cues["label"] <= 6 / 30
    assert cues["after_row"] >= cues["before_count"][1]
    assert cues["after_count"][0] > cues["after_row"]
    land = cues["tag"] + 0.35
    assert plan.duration - land >= 1.5 - 1e-6
    rows = plan.payload["data"]["rows"]
    assert rows[0]["pct"] == 100 and rows[1]["pct"] < 5
    assert plan.payload["colors"]["slate"] != plan.payload["colors"]["amber"]


def test_kinetic_one_line_at_a_time(fonts):
    plan = plan_motion(_job("kinetic"), "16x9", fonts)
    cues = plan.payload["cues"]
    beats = cues["beats"]
    assert len(beats) == 5
    assert round(cues["exit"] * 30) == 7 and round(cues["enter"] * 30) == 10
    for before, after in zip(beats, beats[1:]):
        assert before["out"] is not None
        gap = after["in"] - (before["out"] + cues["exit"])
        assert round(gap * 30) == 2
    assert beats[-1]["out"] is None
    assert beats[0]["in"] <= 6 / 30
    words = plan.payload["el"]["subline"]["words"]
    hold = plan.duration - (beats[-1]["in"] + cues["enter"])
    assert hold >= max(1.5, 0.3 * words) - 1e-3
    line = plan.payload["el"]["line2"]["lines"]
    amber = [run[0] for row in line for run in row if run[1]]
    assert amber == ["code"]
    assert any(run[0] == "." and run[2] for row in line for run in row)


def test_kinetic_amber_word_must_be_in_a_line(fonts):
    plan = plan_motion(_job("kinetic", vars={"amber_word": "pickles"}), "16x9", fonts)
    assert any("amber_word" in e for e in plan.errors)


def test_chapter_label_comes_from_the_scenes_file(fonts):
    plan = plan_motion(_job("chapter-card"), "16x9", fonts)
    label = plan.payload["el"]["label"]["lines"][0]
    assert " ".join(run[0] for run in label) == "AI SURVIVAL \u00b7 EP 02"
    job = _job("chapter-card")
    job.series, job.episode = None, None
    assert any("series and episode" in e for e in plan_motion(job, "16x9", fonts).errors)


def test_source_required_with_figures(fonts):
    values = dict(COUNT_UP)
    del values["source"]
    plan = plan_motion(MotionJob("m", "count-up", values), "16x9", fonts)
    assert any("'source' is required" in e for e in plan.errors)
    card = _job("chapter-card", vars={"title": "*Step* 4 saves 98%"})
    assert any("source line" in e for e in plan_motion(card, "16x9", fonts).errors)


def test_required_vars_have_no_defaults(fonts):
    plan = plan_motion(MotionJob("m", "kinetic", {"lines": ["One."]}), "16x9", fonts)
    for name in ("label", "amber_word", "subline"):
        assert any(f"'{name}' is required" in e for e in plan.errors)


@pytest.mark.parametrize(
    "text", ["[PLACEHOLDER] headline", "TODO", "Short prompts. Fresh chats. Smaller bills."]
)
def test_placeholder_copy_fails_unless_allowed(fonts, text):
    job = _job("kinetic", vars={"subline": text})
    assert any("placeholder" in e for e in plan_motion(job, "16x9", fonts).errors)
    allowed = plan_motion(job, "16x9", fonts, allow_placeholder=True)
    assert allowed.errors == [] and allowed.warnings


def test_vendor_names_fail(fonts):
    job = _job("count-up", vars={"source": "Example from OpenAI, Nov 2025"})
    assert any("vendor" in e for e in plan_motion(job, "16x9", fonts).errors)


def test_headline_needs_exactly_one_amber_word(fonts):
    two = _job("count-up", vars={"title": "*Tokens* for one task, *before* and after"})
    assert any("exactly one" in e for e in plan_motion(two, "16x9", fonts).errors)
    none = _job("count-up", vars={"title": "Tokens for one task"})
    assert any("exactly one" in e for e in plan_motion(none, "16x9", fonts).errors)
    card = plan_motion(_job("chapter-card", vars={"title": "How the bill works"}), "16x9", fonts)
    assert card.errors == [] and any("amber" in w for w in card.warnings)


def test_copy_that_cannot_fit_fails(fonts):
    long = "Move " + " ".join(["right-answer work into code"] * 6)
    plan = plan_motion(_job("chapter-card", vars={"title": f"*{long}*"}), "16x9", fonts)
    assert any("does not fit" in e for e in plan.errors)


def test_clip_duration_cannot_cut_the_hold(fonts):
    plan = plan_motion(_job("chapter-card", clip_duration=1.0), "16x9", fonts)
    assert any("clip_duration" in e for e in plan.errors)
    longer = plan_motion(_job("chapter-card", clip_duration=6.0), "16x9", fonts)
    assert longer.errors == [] and longer.duration == 6.0


# --------------------------------------------------------------------------- tool lookup


def test_hyperframes_lookup(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("YTP_HYPERFRAMES", str(MOCK))
    assert hyperframes_command() == [sys.executable, str(MOCK)]
    install = tmp_path / "hyperframes"
    mjs = install / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
    mjs.parent.mkdir(parents=True)
    mjs.write_text("", encoding="utf-8")
    cmd = install / "node_modules" / ".bin" / "hyperframes.cmd"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("", encoding="utf-8")
    monkeypatch.setenv("YTP_NODE", "/opt/node/bin/node")
    assert hyperframes_command({"hyperframes": str(install)}) == ["/opt/node/bin/node", str(mjs)]
    monkeypatch.setenv("YTP_HYPERFRAMES", str(cmd))
    assert hyperframes_command() == ["/opt/node/bin/node", str(mjs)]
    monkeypatch.setenv("YTP_HYPERFRAMES", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        hyperframes_command()


def test_missing_fonts_say_where_to_get_them(tmp_path, monkeypatch):
    monkeypatch.setenv("YTP_MOTION_FONTS", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="Fraunces"):
        motion.find_fonts_dir()


# --------------------------------------------------------------------------- rendering


def _write_spec(episode: Path, frames: list[dict], **top) -> Path:
    path = episode / "edit" / "scenes.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_spec(frames, **top), sort_keys=False), encoding="utf-8")
    return path


def _motion_frame(frame_id: str, template: str, **extra) -> dict:
    return {"id": frame_id, "kind": "motion", "template": template, "vars": COPY[template], **extra}


def test_render_both_aspects_cache_and_env(tmp_path, mock_hf):
    require_ffmpeg()
    episode = tmp_path / "ep"
    spec = _write_spec(episode, [_motion_frame("card", "chapter-card", start=0)])
    run = run_motion(spec, aspect="both")
    assert {r.aspect for r in run.results} == {"16x9", "9x16"}
    for result in run.results:
        assert result.path.is_file() and result.path.parent == episode / "clips" / "motion"
        meta = json.loads(result.path.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["frames"] == result.frames
    calls = _calls(mock_hf)
    renders = [c for c in calls if c["argv"][0] == "render"]
    lints = [c for c in calls if c["argv"][0] == "lint"]
    assert len(renders) == 2 and len(lints) == 2
    assert all("--gpu" in c["argv"] for c in renders)
    for call in calls:
        assert call["env"]["HYPERFRAMES_NO_TELEMETRY"] == "1"
        assert call["env"]["DO_NOT_TRACK"] == "1"
        assert call["env"]["HYPERFRAMES_NO_UPDATE_CHECK"] == "1"
    project = episode / ".ytp-motion" / "card-16x9"
    html = (project / "index.html").read_text(encoding="utf-8")
    assert 'data-composition-id="ytp-motion"' in html and "{{" not in html
    assert (project / "assets" / "gsap.min.js").is_file()
    assert "HYPERFRAMES_NO_TELEMETRY=1" in (project / ".env").read_text(encoding="utf-8")
    fragment = yaml.safe_load(run.fragment_path.read_text(encoding="utf-8"))
    assert [s["id"] for s in fragment["segments"]] == ["scene-card"]
    assert fragment["segments"][0]["kind"] == "clip"
    # Second run is cached: no new renders.
    again = run_motion(spec, aspect="both")
    assert all(r.cached for r in again.results)
    assert len([c for c in _calls(mock_hf) if c["argv"][0] == "render"]) == 2
    # Changing the copy changes the hash and renders again.
    frame = _motion_frame("card", "chapter-card", start=0)
    frame["vars"] = {"title": "Step two: *Trim* what it reads"}
    spec = _write_spec(episode, [frame])
    third = run_motion(spec, aspect="16x9")
    assert not third.results[0].cached
    assert third.results[0].path != run.results[0].path or not run.results[0].path.exists()
    assert len(list((episode / "clips" / "motion").glob("card-16x9-*.mp4"))) == 1


def test_gpu_failure_falls_back_to_x264(tmp_path, mock_hf, monkeypatch):
    require_ffmpeg()
    monkeypatch.setenv("MOCK_HF_FAIL_GPU", "1")
    spec = _write_spec(tmp_path / "ep", [_motion_frame("card", "chapter-card", start=0)])
    run = run_motion(spec)
    assert run.results[0].encoder == "libx264"
    renders = [c["argv"] for c in _calls(mock_hf) if c["argv"][0] == "render"]
    assert ["--gpu" in argv for argv in renders] == [True, False]


def test_hyperframes_lint_failure_stops_the_render(tmp_path, mock_hf, monkeypatch):
    monkeypatch.setenv("MOCK_HF_FAIL_LINT", "1")
    spec = _write_spec(tmp_path / "ep", [_motion_frame("card", "chapter-card", start=0)])
    with pytest.raises(motion.MotionError, match="hyperframes lint failed"):
        run_motion(spec)
    assert not [c for c in _calls(mock_hf) if c["argv"][0] == "render"]


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("black_start", "first frame is black"),
        ("black_end", "last frame is black"),
        ("fade_end", "fades"),
        ("outside", "safe slice"),
    ],
)
def test_frame_checks_catch_bad_renders(tmp_path, mock_hf, monkeypatch, mode, message):
    require_ffmpeg()
    monkeypatch.setenv("MOCK_HF_MODE", mode)
    spec = _write_spec(tmp_path / "ep", [_motion_frame("card", "chapter-card", start=0)])
    with pytest.raises(MotionLintError, match=message):
        run_motion(spec)
    assert (
        not list((tmp_path / "ep" / "clips" / "motion").glob("*.mp4"))
        if (tmp_path / "ep" / "clips" / "motion").exists()
        else True
    )


def test_lint_only_renders_nothing(tmp_path, mock_hf):
    spec = _write_spec(tmp_path / "ep", [_motion_frame("k", "kinetic", start=0)])
    assert main(["motion", str(spec), "--lint-only", "--aspect", "both"]) == 0
    calls = [c["argv"][0] for c in _calls(mock_hf)]
    assert calls.count("lint") == 2 and "render" not in calls


def test_cli_fails_on_placeholder_copy(tmp_path, mock_hf, capsys):
    frame = _motion_frame("k", "kinetic", start=0)
    frame["vars"] = {**KINETIC, "subline": "[PLACEHOLDER] subline"}
    spec = _write_spec(tmp_path / "ep", [frame])
    assert main(["motion", str(spec), "--lint-only"]) == 2
    assert "placeholder" in capsys.readouterr().err
    assert main(["motion", str(spec), "--lint-only", "--allow-placeholder"]) == 0


# --------------------------------------------------------------------------- plan integration


def test_scenes_plan_picks_up_motion_clips(tmp_path, mock_hf):
    require_ffmpeg()
    episode = tmp_path / "ep"
    plan_path = episode / "edit" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan = EditPlan(
        series="ai-survival",
        narration=[NarrationLine("n1", "Hello.", 0.0, 6.0)],
        segments=[Segment("talk", "clip", "take.mp4", 6, 2)],
    )
    save_plan(plan, plan_path)
    spec = _write_spec(episode, [_motion_frame("card", "chapter-card", line="n1")])
    assert main(["scenes", str(spec), "--plan"]) == 0
    merged = load_plan(plan_path)
    ids = [s.id for s in merged.segments]
    assert ids == ["scene-card", "scene-card-hold", "talk"]
    clip, hold = merged.segments[0], merged.segments[1]
    assert clip.kind == "clip" and clip.source.startswith("clips/motion/card-16x9-")
    assert clip.template is None
    assert hold.kind == "still" and round(clip.duration + hold.duration, 3) == 6.0
    assert validate_plan(merged) == []
    # --no-motion reuses the cached clip instead of rendering.
    before = len(_calls(mock_hf))
    assert main(["scenes", str(spec), "--plan", "--no-motion"]) == 0
    assert [c["argv"][0] for c in _calls(mock_hf)[before:]] == ["--version"] or len(
        [c for c in _calls(mock_hf)[before:] if c["argv"][0] == "render"]
    ) == 0


def test_motion_plan_merge_keeps_other_scene_segments(tmp_path, mock_hf):
    require_ffmpeg()
    episode = tmp_path / "ep"
    plan_path = episode / "edit" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    save_plan(
        EditPlan(
            series="ai-survival",
            segments=[
                Segment("scene-still", "still", "frames/still.png", 0, 2),
                Segment("scene-card", "still", "frames/old.png", 2, 1),
            ],
        ),
        plan_path,
    )
    spec = _write_spec(
        episode,
        [
            {"id": "still", "kind": "card", "big": "HI", "start": 0},
            _motion_frame("card", "chapter-card", start=2),
        ],
    )
    assert main(["motion", str(spec), "--plan"]) == 0
    merged = load_plan(plan_path)
    assert [s.id for s in merged.segments] == ["scene-still", "scene-card"]
    assert merged.segments[1].kind == "clip"


def test_short_slot_cannot_cut_the_motion_hold(tmp_path, mock_hf):
    require_ffmpeg()
    spec = _write_spec(
        tmp_path / "ep",
        [
            _motion_frame("card", "chapter-card", start=0),
            {"id": "next", "kind": "card", "big": "HI", "start": 1},
        ],
    )
    assert main(["scenes", str(spec)]) == 2
