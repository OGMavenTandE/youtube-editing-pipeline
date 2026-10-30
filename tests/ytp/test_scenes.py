"""ytp scenes: frames from a scene spec. Generated test images only, no episode art."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytest.importorskip("PIL", reason="ytp scenes needs Pillow")

from PIL import Image, ImageDraw  # noqa: E402

from tests.ytp.mediautil import require_ffmpeg
from ytp.cli import main
from ytp.plan import EditPlan, NarrationLine, Segment, load_plan, save_plan, validate_plan
from ytp.render import _encode_segment, motion_filter
from ytp.rules import repo_root
from ytp.scenes import (
    Count,
    Fonts,
    SceneRenderer,
    count_texts,
    find_font,
    frame_timing,
    parse_zoom,
    run_scenes,
    safe_zone,
    spec_from_dict,
)

TEST_FONT = repo_root() / "pipeline" / "fonts" / "Inter-Bold.ttf"
BRIGHT_BIG = (0xFF, 0xD2, 0x3F)
DARK_BIG = (0xFF, 0x8A, 0x2B)
PLATE = (40, 90, 160)


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


@pytest.fixture
def episode(tmp_path, monkeypatch):
    monkeypatch.setenv("YTP_SCENE_FONT", str(TEST_FONT))
    monkeypatch.delenv("YTP_SCENE_ASSETS", raising=False)
    root = tmp_path / "ep"
    art = root / "art"
    art.mkdir(parents=True)
    Image.new("RGB", (1600, 900), PLATE).save(art / "plate.png")
    framed = Image.new("RGB", (1000, 600), "white")
    ImageDraw.Draw(framed).rectangle((100, 100, 899, 499), fill=(200, 30, 30))
    framed.save(art / "framed.png")
    prop = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    ImageDraw.Draw(prop).rectangle((0, 0, 99, 99), fill=(0, 200, 0, 255))
    prop.save(art / "prop.png")
    (root / "edit").mkdir()
    return root


def _spec(frames, **extra):
    data = {
        "version": 1,
        "asset_dirs": ["art"],
        "assets": {"plate": "plate.png", "framed": "framed.png", "prop": "prop.png"},
        "frames": frames,
    }
    data.update(extra)
    return data


def _renderer(episode: Path, data: dict) -> tuple[SceneRenderer, object]:
    spec = spec_from_dict(data, episode / "edit" / "scenes.yaml")
    return SceneRenderer(spec, episode, Fonts(TEST_FONT)), spec


def _count(image: Image.Image, box, color, tol: int = 6) -> int:
    crop = image.crop(tuple(int(v) for v in box)).convert("RGB")
    hits = 0
    for pixel in crop.getdata():
        if all(abs(a - b) <= tol for a, b in zip(pixel, color)):
            hits += 1
    return hits


def test_frame_is_1920x1080_rgb_with_plate_and_bright_big_word(episode):
    renderer, spec = _renderer(
        episode, _spec([{"id": "a", "art": "plate", "big": "FREEZE", "caption": "Freeze."}])
    )
    out = renderer.render(spec.frames[0])
    assert out.image.size == (1920, 1080)
    assert out.image.mode == "RGB"
    assert out.image.getpixel((5, 500)) == PLATE
    big = out.boxes["big"]
    assert abs((big[0] + big[2]) / 2 - 960) < 3
    assert big[1] < 150 < big[3]
    assert _count(out.image, big, BRIGHT_BIG) > 500


def test_dark_palette_and_accent_override(episode):
    frames = [
        {"id": "d", "art": "plate", "palette": "dark", "big": "10"},
        {"id": "r", "art": "plate", "big": "10", "accent": "#E63946"},
    ]
    renderer, spec = _renderer(episode, _spec(frames))
    dark = renderer.render(spec.frames[0])
    red = renderer.render(spec.frames[1])
    assert _count(dark.image, dark.boxes["big"], DARK_BIG) > 500
    assert _count(dark.image, dark.boxes["big"], BRIGHT_BIG) == 0
    assert _count(red.image, red.boxes["big"], (0xE6, 0x39, 0x46)) > 500


def test_caption_pill_label_and_source_positions(episode):
    frame = {
        "id": "c",
        "art": "plate",
        "big": "94%",
        "caption": "PERSON, ninety-four percent sure.",
        "labels": [
            {"text": "PERSON 94%", "x": 900, "y": 300},
            {"text": "HAT 80%", "box": [1180, 430, 1560, 530], "size": 60},
        ],
        "source": "Source: test data",
    }
    renderer, spec = _renderer(episode, _spec([frame]))
    out = renderer.render(spec.frames[0])
    cap = out.boxes["caption"]
    assert abs((cap[0] + cap[2]) / 2 - 960) < 1
    assert abs((cap[1] + cap[3]) / 2 - 990) < 1
    assert cap[3] - cap[1] == int(64 * 1.6)
    assert out.image.getpixel((int(cap[0]) + 30, 990)) == (0, 0, 0)
    auto, fixed = out.boxes["labels"]
    assert auto[0] == 900 and auto[1] == 300 and auto[3] == 390
    assert fixed == (1180, 430, 1560, 530)
    assert out.image.getpixel((1200, 480)) == (0xE6, 0x39, 0x46)
    source = out.boxes["source"]
    assert source[2] <= 1920 - 30 and source[3] <= 1080 - 10
    assert source[0] > 1500 and source[1] > 1000


def test_trim_plate_cuts_white_border(episode):
    data = _spec([{"id": "t", "art": "framed"}], plate={"trim": True})
    renderer, spec = _renderer(episode, data)
    image = renderer.render(spec.frames[0]).image
    assert image.getpixel((2, 2)) == (200, 30, 30)
    plain, spec2 = _renderer(episode, _spec([{"id": "t", "art": "framed"}]))
    assert plain.render(spec2.frames[0]).image.getpixel((2, 2)) == (255, 255, 255)


def test_layers_keep_alpha_flip_and_z_order(episode):
    frame = {
        "id": "l",
        "art": "plate",
        "layers": [
            {"asset": "prop", "x": 400, "y": 400, "z": 1},
            {"asset": "prop", "x": 1000, "y": 400, "flip": True, "scale": 2},
        ],
    }
    renderer, spec = _renderer(episode, _spec([frame]))
    out = renderer.render(spec.frames[0])
    image = out.image
    # first layer: opaque half on the left of its box, transparent half shows the plate
    assert image.getpixel((330, 400)) == (0, 200, 0)
    assert image.getpixel((470, 400)) == PLATE
    # flipped and doubled: opaque half moves to the right
    assert image.getpixel((1150, 400)) == (0, 200, 0)
    assert image.getpixel((850, 400)) == PLATE
    boxes = [box for _asset, _role, box in out.boxes["layers"]]
    assert boxes[0] == (800, 300, 1200, 500)  # z 0 drawn first
    assert boxes[1] == (300, 350, 500, 450)


def test_safe_zone_fit_and_warnings(episode):
    assert safe_zone(1920, 1080) == (656, 0, 1264, 1080)
    frames = [
        {"id": "fit", "art": "plate", "big": "CONCEALMENT"},
        {"id": "moved", "art": "plate", "big": "TIP 3", "big_x": 420},
        {
            "id": "host",
            "art": "plate",
            "layers": [
                {"asset": "prop", "x": 200, "y": 500, "role": "host"},
                {"asset": "prop", "x": 1700, "y": 500},
            ],
        },
    ]
    renderer, spec = _renderer(episode, _spec(frames))
    fit = renderer.render(spec.frames[0])
    x1, _y1, x2, _y2 = fit.boxes["big"]
    assert 656 <= x1 and x2 <= 1264
    assert fit.boxes["big_size"] < 160
    assert fit.warnings == []
    moved = renderer.render(spec.frames[1])
    assert any("big word sits outside" in w for w in moved.warnings)
    host = renderer.render(spec.frames[2])
    assert len(host.warnings) == 1 and "host layer prop" in host.warnings[0]
    guides = renderer.guides(fit.image)
    assert guides.getpixel((657, 540)) == (0, 255, 255)


def test_fixed_fit_keeps_ep01_sizes(episode):
    data = _spec(
        [{"id": "f", "art": "plate", "big": "CONCEALMENT"}], layout={"big": {"fit": "fixed"}}
    )
    renderer, spec = _renderer(episode, data)
    assert renderer.render(spec.frames[0]).boxes["big_size"] == 160


def test_strikethrough_and_was_line(episode):
    frames = [
        {"id": "s", "art": "plate", "big": "150,000", "strike": True},
        {"id": "w", "art": "plate", "big": "2,000", "was": "150,000"},
    ]
    renderer, spec = _renderer(episode, _spec(frames))
    struck = renderer.render(spec.frames[0])
    box = struck.boxes["big"]
    mid = (box[1] + box[3]) / 2
    band = (box[0], mid - 3, box[2], mid + 3)
    assert _count(struck.image, band, (0xE6, 0x39, 0x46)) > 200
    was = renderer.render(spec.frames[1])
    assert was.boxes["was"][3] < was.boxes["big"][1]


def test_card_kind_uses_palette_background(episode):
    renderer, spec = _renderer(episode, _spec([{"id": "k", "kind": "card", "big": "STEP 1"}]))
    out = renderer.render(spec.frames[0])
    assert out.image.getpixel((5, 5)) == _hex("#14213D")
    assert out.boxes["big"][1] < 470 < out.boxes["big"][3]


def test_count_texts_format_and_direction():
    count = Count(start=150000, end=2000, duration=1.0)
    texts = count_texts(count, 10)
    assert texts[0] == "150,000" and texts[-1] == "2,000" and len(texts) == 10
    values = [int(t.replace(",", "")) for t in texts]
    assert values == sorted(values, reverse=True)
    up = Count(start=0, end=98.7, duration=0.5, decimals=1, suffix="%")
    assert count_texts(up, 4)[-1] == "98.7%"


def test_timing_is_contiguous_and_uses_narration_lines():
    spec = spec_from_dict(
        _spec(
            [
                {"id": "a", "art": "plate", "start": 0},
                {"id": "b", "art": "plate", "line": "n2"},
                {"id": "c", "art": "plate", "start": "TBD"},
                {"id": "d", "kind": "card", "start": 9.5, "duration": 2},
            ]
        )
    )
    lines = [NarrationLine("n2", "x", 4.25, 7.0)]
    timed, warnings = frame_timing(spec.frames, lines)
    assert [(t.frame.id, t.start, t.duration) for t in timed] == [
        ("a", 0.0, 4.25),
        ("b", 4.25, 5.25),
        ("d", 9.5, 2.0),
    ]
    assert any("c" in w and "left out" in w for w in warnings)
    with pytest.raises(ValueError, match="needs an end"):
        frame_timing(spec_from_dict(_spec([{"id": "z", "art": "plate", "start": 1}])).frames)


def test_motion_parsing_and_filter_string():
    assert parse_zoom("1.0->1.08") == (1.0, 1.08)
    assert parse_zoom([1.02, 1.1]) == (1.02, 1.1)
    with pytest.raises(ValueError):
        parse_zoom("0.5->1.0")
    graph = motion_filter({"zoom": [1.0, 1.08], "pan": "right"}, 1920, 1080, 30, 4.0)
    assert "scale=7680:4320:flags=lanczos" in graph
    assert "zoompan=z='1+(0.08)*(on/119)'" in graph
    assert "x='(iw-iw/zoom)*(0.5+0.5*(on/119))'" in graph
    assert "y='(ih-ih/zoom)/2'" in graph
    assert ":d=120:s=1920x1080:fps=30" in graph
    left = motion_filter({"zoom": [1.0, 1.0], "pan": "up"}, 1920, 1080, 30, 1.0, oversample=2)
    assert "y='(ih-ih/zoom)*(0.5-0.5*(on/29))'" in left and "scale=3840:2160" in left


def test_plan_motion_round_trip_and_validation(tmp_path):
    plan = EditPlan(
        series="ai-survival",
        segments=[
            Segment(
                "a", "still", "frames/a.png", 0, 2, motion={"zoom": "1.0->1.08", "pan": "left"}
            ),
            Segment("b", "clip", "x.mp4", 2, 1, motion={"zoom": [1.0, 1.1], "pan": "sideways"}),
        ],
    )
    path = tmp_path / "plan.yaml"
    save_plan(plan, path)
    loaded = load_plan(path)
    assert loaded.segments[0].motion == {"zoom": [1.0, 1.08], "pan": "left"}
    errors = validate_plan(loaded)
    assert any("b: motion only applies to still" in e for e in errors)
    assert any("b: motion pan" in e for e in errors)
    assert not any("segment a" in e for e in errors)


def test_font_lookup_finds_inter_and_explains_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("YTP_SCENE_FONT", raising=False)
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="Inter not found"):
        find_font(None, dirs=[empty])
    fonts = tmp_path / "fonts" / "sub"
    fonts.mkdir(parents=True)
    shutil.copy(TEST_FONT, fonts / "InterVariable.ttf")
    assert find_font(None, dirs=[tmp_path / "fonts"]).name == "InterVariable.ttf"
    spec = spec_from_dict(_spec([{"id": "a", "art": "plate"}], font={"path": "missing.ttf"}))
    with pytest.raises(FileNotFoundError, match="scene font not found"):
        find_font(spec, dirs=[tmp_path / "fonts"])


def test_missing_asset_lists_where_it_looked(episode):
    data = _spec([{"id": "a", "art": "plate"}])
    data["assets"]["plate"] = "nope.png"
    renderer, spec = _renderer(episode, data)
    with pytest.raises(FileNotFoundError, match="Looked in"):
        renderer.render(spec.frames[0])
    data = _spec([{"id": "a", "art": "ghost"}])
    renderer, spec = _renderer(episode, data)
    with pytest.raises(FileNotFoundError, match="unknown asset id"):
        renderer.render(spec.frames[0])


def test_spec_errors_are_readable():
    with pytest.raises(ValueError, match="kind must be one of"):
        spec_from_dict(_spec([{"id": "a", "art": "plate", "kind": "wipe"}]))
    with pytest.raises(ValueError, match="needs art"):
        spec_from_dict(_spec([{"id": "a"}]))
    with pytest.raises(ValueError, match="duplicate frame id"):
        spec_from_dict(_spec([{"id": "a", "art": "p"}, {"id": "a", "art": "p"}]))
    with pytest.raises(ValueError, match="unknown palette"):
        spec_from_dict(_spec([{"id": "a", "art": "p", "palette": "neon"}]))


def test_cli_writes_frames_and_plan_fragment(episode, capsys):
    spec_path = episode / "edit" / "scenes.yaml"
    frames = [
        {"id": "01", "art": "plate", "big": "MEET", "caption": "Hi.", "start": 0},
        {"id": "02", "kind": "card", "big": "TIP 1", "start": 2.5, "end": 4},
    ]
    spec_path.write_text(yaml.safe_dump(_spec(frames)), encoding="utf-8")
    assert main(["scenes", str(spec_path), "--safe-guides"]) == 0
    assert Image.open(episode / "frames" / "01.png").size == (1920, 1080)
    assert (episode / "frames" / "guides" / "02.png").is_file()
    fragment = yaml.safe_load((episode / "frames" / "scenes-plan.yaml").read_text())
    segs = fragment["segments"]
    assert [(s["id"], s["source"], s["start"], s["duration"]) for s in segs] == [
        ("scene-01", "frames/01.png", 0.0, 2.5),
        ("scene-02", "frames/02.png", 2.5, 1.5),
    ]
    assert "scenes-plan.yaml" in capsys.readouterr().out


def test_plan_merge_replaces_scene_segments_and_keeps_others(episode):
    plan_path = episode / "edit" / "plan.yaml"
    plan = EditPlan(
        series="ai-survival",
        narration=[NarrationLine("n1", "Hello.", 0.0, 3.0)],
        segments=[
            Segment("scene-old", "still", "frames/old.png", 0, 3),
            Segment("talk", "clip", "take.mp4", 3, 2),
        ],
    )
    save_plan(plan, plan_path)
    spec_path = episode / "edit" / "scenes.yaml"
    frames = [
        {
            "id": "01",
            "art": "plate",
            "big": "HI",
            "line": "n1",
            "motion": {"zoom": "1.0->1.08", "pan": "left"},
        }
    ]
    spec_path.write_text(yaml.safe_dump(_spec(frames)), encoding="utf-8")
    assert main(["scenes", str(spec_path), "--plan"]) == 0
    merged = load_plan(plan_path)
    assert [s.id for s in merged.segments] == ["scene-01", "talk"]
    first = merged.segments[0]
    assert (first.start, first.duration, first.narration_ids) == (0.0, 3.0, ["n1"])
    assert first.motion == {"zoom": [1.0, 1.08], "pan": "left"}
    assert validate_plan(merged) == []


def test_count_clip_and_plan_segments(episode):
    require_ffmpeg()
    spec_path = episode / "edit" / "scenes.yaml"
    frames = [
        {
            "id": "n",
            "art": "plate",
            "was": "150,000",
            "count": {"from": 150000, "to": 2000, "duration": 0.5},
            "start": 0,
            "end": 2,
        }
    ]
    spec_path.write_text(yaml.safe_dump(_spec(frames, fps=30)), encoding="utf-8")
    result = run_scenes(spec_path, log=lambda _m: None)
    clip = result.clips["n"]
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames,width,height",
            "-of",
            "csv=p=0",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "1920,1080,15"
    kinds = [(s.id, s.kind, s.start, s.duration) for s in result.segments]
    assert kinds == [("scene-n-count", "clip", 0.0, 0.5), ("scene-n", "still", 0.5, 1.5)]


def test_render_applies_motion_to_still_segment(tmp_path):
    require_ffmpeg()
    still = tmp_path / "s.png"
    Image.new("RGB", (320, 180), (90, 120, 200)).save(still)
    plan = EditPlan(series="x", width=320, height=180, fps=30)
    segment = Segment("s", "still", str(still), 0, 1.0, motion={"zoom": [1.0, 1.1], "pan": "right"})
    part = tmp_path / "part.mp4"
    _encode_segment(still, part, segment, plan, "libx264", "veryfast")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames,width,height",
            "-of",
            "csv=p=0",
            str(part),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "320,180,30"


def test_safe_area_measures_visible_pixels_not_the_canvas(episode):
    art = episode / "art"
    lower = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    ImageDraw.Draw(lower).rectangle((666, 860, 1254, 980), fill=(20, 20, 20, 230))
    lower.save(art / "lower.png")
    wide = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    ImageDraw.Draw(wide).rectangle((600, 860, 1254, 980), fill=(20, 20, 20, 230))
    wide.save(art / "wide.png")
    Image.new("RGBA", (1920, 1080), (0, 0, 0, 0)).save(art / "empty.png")
    layer = {"x": 0, "y": 0, "anchor": "topleft", "role": "text"}
    frames = [
        {"id": "lt", "art": "plate", "layers": [{**layer, "asset": "lower"}]},
        {"id": "wide", "art": "plate", "layers": [{**layer, "asset": "wide"}]},
        {"id": "empty", "art": "plate", "layers": [{**layer, "asset": "empty"}]},
    ]
    data = _spec(frames)
    data["assets"].update({"lower": "lower.png", "wide": "wide.png", "empty": "empty.png"})
    renderer, spec = _renderer(episode, data)
    lt = renderer.render(spec.frames[0])
    assert lt.warnings == []
    assert lt.boxes["layers"][0][2] == (0, 0, 1920, 1080)
    assert lt.boxes["layers_visible"][0][2] == (666, 860, 1255, 981)
    wide_out = renderer.render(spec.frames[1])
    assert len(wide_out.warnings) == 1 and "visible x 600..1255" in wide_out.warnings[0]
    assert renderer.render(spec.frames[2]).warnings == []


def test_scene_assets_default_to_ytp_art_output(episode, tmp_path, monkeypatch):
    from ytp import art
    from ytp.scenes import asset_search_dirs

    out = tmp_path / "YouTube" / "assets" / "scenes"
    monkeypatch.setenv("YTP_ART_OUT", str(out))
    spec = spec_from_dict(_spec([{"id": "a", "art": "plate"}]), episode / "edit" / "scenes.yaml")
    dirs = asset_search_dirs(spec, episode)
    assert out in dirs and dirs.index(out) < dirs.index(episode / "assets" / "scenes")
    assert repo_root().parent / "assets" / "scenes" not in dirs
    override = tmp_path / "override"
    monkeypatch.setenv("YTP_SCENE_ASSETS", str(override))
    dirs = asset_search_dirs(spec, episode)
    assert dirs.index(override) < dirs.index(out)
    monkeypatch.delenv("YTP_ART_OUT")
    assert art.default_out_dir() in asset_search_dirs(spec, episode)
    (out / "plate2.png").parent.mkdir(parents=True)
    Image.new("RGB", (16, 9), PLATE).save(out / "plate2.png")
    monkeypatch.setenv("YTP_ART_OUT", str(out))
    monkeypatch.delenv("YTP_SCENE_ASSETS")
    from ytp.scenes import resolve_asset

    data = _spec([{"id": "a", "art": "plate"}])
    data["assets"]["p2"] = "plate2.png"
    assert resolve_asset("p2", spec_from_dict(data, episode / "edit" / "scenes.yaml"), episode) == (
        out / "plate2.png"
    )
