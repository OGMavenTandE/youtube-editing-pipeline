"""ytp art: jobs validation, paths, sidecars, contact sheet, alpha. Pipelines are faked."""

from __future__ import annotations

import builtins
import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("PIL", reason="ytp art needs Pillow")

from PIL import Image, ImageDraw  # noqa: E402

from ytp import art  # noqa: E402
from ytp.art import (  # noqa: E402
    ArtExtraMissing,
    alpha_stats,
    check_model,
    contact_sheet,
    fit,
    jobs_from_dict,
    load_jobs,
    native_size,
    prepare_model_env,
    require_art_extra,
    run_art,
)
from ytp.cli import main  # noqa: E402
from ytp.rules import repo_root  # noqa: E402


class FakeBackend:
    """Solid images at the requested size; the matte clears the left half."""

    def __init__(self):
        self.calls: list[dict] = []
        self.mattes: list[str] = []

    def generate(self, job, seed, size, init, mask):
        self.calls.append(
            {
                "id": job.id,
                "task": job.task,
                "seed": seed,
                "size": size,
                "init": None if init is None else init.size,
                "mask": None if mask is None else mask.size,
                "model": job.diffusion_model,
            }
        )
        shade = seed % 200
        return Image.new("RGB", size, (shade, 120, 200))

    def matte(self, image, model):
        self.mattes.append(model)
        rgba = image.convert("RGBA")
        alpha = Image.new("L", rgba.size, 255)
        ImageDraw.Draw(alpha).rectangle((0, 0, rgba.width // 2 - 1, rgba.height), fill=0)
        rgba.putalpha(alpha)
        return rgba

    def stats(self):
        return {"device": "fake", "vram_peak_gb": 1.5}


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _png(path: Path, size=(1024, 1024), color=(200, 60, 60, 255), mode="RGBA") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color if mode == "RGBA" else color[:3]).save(path)
    return path


def _jobs(tmp_path: Path) -> Path:
    _png(tmp_path / "refs" / "style.png", (800, 600))
    _png(tmp_path / "refs" / "host.png", (1024, 1024))
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rectangle((300, 0, 700, 250), fill=255)
    mask.save(tmp_path / "refs" / "mask.png")
    return _write(
        tmp_path / "batch.yaml",
        {
            "version": 1,
            "defaults": {"negative": "text, watermark", "steps": 4},
            "jobs": [
                {"id": "plate-a", "kind": "plate", "prompt": "a kitchen", "seed": 10, "count": 2},
                {
                    "id": "host-a",
                    "kind": "pose",
                    "prompt": "a host waving",
                    "seed": 20,
                    "alpha": True,
                    "reference": "refs/style.png",
                    "strength": 0.5,
                },
                {
                    "id": "coin",
                    "kind": "prop",
                    "prompt": "a coin",
                    "size": "768x768",
                    "seed": 30,
                    "alpha": True,
                },
                {
                    "id": "host-fix",
                    "kind": "pose",
                    "mode": "inpaint",
                    "init": "refs/host.png",
                    "mask": "refs/mask.png",
                    "prompt": "cap facing forward",
                    "seed": 40,
                },
            ],
        },
    )


# --------------------------------------------------------------------------- validation


def test_docs_example_is_valid(tmp_path):
    text = (repo_root() / "docs" / "art-jobs.md").read_text(encoding="utf-8")
    block = re.search(r"## Jobs file\s+```yaml\n(.*?)```", text, re.S).group(1)
    jobs = jobs_from_dict(yaml.safe_load(block), tmp_path / "art.yaml").jobs
    assert [job.id for job in jobs] == [
        "plate-kitchen",
        "host-wave",
        "coin-meter",
        "host-wave-capfix",
    ]
    assert [job.kind for job in jobs] == ["plate", "pose", "prop", "pose"]
    assert [job.task for job in jobs] == ["txt2img", "img2img", "txt2img", "inpaint"]
    assert jobs[0].size == (1920, 1080) and jobs[0].seeds() == [1201, 1202]
    assert jobs[1].alpha and jobs[1].strength == 0.65
    assert jobs[1].reference == tmp_path / "refs" / "host-style.png"
    assert jobs[2].size == (768, 768)
    assert jobs[3].diffusion_model == art.DEFAULT_INPAINT_MODEL
    assert all(job.cfg == 6.5 and job.steps == 30 for job in jobs)


def test_defaults_sizes_and_strengths(tmp_path):
    jobs = load_jobs(_jobs(tmp_path)).jobs
    plate, host, coin, fix = jobs
    assert plate.size == (1920, 1080) and plate.negative == "text, watermark" and plate.steps == 4
    assert host.size == (1024, 1024) and host.task == "img2img" and host.strength == 0.5
    assert coin.size == (768, 768) and coin.strength is None
    assert fix.size == (1024, 1024) and fix.strength == art.DEFAULT_INPAINT_STRENGTH


def test_inpaint_defaults_to_init_size(tmp_path):
    _png(tmp_path / "init.png", (900, 1200))
    _png(tmp_path / "mask.png", (900, 1200))
    data = {
        "version": 1,
        "jobs": [
            {
                "id": "x",
                "kind": "pose",
                "mode": "inpaint",
                "init": "init.png",
                "mask": "mask.png",
                "prompt": "p",
            },
        ],
    }
    assert load_jobs(_write(tmp_path / "j.yaml", data)).jobs[0].size == (900, 1200)


def test_seed_defaults_to_stable_number():
    data = {"version": 1, "jobs": [{"id": "abc", "kind": "prop", "prompt": "p", "count": 3}]}
    first = jobs_from_dict(data, Path("j.yaml")).jobs[0]
    again = jobs_from_dict(data, Path("j.yaml")).jobs[0]
    assert first.seed == again.seed == art.default_seed("abc")
    assert first.seeds() == [first.seed, first.seed + 1, first.seed + 2]


@pytest.mark.parametrize(
    ("job", "message"),
    [
        ({"kind": "poster"}, "kind must be one of"),
        ({"prompt": ""}, "prompt is required"),
        ({"size": "big"}, "size must look like"),
        ({"size": "32x32"}, "64..4096"),
        ({"count": 0}, "count must be 1..16"),
        ({"steps": 2.5}, "whole number"),
        ({"cfg": 99}, "cfg must be"),
        ({"alpha": "yes"}, "alpha must be true or false"),
        ({"mode": "outpaint"}, "mode must be one of"),
        ({"mode": "inpaint", "init": "a.png"}, "needs mask"),
        ({"mode": "img2img"}, "needs init"),
        ({"mask": "m.png"}, "mask needs mode inpaint"),
        ({"init": "i.png"}, "init needs mode"),
        ({"strength": 0.5}, "strength needs a reference"),
        (
            {"mode": "inpaint", "init": "a.png", "mask": "m.png", "reference": "r.png"},
            "reference is for text-to-image",
        ),
        ({"colour": "red"}, "unknown keys: colour"),
        ({"id": "bad id"}, "id must be"),
    ],
)
def test_job_validation_errors(job, message):
    base = {"id": "j1", "kind": "prop", "prompt": "a coin"}
    base.update(job)
    with pytest.raises(ValueError, match=message):
        jobs_from_dict({"version": 1, "jobs": [base]}, Path("jobs.yaml"))


def test_file_level_validation():
    good = {"id": "a", "kind": "prop", "prompt": "p"}
    with pytest.raises(ValueError, match="version must be 1"):
        jobs_from_dict({"version": 2, "jobs": [good]}, Path("j.yaml"))
    with pytest.raises(ValueError, match="non-empty list"):
        jobs_from_dict({"version": 1, "jobs": []}, Path("j.yaml"))
    with pytest.raises(ValueError, match="duplicate id"):
        jobs_from_dict({"version": 1, "jobs": [good, good]}, Path("j.yaml"))
    with pytest.raises(ValueError, match="unknown top-level keys: extra"):
        jobs_from_dict({"version": 1, "jobs": [good], "extra": 1}, Path("j.yaml"))
    with pytest.raises(ValueError, match="unknown keys in defaults: id"):
        jobs_from_dict({"version": 1, "defaults": {"id": "x"}, "jobs": [good]}, Path("j.yaml"))


# --------------------------------------------------------------------------- model policy


def test_default_models_pass_policy_and_carry_origin():
    for model_id, origin in [
        (art.DEFAULT_MODEL, "UK"),
        (art.DEFAULT_INPAINT_MODEL, "US"),
        (art.DEFAULT_MATTE_MODEL, "CA"),
    ]:
        info = check_model(model_id, {}, art.DEFAULT_ALLOWED_ORIGINS)
        assert info.origin == origin and info.license and info.org


def test_unknown_model_is_refused():
    with pytest.raises(ValueError, match="no declared origin"):
        check_model("someone/mystery-model", {}, ("US",))


@pytest.mark.parametrize(
    "model_id",
    [
        "h94/IP-Adapter",
        "ZhengPeng7/BiRefNet",
        "Kwai-Kolors/Kolors",
        "Tencent-Hunyuan/HunyuanDiT",
        "ByteDance/SDXL-Lightning",
        "birefnet-general",
    ],
)
def test_blocked_models_refused_even_when_declared(model_id):
    declared = {model_id: art.ModelInfo(model_id, "diffusion", "x", "US", "MIT")}
    with pytest.raises(ValueError, match="blocked"):
        check_model(model_id, declared, ("US",))


def test_origin_list_is_configurable(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="from UK"):
        check_model(art.DEFAULT_MODEL, {}, ("US",))
    data = {
        "version": 1,
        "models": {
            "acme/us-diffusion": {
                "role": "diffusion",
                "org": "Acme (Ohio)",
                "origin": "us",
                "license": "Apache-2.0",
            },
        },
        "defaults": {"model": "acme/us-diffusion", "allowed_origins": ["US"]},
        "jobs": [{"id": "a", "kind": "plate", "prompt": "p", "seed": 1}],
    }
    jobs_path = _write(tmp_path / "us.yaml", data)
    backend = FakeBackend()
    result = run_art(jobs_path, out_dir=tmp_path / "out", backend=backend, log=lambda _: None)
    assert backend.calls[0]["model"] == "acme/us-diffusion"
    sidecar = json.loads(result.jobs[0].sidecar.read_text(encoding="utf-8"))
    assert sidecar["model"]["origin"] == "US" and sidecar["model"]["license"] == "Apache-2.0"
    monkeypatch.setenv("YTP_ART_ALLOWED_ORIGINS", "UK")
    with pytest.raises(ValueError, match="from US"):
        run_art(jobs_path, out_dir=tmp_path / "o2", backend=FakeBackend(), log=lambda _: None)


def test_declared_model_needs_origin():
    data = {
        "version": 1,
        "models": {"a/b": {"org": "x", "license": "MIT"}},
        "jobs": [{"id": "a", "kind": "prop", "prompt": "p"}],
    }
    with pytest.raises(ValueError, match="needs origin"):
        jobs_from_dict(data, Path("j.yaml"))


def test_model_ids_come_from_env_and_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("YTP_ART_MODEL", "acme/us-diffusion")
    data = {"version": 1, "jobs": [{"id": "a", "kind": "prop", "prompt": "p"}]}
    assert jobs_from_dict(data, Path("j.yaml")).jobs[0].model == "acme/us-diffusion"
    monkeypatch.delenv("YTP_ART_MODEL")
    jobs_path = _write(tmp_path / "j.yaml", data)
    with pytest.raises(ValueError, match="no declared origin"):
        run_art(
            jobs_path,
            out_dir=tmp_path / "o",
            model="acme/other",
            backend=FakeBackend(),
            log=lambda _: None,
        )


# --------------------------------------------------------------------------- sizes and images


def test_native_size_picks_closest_sdxl_bucket():
    assert native_size(1920, 1080) == (1344, 768)
    assert native_size(1024, 1024) == (1024, 1024)
    assert native_size(768, 768) == (1024, 1024)
    assert native_size(1080, 1920) == (768, 1344)
    assert native_size(2560, 1080) == (1536, 640)


def test_fit_covers_and_crops_exactly():
    image = Image.new("RGB", (1344, 768), (0, 0, 255))
    ImageDraw.Draw(image).rectangle((0, 0, 10, 767), fill=(255, 0, 0))
    out = fit(image, 1920, 1080)
    assert out.size == (1920, 1080)
    square = fit(Image.new("RGB", (1344, 768)), 1024, 1024)
    assert square.size == (1024, 1024)


def test_alpha_stats_and_flatten():
    rgba = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
    rgba.paste((255, 0, 0, 255), (0, 0, 5, 10))
    rgba.putpixel((9, 9), (255, 0, 0, 128))
    stats = alpha_stats(rgba)
    assert stats == {"transparent": 0.49, "partial": 0.01, "opaque": 0.5}
    assert alpha_stats(Image.new("RGB", (4, 4)))["opaque"] == 1.0
    flat = art.flatten(rgba)
    assert flat.mode == "RGB" and flat.getpixel((9, 0)) == (255, 255, 255)


def test_contact_sheet_checkerboard_behind_alpha(tmp_path):
    clear = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    solid = _png(tmp_path / "solid.png", (400, 300), (10, 200, 10, 255))
    clear.save(tmp_path / "clear.png")
    sheet = contact_sheet([("clear", tmp_path / "clear.png"), ("solid", solid)], columns=2)
    assert sheet.size == (2 * (320 + 16), 240 + 28 + 16)
    tile = sheet.crop((8, 8, 8 + 320, 8 + 240))
    colors = {color for _count, color in tile.getcolors(maxcolors=10)}
    assert colors == {(204, 204, 204), (255, 255, 255)}
    assert sheet.getpixel((336 + 8 + 160, 8 + 120)) == (10, 200, 10)
    label_band = sheet.crop((8, 8 + 240, 8 + 320, 8 + 240 + 28)).convert("L")
    assert label_band.getextrema()[1] > 200  # label text drawn


# --------------------------------------------------------------------------- runs


def test_run_writes_pngs_sidecars_and_contact_sheet(tmp_path):
    out = tmp_path / "out"
    backend = FakeBackend()
    result = run_art(_jobs(tmp_path), out_dir=out, backend=backend, log=lambda _: None)

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert files == [
        "contact-batch.png",
        "plate/plate-a-v2.png",
        "plate/plate-a.json",
        "plate/plate-a.png",
        "pose/host-a.json",
        "pose/host-a.png",
        "pose/host-fix.json",
        "pose/host-fix.png",
        "prop/coin.json",
        "prop/coin.png",
    ]
    assert [(c["id"], c["seed"], c["size"], c["task"]) for c in backend.calls] == [
        ("plate-a", 10, (1344, 768), "txt2img"),
        ("plate-a", 11, (1344, 768), "txt2img"),
        ("host-a", 20, (1024, 1024), "img2img"),
        ("coin", 30, (1024, 1024), "txt2img"),
        ("host-fix", 40, (1024, 1024), "inpaint"),
    ]
    assert backend.calls[2]["init"] == (1024, 1024)  # reference fit to the bucket
    assert backend.calls[4]["mask"] == (1024, 1024)
    assert backend.mattes == ["u2net", "u2net"]

    with Image.open(out / "plate" / "plate-a.png") as plate:
        assert plate.size == (1920, 1080) and plate.mode == "RGB"
    with Image.open(out / "prop" / "coin.png") as coin:
        assert coin.size == (768, 768) and coin.mode == "RGBA"
        assert coin.getpixel((10, 10))[3] == 0 and coin.getpixel((700, 10))[3] == 255

    side = json.loads((out / "pose" / "host-a.json").read_text(encoding="utf-8"))
    assert side["prompt"] == "a host waving" and side["negative"] == "text, watermark"
    assert side["model"]["id"] == art.DEFAULT_MODEL and side["model"]["origin"] == "UK"
    assert side["model"]["license"] == "CreativeML Open RAIL++-M"
    assert side["matte_model"]["origin"] == "CA"
    assert side["variants"][0]["seed"] == 20 and side["variants"][0]["seconds"] >= 0
    assert side["variants"][0]["alpha_stats"]["transparent"] == 0.5
    assert side["runtime"]["vram_peak_gb"] == 1.5
    assert side["reference"].endswith("style.png") and side["strength"] == 0.5
    fix = json.loads((out / "pose" / "host-fix.json").read_text(encoding="utf-8"))
    assert fix["model"]["id"] == art.DEFAULT_INPAINT_MODEL and fix["matte_model"] is None
    plate_side = json.loads((out / "plate" / "plate-a.json").read_text(encoding="utf-8"))
    assert [v["file"] for v in plate_side["variants"]] == ["plate-a.png", "plate-a-v2.png"]

    with Image.open(result.contact) as sheet:
        assert sheet.size == (4 * 336, 2 * (240 + 28 + 16))


def test_no_overwrite_without_force(tmp_path):
    out = tmp_path / "out"
    jobs_path = _jobs(tmp_path)
    run_art(jobs_path, out_dir=out, only=["coin"], backend=FakeBackend(), log=lambda _: None)
    stamp = (out / "prop" / "coin.png").stat().st_mtime_ns
    backend = FakeBackend()
    with pytest.raises(FileExistsError, match="coin.png"):
        run_art(jobs_path, out_dir=out, only=["coin"], backend=backend, log=lambda _: None)
    assert backend.calls == []  # stops before any generation
    assert (out / "prop" / "coin.png").stat().st_mtime_ns == stamp

    result = run_art(
        jobs_path, out_dir=out, only=["coin"], force=True, backend=backend, log=lambda _: None
    )
    assert len(backend.calls) == 1 and result.contact == out / "contact-batch.png"


def test_contact_sheet_is_not_replaced_without_force(tmp_path):
    out = tmp_path / "out"
    jobs_path = _jobs(tmp_path)
    run_art(jobs_path, out_dir=out, only=["coin"], backend=FakeBackend(), log=lambda _: None)
    second = run_art(
        jobs_path, out_dir=out, only=["plate-a"], backend=FakeBackend(), log=lambda _: None
    )
    assert second.contact == out / "contact-batch-2.png"


def test_only_and_unknown_only(tmp_path):
    jobs_path = _jobs(tmp_path)
    backend = FakeBackend()
    run_art(
        jobs_path, out_dir=tmp_path / "o", only=["host-fix"], backend=backend, log=lambda _: None
    )
    assert [c["id"] for c in backend.calls] == ["host-fix"]
    with pytest.raises(ValueError, match="no job with id nope"):
        run_art(
            jobs_path, out_dir=tmp_path / "o", only=["nope"], backend=backend, log=lambda _: None
        )


def test_dry_run_loads_nothing_and_writes_nothing(tmp_path):
    jobs_path = _jobs(tmp_path)
    lines: list[str] = []

    def explode():
        raise AssertionError("dry run must not build a backend")

    result = run_art(
        jobs_path,
        out_dir=tmp_path / "out",
        dry_run=True,
        backend_factory=explode,
        models_dir=tmp_path / "models",
        log=lines.append,
    )
    assert result.dry_run and not (tmp_path / "out").exists() and not (tmp_path / "models").exists()
    text = "\n".join(lines)
    assert "plate-a: plate txt2img 1920x1080 (generate 1344x768)" in text
    assert "seeds 10, 11" in text and "alpha via u2net" in text
    assert "origin UK" in text and "origin CA" in text and "origin US" in text


def test_missing_input_image_is_reported_before_models(tmp_path):
    data = {
        "version": 1,
        "jobs": [{"id": "a", "kind": "pose", "prompt": "p", "reference": "nope.png"}],
    }
    backend = FakeBackend()
    with pytest.raises(FileNotFoundError, match="nope.png"):
        run_art(
            _write(tmp_path / "j.yaml", data),
            out_dir=tmp_path / "o",
            backend=backend,
            log=lambda _: None,
        )
    assert backend.calls == []


def test_inpaint_mask_from_alpha_and_init_alpha_flattened(tmp_path):
    rgba = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).rectangle((0, 0, 31, 63), fill=(0, 0, 0, 255))
    rgba.save(tmp_path / "m.png")
    mask = art.load_mask(tmp_path / "m.png", (128, 128))
    assert mask.mode == "L" and mask.getpixel((10, 10)) == 255 and mask.getpixel((120, 10)) == 0
    init = art.load_init(tmp_path / "m.png", (128, 128))
    assert init.mode == "RGB" and init.getpixel((120, 10)) == (255, 255, 255)


# --------------------------------------------------------------------------- environment


def test_model_cache_env_points_at_models_dir(tmp_path, monkeypatch):
    for key in ("HF_HOME", "U2NET_HOME"):
        monkeypatch.delenv(key, raising=False)
    values = prepare_model_env(tmp_path / "models")
    assert values["HF_HOME"] == str(tmp_path / "models")
    assert values["U2NET_HOME"] == str(tmp_path / "models" / "u2net")
    import os

    assert os.environ["HF_HOME"] == str(tmp_path / "models")


def test_default_dirs_follow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("YTP_ART_OUT", str(tmp_path / "art"))
    monkeypatch.setenv("YTP_MODELS_DIR", str(tmp_path / "m"))
    assert art.default_out_dir() == tmp_path / "art"
    assert art.default_models_dir() == tmp_path / "m"


def test_missing_extra_has_clear_message(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("torch", "diffusers", "rembg"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ArtExtraMissing, match=r"pip install -e \.\[art\].*torch, diffusers, rembg"):
        require_art_extra()


def test_cli_dry_run_and_missing_extra(tmp_path, monkeypatch, capsys):
    jobs_path = _jobs(tmp_path)
    assert main(["art", str(jobs_path), "--out", str(tmp_path / "o"), "--dry-run"]) == 0
    assert "dry run: 4 job(s), 5 image(s)" in capsys.readouterr().out

    def missing(**_kwargs):
        raise ArtExtraMissing(art.EXTRA_HINT)

    monkeypatch.setattr(art, "DiffusersBackend", missing)
    monkeypatch.setenv("YTP_MODELS_DIR", str(tmp_path / "models"))
    code = main(["art", str(jobs_path), "--out", str(tmp_path / "o"), "--only", "coin"])
    assert code == 2 and "[art] extra" in capsys.readouterr().err


def test_cli_runs_with_backend_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    jobs_path = _jobs(tmp_path)
    monkeypatch.setattr(art, "DiffusersBackend", lambda **_kwargs: FakeBackend())
    monkeypatch.setenv("YTP_MODELS_DIR", str(tmp_path / "models"))
    args = ["art", str(jobs_path), "--out", str(tmp_path / "o"), "--only", "coin"]
    assert main(args) == 0
    assert "done: 1 image(s)" in capsys.readouterr().out
    assert main(args) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (
        main(
            [
                "art",
                str(jobs_path),
                "--out",
                str(tmp_path / "o"),
                "--allow-origin",
                "US",
                "--only",
                "coin",
                "--force",
            ]
        )
        == 2
    )
    assert "allowed origins are US" in capsys.readouterr().err


# ------------------------------------------------------------- diffusers backend (fake modules)


class _FakePipe:
    log: list = []

    def __init__(self, cls_name, model, kwargs):
        self.cls_name, self.model, self.kwargs = cls_name, model, kwargs
        self.vae = type(
            "Vae", (), {"enable_tiling": lambda _self: _FakePipe.log.append("tiling")}
        )()

    def enable_model_cpu_offload(self):
        _FakePipe.log.append(("offload", self.cls_name))

    def to(self, device):
        _FakePipe.log.append(("to", device))

    def set_progress_bar_config(self, **_kwargs):
        pass

    def __call__(self, **kwargs):
        _FakePipe.log.append(("call", self.cls_name, sorted(kwargs)))
        size = (kwargs.get("width", 64), kwargs.get("height", 64))
        return type("Out", (), {"images": [Image.new("RGB", size)]})()


def _auto(name):
    class Auto:
        @classmethod
        def from_pretrained(cls, model, **kwargs):
            _FakePipe.log.append(("load", name, model, kwargs.get("variant")))
            return _FakePipe(name, model, kwargs)

        @classmethod
        def from_pipe(cls, pipe):
            _FakePipe.log.append(("from_pipe", name, pipe.model))
            return _FakePipe(name, pipe.model, {})

    return Auto


@pytest.fixture
def fake_stack(monkeypatch):
    import sys
    import types

    torch = types.ModuleType("torch")
    torch.__version__ = "0-fake"
    torch.float16, torch.float32 = "torch.float16", "torch.float32"

    class Cuda:
        is_available = staticmethod(lambda: True)
        reset_peak_memory_stats = staticmethod(lambda: None)
        get_device_properties = staticmethod(
            lambda _i: type("P", (), {"total_memory": 12 * 1024**3})
        )
        get_device_name = staticmethod(lambda _i: "Fake 4070")
        max_memory_allocated = staticmethod(lambda: 7 * 1024**3)
        max_memory_reserved = staticmethod(lambda: 8 * 1024**3)

    class Generator:
        def __init__(self, device):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    torch.cuda = Cuda
    torch.Generator = Generator
    diffusers = types.ModuleType("diffusers")
    diffusers.__version__ = "0-fake"
    diffusers.AutoPipelineForText2Image = _auto("txt2img")
    diffusers.AutoPipelineForImage2Image = _auto("img2img")
    diffusers.AutoPipelineForInpainting = _auto("inpaint")
    rembg = types.ModuleType("rembg")
    rembg.new_session = lambda name: ("session", name)
    rembg.remove = lambda image, session: image.convert("RGBA")
    for name, module in (("torch", torch), ("diffusers", diffusers), ("rembg", rembg)):
        monkeypatch.setitem(sys.modules, name, module)
    _FakePipe.log = []
    return _FakePipe.log


def test_diffusers_backend_offloads_reuses_weights_and_reports(tmp_path, fake_stack):
    jobs = load_jobs(_jobs(tmp_path)).jobs
    backend = art.DiffusersBackend()
    assert backend.offload == "model"  # 12 GB card
    plate, host, _coin, fix = jobs
    backend.generate(plate, 1, (1344, 768), None, None)
    init = Image.new("RGB", (1024, 1024))
    backend.generate(host, 2, (1024, 1024), init, None)
    backend.generate(fix, 3, (1024, 1024), init, Image.new("L", (1024, 1024)))
    matte = backend.matte(Image.new("RGB", (8, 8)), "u2net")
    assert matte.mode == "RGBA"
    loads = [entry for entry in fake_stack if entry[0] in ("load", "from_pipe")]
    assert loads == [
        ("load", "txt2img", art.DEFAULT_MODEL, "fp16"),
        ("from_pipe", "img2img", art.DEFAULT_MODEL),
        ("load", "inpaint", art.DEFAULT_INPAINT_MODEL, "fp16"),
    ]
    assert ("offload", "txt2img") in fake_stack and fake_stack.count("tiling") == 2
    calls = [entry for entry in fake_stack if entry[0] == "call"]
    assert "width" in calls[0][2] and "strength" not in calls[0][2]
    assert "image" in calls[1][2] and "strength" in calls[1][2]
    assert "mask_image" in calls[2][2]
    stats = backend.stats()
    assert stats["vram_peak_gb"] == 7.0 and stats["gpu"] == "Fake 4070"
    assert set(stats["load_seconds"]) == {
        f"{art.DEFAULT_MODEL} (txt2img)",
        f"{art.DEFAULT_MODEL} (img2img)",
        f"{art.DEFAULT_INPAINT_MODEL} (inpaint)",
        "u2net (matte)",
    }
