"""ytp motion: HyperFrames motion clips, tested against a mocked hyperframes CLI."""

from __future__ import annotations

import json
import math
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
    "title": "Tokens for one task, before and *after*",
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
    "header": "SANDWICH STAND, AI BILL",
    "lines": [
        "Reread policy binder, 40 pages",
        "Reread policy binder, again",
        "Read all 10,000 rows",
        "Instructions, again",
        "Big model, easy question",
    ],
    "total_label": "TOTAL:",
    "total": 150000,
    "unit": "TOKENS",
    "source": "Example from a major AI lab, Nov 2025",
}
SLIP = {
    "header": "SANDWICH STAND, NEW BILL",
    "lines": ["Read 5 rows"],
    "total_label": "TOTAL:",
    "total": 2000,
    "unit": "TOKENS",
    "source": "Example from a major AI lab, Nov 2025",
}
# A synthetic 608x918 strip (paper and ink bars), the size of Studio's tallest receipt.
STRIP = Path(__file__).parent / "fixtures" / "receipt-strip.png"
RECEIPT_IMAGE = {"image": str(STRIP)}
COPY = {
    "receipt-image": RECEIPT_IMAGE,
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


def test_five_templates_ship():
    assert set(list_templates()) == {
        "count-up",
        "kinetic",
        "chapter-card",
        "receipt-unroll",
        "receipt-image",
    }


@pytest.mark.parametrize(
    "name", ["count-up", "kinetic", "chapter-card", "receipt-unroll", "receipt-image"]
)
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


@pytest.mark.parametrize(
    "name", ["count-up", "kinetic", "chapter-card", "receipt-unroll", "receipt-image"]
)
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


@pytest.mark.parametrize(
    "name", ["count-up", "kinetic", "chapter-card", "receipt-unroll", "receipt-image"]
)
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
    assert cues["label"] == 0
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
        assert result.path.with_suffix(".last.png").is_file()  # hold stills for both cuts
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
        ("late_start", "frame 1 is empty"),
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


def test_scenes_plan_picks_up_motion_clips(tmp_path, mock_hf, monkeypatch):
    require_ffmpeg()
    # ytp scenes needs Inter; use the repo's copy so machines without it installed pass.
    monkeypatch.setenv("YTP_SCENE_FONT", str(repo_root() / "pipeline" / "fonts" / "Inter-Bold.ttf"))
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


# --------------------------------------------------------------------------- v1.1 review fixes


# receipt-image has no label; its frame 1 is the slot and the paper lip (tested below)
@pytest.mark.parametrize("name", ["count-up", "kinetic", "chapter-card", "receipt-unroll"])
def test_label_is_on_from_frame_one(fonts, name):
    plan = plan_motion(_job(name), "16x9", fonts)
    assert plan.payload["cues"]["label"] == 0
    script = (repo_root() / "templates" / "motion" / name / "template.js").read_text("utf-8")
    assert 'H.show("#label")' in script
    assert 'inUp("#label"' not in script
    if name == "chapter-card":
        assert 'H.show(".cc-rule")' in script and "scaleX" not in script


def test_counts_stay_hidden_until_they_run():
    house = (repo_root() / "templates" / "motion" / "_shared" / "house.js").read_text("utf-8")
    body = house.split("H.count = function", 1)[1].split("H.finish", 1)[0]
    assert "tl.set(node, { opacity: 0 }, 0)" in body
    assert "show === undefined ? t0 + 1 / Y.fps : show" in body
    script = (repo_root() / "templates" / "motion" / "count-up" / "template.js").read_text("utf-8")
    assert "c.before_show" in script and "c.after_show" in script


@pytest.mark.parametrize("aspect", ["16x9", "9x16"])
def test_no_frame_ever_shows_a_zero_count(fonts, aspect):
    """Frame-exact: walk every rendered frame time. A number is on screen from its show
    cue on, and on every one of those frames its value must already be past 0."""
    plan = plan_motion(_job("count-up"), aspect, fonts)
    cues, fps = plan.payload["cues"], plan.job.fps
    rows = {r["key"]: r["value"] for r in plan.payload["data"]["rows"]}
    for key in ("before", "after"):
        t0, t1 = cues[f"{key}_count"]
        show = cues[f"{key}_show"]
        visible = [n for n in range(plan.frames) if n / fps >= show]
        assert visible, key
        first = visible[0]
        # the cue sits strictly inside the frame gap, so float seek can't slip a frame
        assert (first - 1) / fps + 0.1 / fps < show < first / fps - 0.1 / fps
        for n in visible:
            value = motion.count_value(0, rows[key], t0, t1, n / fps, "power2.out")
            assert motion.fmt_number(value) != "0", (key, n)
        assert first / fps > t0  # never the t0 frame itself, where the value is exactly 0
    # Studio's frames: before showed 0 at f031 (t 1.00 s), after at f091 (t 3.00 s); the
    # first frames now on screen are the ones Studio read as 9,181 and 193 (GSAP cubic out)
    assert 30 / fps < cues["before_show"] < 31 / fps and 90 / fps < cues["after_show"] < 91 / fps
    assert motion.fmt_number(motion.count_value(0, 150000, 1.0, 2.6, 31 / fps, "power2.out")) == (
        "9,181"
    )


def test_count_show_catches_a_single_zero_frame():
    # a slow count whose first two frames after t0 still round to 0
    show = motion.count_show(0, 40, 1.0, 10.0, 30, 0, "power1.out")
    first = math.ceil(show * 30)
    assert motion.fmt_number(motion.count_value(0, 40, 1.0, 10.0, first / 30, "power1.out")) != "0"
    before = motion.count_value(0, 40, 1.0, 10.0, (first - 1) / 30, "power1.out")
    assert motion.fmt_number(before) == "0"


def test_receipt_amounts_wait_for_their_count(fonts):
    lines = [{"label": "Reread binder", "amount": 148000}, {"label": "Read 5 rows", "amount": 2000}]
    plan = plan_motion(_job("receipt-unroll", vars={"lines": lines}), "16x9", fonts)
    for cue, item in zip(plan.payload["cues"]["items"], plan.payload["data"]["items"]):
        t0 = cue["t"] + 0.05
        assert cue["show"] > t0
        n = math.ceil(cue["show"] * 30)
        value = motion.count_value(0, item["amount"], t0, t0 + 0.3, n / 30, "power1.out")
        assert motion.fmt_number(value) != "0"
    script = (repo_root() / "templates" / "motion" / "receipt-unroll" / "template.js").read_text(
        "utf-8"
    )
    assert "cue.show" in script and "nodes.push(amt)" not in script


def _receipt_on_screen(plan) -> None:
    d, cues, stage = plan.payload["data"], plan.payload["cues"], plan.stage
    assert "total_scroll" not in cues and "scroll" not in str(cues["items"])
    assert cues["total_paper_h"] == d["content_h"] <= d["room"] + 0.5
    for item in d["items"]:
        assert item["y"] + item["h"] <= cues["total_paper_h"]
    els = plan.payload["el"]
    above = sum(els[k]["height"] + d["gap"] for k in ("label", "title") if k in els)
    assert d["top"] + above + d["content_h"] <= stage.h - els["source"]["height"] + 0.5
    for name, el in els.items():
        if name.startswith(("item", "amount", "total", "header")):
            assert el["size"] >= 40 * stage.scale - 1e-6, name


@pytest.mark.parametrize("aspect", ["16x9", "9x16"])
@pytest.mark.parametrize("extra", [0, 1])
def test_every_receipt_line_is_on_screen_at_the_total(fonts, aspect, extra):
    lines = RECEIPT["lines"] + ["Same instructions, one more time"] * extra
    plan = plan_motion(_job("receipt-unroll", vars={"lines": lines}), aspect, fonts)
    assert plan.errors == []
    assert len(plan.payload["data"]["items"]) == 5 + extra
    _receipt_on_screen(plan)


def test_receipt_header_is_fixed_and_paper_shows_on_frame_one(fonts):
    plan = plan_motion(_job("receipt-unroll"), "16x9", fonts)
    d = plan.payload["data"]
    assert d["paper0"] > 0 and d["header_rule_y"] < d["items"][0]["y"]
    script = (repo_root() / "templates" / "motion" / "receipt-unroll" / "template.js").read_text(
        "utf-8"
    )
    assert "paper, { y" not in script  # the paper never scrolls
    assert 'vp.style.height = d.paper0 + "px"' in script


def test_receipt_that_cannot_fit_fails_instead_of_scrolling(fonts):
    lines = RECEIPT["lines"] * 3
    plan = plan_motion(_job("receipt-unroll", vars={"lines": lines}), "16x9", fonts)
    assert any("too tall to show every line" in e for e in plan.errors)


@pytest.mark.parametrize("aspect", ["16x9", "9x16"])
def test_payoff_slip_plans(fonts, aspect):
    plan = plan_motion(MotionJob("slip", "receipt-unroll", dict(SLIP)), aspect, fonts)
    assert plan.errors == []
    assert plan.payload["data"]["one_row"]
    _receipt_on_screen(plan)


def test_receipt_items_with_amounts_still_work(fonts):
    lines = [{"label": "Reread binder", "amount": 148000}, {"label": "Read 5 rows", "amount": 2000}]
    plan = plan_motion(_job("receipt-unroll", vars={"lines": lines}), "16x9", fonts)
    assert plan.errors == [] and plan.warnings == []
    assert [i["amount"] for i in plan.payload["data"]["items"]] == [148000, 2000]


@pytest.mark.parametrize(
    "text", ["$150,000", "150,000 dollars", "\u20ac2", "2,000 USD", "\u00a35", "50 cents"]
)
def test_prices_and_currency_never_pass(fonts, text):
    job = _job("receipt-unroll", vars={"lines": [text]})
    plan = plan_motion(job, "16x9", fonts, allow_placeholder=True)
    assert any("currency" in e for e in plan.errors)
    card = _job("count-up", vars={"unit": "dollars"})
    assert any("currency" in e for e in plan_motion(card, "16x9", fonts).errors)


@pytest.mark.parametrize(
    "name", ["count-up", "kinetic", "chapter-card", "receipt-unroll", "receipt-image"]
)
def test_templates_never_add_currency(fonts, name):
    folder = repo_root() / "templates" / "motion"
    for path in [*(folder / name).iterdir(), *(folder / "_shared").iterdir()]:
        source = path.read_text(encoding="utf-8")
        strings = re.findall(r'"[^"\n]*"|\'[^\'\n]*\'', source)
        for literal in strings:
            assert not motion.CURRENCY_PATTERN.search(literal), (path.name, literal)
    for aspect in ("16x9", "9x16"):
        payload = plan_motion(_job(name), aspect, fonts).payload
        shown = json.dumps(payload["el"], ensure_ascii=False)
        assert not motion.CURRENCY_PATTERN.search(shown)
    assert motion.fmt_number(150000) == "150,000"


def test_chapter_cards_share_one_title_size(tmp_path, fonts):
    from ytp import scenes

    long = "Step three: Give right-answer work to *code*"
    spec_path = _write_spec(
        tmp_path / "ep",
        [
            _motion_frame("a", "chapter-card", start=0),
            {**_motion_frame("b", "chapter-card", start=5), "vars": {"title": long}},
        ],
    )
    spec = scenes.load_spec(spec_path)
    caps = motion.set_caps(spec, fonts)
    sizes = {}
    for frame in spec.frames:
        for aspect in ("16x9", "9x16"):
            plan = plan_motion(motion.job_for(frame, spec, caps), aspect, fonts)
            assert plan.errors == []
            sizes.setdefault(aspect, set()).add(plan.payload["el"]["title"]["size"])
    assert all(len(v) == 1 for v in sizes.values()), sizes
    alone = plan_motion(motion.job_for(spec.frames[0], spec), "16x9", fonts)
    assert alone.payload["el"]["title"]["size"] >= next(iter(sizes["16x9"]))


def test_label_wraps_are_balanced(fonts):
    plan = plan_motion(_job("count-up"), "9x16", fonts)
    for name in ("before_label", "after_label"):
        lines = plan.payload["el"][name]["lines"]
        assert all(len(line) >= 2 for line in lines), (name, lines)


def test_kinetic_holds_on_a_strong_closing_line(fonts):
    for aspect, need in (("16x9", 60), ("9x16", 84)):
        plan = plan_motion(_job("kinetic"), aspect, fonts)
        sub = plan.payload["el"]["subline"]
        assert sub["face"] == "display" and sub["size"] >= need


def test_written_duration_never_rounds_up_a_frame():
    for fps in (24, 25, 30, 60):
        for frames in range(1, 2000):
            written = motion.render_duration(frames, fps)
            assert math.ceil(written * fps) == frames == round(written * fps), (fps, frames)


# --------------------------------------------------------------------------- receipt-image


def _strip(path: Path, w: int, h: int) -> Path:
    from PIL import Image

    Image.new("RGBA", (w, h), (251, 247, 236, 255)).save(path)
    return path


def test_receipt_image_is_one_to_one_in_the_16x9_slice(fonts):
    plan = plan_motion(_job("receipt-image"), "16x9", fonts)
    assert plan.errors == [] and plan.warnings == []
    d = plan.payload["data"]
    assert d["image_size"] == [608, 918] and d["scale"] == 1
    assert (d["x"], d["w"], d["h"]) == (656, 608, 918)  # exactly the slice x 656-1264
    assert d["y"] == 60 and d["y"] + d["h"] <= 1080 - 60
    assert d["slot_x"] >= motion.SAFE_SLICE[0] and d["slot_x"] + d["slot_w"] <= 1264
    assert 0 < d["lip"] <= 31  # frame 1: the slot plus blank paper, above the first ink


def test_receipt_image_fits_the_9x16_safe_area(fonts):
    plan = plan_motion(_job("receipt-image"), "9x16", fonts)
    assert plan.errors == []
    d, st = plan.payload["data"], plan.stage
    assert d["w"] <= st.w + 1e-6 and d["x"] >= st.x - 1e-6 and d["x"] + d["w"] <= st.x + st.w
    assert d["y"] >= st.y and d["y"] + d["h"] <= st.y + st.h + 1e-6
    assert d["scale"] == pytest.approx(st.w / 608, abs=1e-3)
    # 40px receipt type lands at 56px or more in 9:16
    assert 40 * d["scale"] >= 56


def test_receipt_image_print_and_hold_timing(fonts):
    plan = plan_motion(_job("receipt-image"), "16x9", fonts)
    assert plan.payload["cues"]["print"] == 3.0 and plan.frames == 150
    quick = plan_motion(_job("receipt-image", vars={"print": 2.5, "hold": 1}), "16x9", fonts)
    assert quick.payload["cues"]["print"] == 2.5 and quick.frames == 105
    bad = plan_motion(_job("receipt-image", vars={"print": 0.1}), "16x9", fonts)
    assert any("out of range" in e for e in bad.errors)
    script = (repo_root() / "templates" / "motion" / "receipt-image" / "template.js").read_text(
        "utf-8"
    )
    assert "tl.set(vp, { height: d.lip }, 0)" in script  # paper on screen from frame 1
    assert 'ease: "none"' in script  # steady feed


def test_receipt_image_never_grows_past_the_slice(tmp_path, fonts):
    wide = _strip(tmp_path / "wide.png", 800, 1200)
    plan = plan_motion(_job("receipt-image", vars={"image": str(wide)}), "16x9", fonts)
    d = plan.payload["data"]
    assert d["w"] <= 608 and d["h"] <= 960 and any("shrunk" in w for w in plan.warnings)
    small = _strip(tmp_path / "small.png", 400, 300)
    plan = plan_motion(_job("receipt-image", vars={"image": str(small)}), "16x9", fonts)
    assert plan.payload["data"]["scale"] == 1  # 16:9 never enlarges the art


def test_receipt_image_missing_or_not_png(tmp_path, fonts):
    plan = plan_motion(
        _job("receipt-image", vars={"image": str(tmp_path / "no.png")}), "16x9", fonts
    )
    assert any("not found" in e for e in plan.errors)
    fake = tmp_path / "fake.png"
    fake.write_text("not a png", encoding="utf-8")
    plan = plan_motion(_job("receipt-image", vars={"image": str(fake)}), "16x9", fonts)
    assert any("not a PNG" in e for e in plan.errors)


def test_receipt_image_path_is_not_screen_copy(tmp_path, fonts):
    # The path has digits but never shows, so no source line is needed; lint is unchanged.
    art = tmp_path / "Ep02 v4" / "R5-final 2,000.png"
    art.parent.mkdir()
    shutil.copy(STRIP, art)
    plan = plan_motion(_job("receipt-image", vars={"image": str(art)}), "16x9", fonts)
    assert plan.errors == []


def test_receipt_image_renders_from_the_episode_folder(tmp_path, mock_hf):
    require_ffmpeg()
    episode = tmp_path / "ep"
    (episode / "art").mkdir(parents=True)
    shutil.copy(STRIP, episode / "art" / "R5-final.png")
    frame = {
        "id": "r5",
        "kind": "motion",
        "template": "receipt-image",
        "start": 0,
        "vars": {"image": "art/R5-final.png", "print": 3.0},
    }
    run = run_motion(_write_spec(episode, [frame]), aspect="both")
    assert {r.aspect for r in run.results} == {"16x9", "9x16"}
    for result in run.results:
        assert result.path.with_suffix(".last.png").is_file()
    project = episode / ".ytp-motion" / "r5-16x9"
    assert (project / "assets" / "receipt.png").read_bytes() == STRIP.read_bytes()
    assert 'src="assets/receipt.png"' in (project / "index.html").read_text(encoding="utf-8")
    plan = json.loads((project / "ytp-plan.json").read_text(encoding="utf-8"))
    assert len(plan["data"]["image_sha256"]) == 64  # new art, new hash, new render
