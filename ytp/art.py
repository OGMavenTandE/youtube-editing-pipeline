"""Studio's art on the local GPU: a jobs file in, plates, poses, and props out.

``ytp art`` reads a small YAML jobs file and makes each image with a local
diffusion pipeline (text to image, img2img from a reference or an init image,
or inpaint with a mask), cuts the background out for jobs with ``alpha: true``,
and writes PNGs, a JSON sidecar per job, and a contact sheet for review.

Nothing here is hard-wired to one model. The diffusion, inpaint, and matte
model ids come from the jobs file, the command line, or the environment, and
every model must be in the origin registry (built in or declared in the jobs
file) with an origin the policy allows. A model with no declared origin is
refused. See docs/art-jobs.md.

Torch, diffusers, and rembg are the optional ``[art]`` extra. They are imported
only when a job actually runs, so ``--dry-run`` and the tests work without them.
"""

from __future__ import annotations

import json
import os
import re
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

try:  # Pillow is a core dependency, but keep the import error readable.
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError as exc:  # pragma: no cover - exercised only without Pillow
    raise ImportError("ytp art needs Pillow: pip install Pillow") from exc

JOBS_VERSION = 1
KINDS = ("plate", "pose", "prop")
MODES = ("txt2img", "img2img", "inpaint")
DEFAULT_SIZES: dict[str, tuple[int, int]] = {
    "plate": (1920, 1080),
    "pose": (1024, 1024),
    "prop": (1024, 1024),
}
# SDXL's training buckets (about one megapixel). Jobs generate at the bucket
# closest to the target aspect, then resize and center-crop to the target.
NATIVE_SIZES: tuple[tuple[int, int], ...] = (
    (1024, 1024),
    (1152, 896),
    (896, 1152),
    (1216, 832),
    (832, 1216),
    (1344, 768),
    (768, 1344),
    (1536, 640),
    (640, 1536),
)
MAX_SIDE = 4096
MAX_COUNT = 16
DEFAULT_STEPS = 30
DEFAULT_CFG = 7.0
DEFAULT_REFERENCE_STRENGTH = 0.6
DEFAULT_IMG2IMG_STRENGTH = 0.6
DEFAULT_INPAINT_STRENGTH = 0.99
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Defaults. Each one can be replaced in the jobs file (defaults.model,
# defaults.inpaint_model, defaults.matte_model), on the command line, or with
# YTP_ART_MODEL / YTP_ART_INPAINT_MODEL / YTP_ART_MATTE_MODEL.
DEFAULT_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
DEFAULT_INPAINT_MODEL = "diffusers/stable-diffusion-xl-1.0-inpainting-0.1"
DEFAULT_MATTE_MODEL = "u2net"
# Origins the policy accepts unless the jobs file, --allow-origin, or
# YTP_ART_ALLOWED_ORIGINS says otherwise (ISO 3166 alpha-2, UK for Britain).
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = ("US", "UK", "CA")

# Refused whatever a jobs file declares: model families from China-origin labs,
# IP-Adapter from h94/Tencent, and BiRefNet. Lowercase substrings of the model id.
BLOCKED_MODEL_PATTERNS: tuple[str, ...] = (
    "birefnet",
    "ip-adapter",
    "ip_adapter",
    "ipadapter",
    "h94/",
    "tencent",
    "hunyuan",
    "kolors",
    "kwai",
    "qwen",
    "alibaba",
    "wan-ai",
    "deepseek",
    "baidu",
    "bytedance",
    "sdxl-lightning",
    "hyper-sd",
    "thudm",
    "zhipu",
    "cogview",
    "pixart",
    "sensetime",
    "latent-consistency",
    "playgroundai",
    "lumina",
    "alpha-vllm",
)


@dataclass(frozen=True)
class ModelInfo:
    """Where a model comes from. Logged in every sidecar."""

    id: str
    role: str  # diffusion, inpaint, or matte
    org: str
    origin: str  # ISO 3166 alpha-2 country of the organization that released it
    license: str
    source: str = ""


KNOWN_MODELS: dict[str, ModelInfo] = {
    "stabilityai/stable-diffusion-xl-base-1.0": ModelInfo(
        id="stabilityai/stable-diffusion-xl-base-1.0",
        role="diffusion",
        org="Stability AI (London)",
        origin="UK",
        license="CreativeML Open RAIL++-M",
        source="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0",
    ),
    "diffusers/stable-diffusion-xl-1.0-inpainting-0.1": ModelInfo(
        id="diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
        role="inpaint",
        org="Hugging Face diffusers team (Hugging Face Inc., New York)",
        origin="US",
        license="CreativeML Open RAIL++-M",
        source="https://huggingface.co/diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
    ),
    "u2net": ModelInfo(
        id="u2net",
        role="matte",
        org="University of Alberta (U^2-Net, Qin et al. 2020), packaged by rembg",
        origin="CA",
        license="Apache-2.0 (U^2-Net); rembg is MIT",
        source="https://github.com/xuebinqin/U-2-Net",
    ),
}


class ArtExtraMissing(RuntimeError):
    """Torch, diffusers, or rembg is not installed."""


EXTRA_HINT = (
    "ytp art needs the optional [art] extra (torch, diffusers, rembg). Install a CUDA "
    "build of torch first (see docs/art-jobs.md), then: pip install -e .[art]"
)


# --------------------------------------------------------------------------- jobs file


@dataclass
class Job:
    id: str
    kind: str
    prompt: str
    negative: str = ""
    width: int = 1024
    height: int = 1024
    seed: int = 0
    count: int = 1
    steps: int = DEFAULT_STEPS
    cfg: float = DEFAULT_CFG
    mode: str = "txt2img"
    reference: Path | None = None
    init: Path | None = None
    mask: Path | None = None
    strength: float | None = None
    alpha: bool = False
    model: str = DEFAULT_MODEL
    inpaint_model: str = DEFAULT_INPAINT_MODEL
    matte_model: str = DEFAULT_MATTE_MODEL

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def task(self) -> str:
        """What the diffusion pipeline does: txt2img, img2img, or inpaint."""
        if self.mode == "txt2img" and self.reference is not None:
            return "img2img"
        return self.mode

    @property
    def source_image(self) -> Path | None:
        return self.reference if self.mode == "txt2img" else self.init

    @property
    def diffusion_model(self) -> str:
        return self.inpaint_model if self.mode == "inpaint" else self.model

    def seeds(self) -> list[int]:
        return [self.seed + index for index in range(self.count)]


@dataclass
class JobsFile:
    path: Path
    jobs: list[Job]
    models: dict[str, ModelInfo] = field(default_factory=dict)
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS

    @property
    def stem(self) -> str:
        return self.path.stem


JOB_KEYS = {
    "id",
    "kind",
    "prompt",
    "negative",
    "size",
    "seed",
    "count",
    "steps",
    "cfg",
    "reference",
    "strength",
    "alpha",
    "mode",
    "init",
    "mask",
    "model",
    "inpaint_model",
    "matte_model",
}
DEFAULT_KEYS = JOB_KEYS - {"id", "init", "mask", "reference"} | {"allowed_origins"}
TOP_KEYS = {"version", "defaults", "jobs", "models"}
MODEL_KEYS = {"role", "org", "origin", "license", "source"}


def parse_size(value: Any, where: str) -> tuple[int, int]:
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*[xX\u00d7]\s*(\d+)\s*", value)
        if not match:
            raise ValueError(f"{where}: size must look like 1920x1080, got {value!r}")
        width, height = int(match.group(1)), int(match.group(2))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            width, height = int(value[0]), int(value[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{where}: size must be two integers") from exc
    else:
        raise ValueError(f"{where}: size must be 'WxH' or [W, H], got {value!r}")
    if not (64 <= width <= MAX_SIDE and 64 <= height <= MAX_SIDE):
        raise ValueError(f"{where}: size {width}x{height} must be 64..{MAX_SIDE} on each side")
    return width, height


def _int(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be a whole number, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{where} must be {low}..{high}, got {value}")
    return value


def _float(value: Any, where: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number, got {value!r}")
    value = float(value)
    if not low <= value <= high:
        raise ValueError(f"{where} must be {low:g}..{high:g}, got {value:g}")
    return value


def _text(value: Any, where: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{where} must be text, got {value!r}")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError(f"{where} is required")
    return value


def _path(value: Any, base: Path, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a file path")
    path = Path(os.path.expandvars(value.strip())).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def default_seed(job_id: str) -> int:
    """A stable seed per id, so a job without one still reproduces."""
    return zlib.crc32(job_id.encode("utf-8")) & 0x7FFFFFFF


def _origins(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where} must be a list of country codes like [US, UK]")
    return tuple(v.strip().upper() for v in value)


def env_origins() -> tuple[str, ...] | None:
    raw = os.environ.get("YTP_ART_ALLOWED_ORIGINS", "").strip()
    return _origins(raw, "YTP_ART_ALLOWED_ORIGINS") if raw else None


def _model_defaults() -> dict[str, str]:
    return {
        "model": os.environ.get("YTP_ART_MODEL", "").strip() or DEFAULT_MODEL,
        "inpaint_model": os.environ.get("YTP_ART_INPAINT_MODEL", "").strip()
        or DEFAULT_INPAINT_MODEL,
        "matte_model": os.environ.get("YTP_ART_MATTE_MODEL", "").strip() or DEFAULT_MATTE_MODEL,
    }


def jobs_from_dict(data: Any, path: Path) -> JobsFile:
    """Validate a parsed jobs file. Paths resolve against the file's folder."""
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a jobs file is a mapping with version, defaults, and jobs")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown top-level keys: {', '.join(sorted(unknown))}")
    if data.get("version") != JOBS_VERSION:
        raise ValueError(f"{path}: version must be {JOBS_VERSION}")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: defaults must be a mapping")
    unknown = set(defaults) - DEFAULT_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown keys in defaults: {', '.join(sorted(unknown))}")

    models: dict[str, ModelInfo] = {}
    for model_id, info in (data.get("models") or {}).items():
        where = f"{path}: models.{model_id}"
        if not isinstance(info, dict):
            raise ValueError(f"{where} must be a mapping with origin, org, and license")
        unknown = set(info) - MODEL_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
        missing = [key for key in ("origin", "org", "license") if not info.get(key)]
        if missing:
            raise ValueError(f"{where} needs {', '.join(missing)} (unknown origin is refused)")
        models[str(model_id)] = ModelInfo(
            id=str(model_id),
            role=str(info.get("role", "")),
            org=str(info["org"]),
            origin=str(info["origin"]).strip().upper(),
            license=str(info["license"]),
            source=str(info.get("source", "")),
        )

    allowed = DEFAULT_ALLOWED_ORIGINS
    if "allowed_origins" in defaults:
        allowed = _origins(defaults["allowed_origins"], f"{path}: defaults.allowed_origins")

    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError(f"{path}: jobs must be a non-empty list")
    base = path.parent
    jobs: list[Job] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_jobs):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: jobs[{index}] must be a mapping")
        job_id = raw.get("id")
        where = f"{path}: job {job_id!r}" if job_id else f"{path}: jobs[{index}]"
        unknown = set(raw) - JOB_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
        if not isinstance(job_id, str) or not ID_PATTERN.match(job_id):
            raise ValueError(f"{where}: id must be letters, digits, '-' or '_'")
        if job_id in seen:
            raise ValueError(f"{where}: duplicate id")
        seen.add(job_id)
        merged = {key: value for key, value in defaults.items() if key != "allowed_origins"}
        merged.update(raw)
        jobs.append(_job(merged, base, where))
    return JobsFile(path=path, jobs=jobs, models=models, allowed_origins=allowed)


def _job(raw: dict[str, Any], base: Path, where: str) -> Job:
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ValueError(f"{where}: kind must be one of {', '.join(KINDS)}, got {kind!r}")
    mode = raw.get("mode", "txt2img")
    if mode not in MODES:
        raise ValueError(f"{where}: mode must be one of {', '.join(MODES)}, got {mode!r}")
    models = _model_defaults()
    job = Job(
        id=raw["id"],
        kind=kind,
        mode=mode,
        prompt=_text(raw.get("prompt"), f"{where}: prompt", required=True),
        negative=_text(raw.get("negative"), f"{where}: negative"),
        seed=_int(raw["seed"], f"{where}: seed", 0, 2**32 - 1)
        if "seed" in raw
        else default_seed(raw["id"]),
        count=_int(raw.get("count", 1), f"{where}: count", 1, MAX_COUNT),
        steps=_int(raw.get("steps", DEFAULT_STEPS), f"{where}: steps", 1, 150),
        cfg=_float(raw.get("cfg", DEFAULT_CFG), f"{where}: cfg", 0.0, 30.0),
        model=_text(raw.get("model") or models["model"], f"{where}: model", required=True),
        inpaint_model=_text(
            raw.get("inpaint_model") or models["inpaint_model"],
            f"{where}: inpaint_model",
            required=True,
        ),
        matte_model=_text(
            raw.get("matte_model") or models["matte_model"], f"{where}: matte_model", required=True
        ),
    )
    alpha = raw.get("alpha", False)
    if not isinstance(alpha, bool):
        raise ValueError(f"{where}: alpha must be true or false")
    job.alpha = alpha
    if raw.get("reference") is not None:
        if mode != "txt2img":
            raise ValueError(f"{where}: reference is for text-to-image jobs; {mode} jobs use init")
        job.reference = _path(raw["reference"], base, f"{where}: reference")
    if mode in ("img2img", "inpaint"):
        if raw.get("init") is None:
            raise ValueError(f"{where}: mode {mode} needs init (the image to change)")
        job.init = _path(raw["init"], base, f"{where}: init")
    elif raw.get("init") is not None:
        raise ValueError(f"{where}: init needs mode img2img or inpaint")
    if mode == "inpaint":
        if raw.get("mask") is None:
            raise ValueError(f"{where}: mode inpaint needs mask (white = repaint)")
        job.mask = _path(raw["mask"], base, f"{where}: mask")
    elif raw.get("mask") is not None:
        raise ValueError(f"{where}: mask needs mode inpaint")
    if "strength" in raw:
        if job.task == "txt2img":
            raise ValueError(f"{where}: strength needs a reference, or mode img2img or inpaint")
        job.strength = _float(raw["strength"], f"{where}: strength", 0.01, 1.0)
    elif job.task == "img2img":
        job.strength = (
            DEFAULT_REFERENCE_STRENGTH if job.reference is not None else DEFAULT_IMG2IMG_STRENGTH
        )
    elif job.task == "inpaint":
        job.strength = DEFAULT_INPAINT_STRENGTH
    if "size" in raw:
        job.width, job.height = parse_size(raw["size"], f"{where}: size")
    elif job.init is not None and job.init.is_file():
        # Fixing an existing image keeps its size unless the job says otherwise.
        with Image.open(job.init) as image:
            job.width, job.height = image.size
    else:
        job.width, job.height = DEFAULT_SIZES[kind]
    return job


def load_jobs(path: Path) -> JobsFile:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"jobs file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    return jobs_from_dict(data, path.resolve())


# --------------------------------------------------------------------------- model policy


def model_info(model_id: str, declared: dict[str, ModelInfo] | None = None) -> ModelInfo | None:
    if declared and model_id in declared:
        return declared[model_id]
    return KNOWN_MODELS.get(model_id)


def check_model(
    model_id: str,
    declared: dict[str, ModelInfo] | None,
    allowed: Iterable[str],
) -> ModelInfo:
    """The model's registry entry, or ValueError when policy refuses it."""
    lowered = model_id.lower()
    for pattern in BLOCKED_MODEL_PATTERNS:
        if pattern in lowered:
            raise ValueError(
                f"model {model_id!r} is blocked ({pattern!r}): no China-origin models, "
                "no IP-Adapter from h94/Tencent, no BiRefNet"
            )
    info = model_info(model_id, declared)
    if info is None:
        raise ValueError(
            f"model {model_id!r} has no declared origin, so it is not used. Add it under "
            "models: in the jobs file with org, origin, and license once the origin is confirmed."
        )
    allowed = tuple(code.upper() for code in allowed)
    if info.origin.upper() not in allowed:
        raise ValueError(
            f"model {model_id!r} is from {info.origin} ({info.org}); allowed origins are "
            f"{', '.join(allowed)}"
        )
    return info


def models_for(jobs: Iterable[Job]) -> list[tuple[str, str]]:
    """(role, model id) pairs the jobs need, in first-use order."""
    needed: list[tuple[str, str]] = []
    for job in jobs:
        pairs = [("inpaint" if job.mode == "inpaint" else "diffusion", job.diffusion_model)]
        if job.alpha:
            pairs.append(("matte", job.matte_model))
        for pair in pairs:
            if pair not in needed:
                needed.append(pair)
    return needed


def check_policy(
    jobs_file: JobsFile, jobs: list[Job], allowed: Iterable[str]
) -> dict[str, ModelInfo]:
    infos: dict[str, ModelInfo] = {}
    for _role, model_id in models_for(jobs):
        infos[model_id] = check_model(model_id, jobs_file.models, allowed)
    return infos


# --------------------------------------------------------------------------- folders


def default_out_dir() -> Path:
    env = os.environ.get("YTP_ART_OUT", "").strip().strip('"')
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(r"C:\AI Eval\YouTube\assets\scenes")
    from ytp.rules import repo_root

    return repo_root().parent / "assets" / "scenes"


def default_models_dir() -> Path:
    env = os.environ.get("YTP_MODELS_DIR", "").strip().strip('"')
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(r"C:\AI Eval\YouTube\models")
    return Path.home() / ".cache" / "ytp-models"


def prepare_model_env(models_dir: Path) -> dict[str, str]:
    """Point the Hugging Face and rembg caches at models_dir (not the user profile)."""
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "HF_HOME": str(models_dir),
        "U2NET_HOME": str(models_dir / "u2net"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
    os.environ.update(values)
    return values


def folder_bytes(folder: Path) -> int:
    total = 0
    if folder.is_dir():
        for root, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    return total


def variant_name(job_id: str, index: int) -> str:
    return job_id if index == 0 else f"{job_id}-v{index + 1}"


def output_paths(job: Job, out_dir: Path) -> list[Path]:
    folder = Path(out_dir) / job.kind
    return [folder / f"{variant_name(job.id, index)}.png" for index in range(job.count)]


def sidecar_path(job: Job, out_dir: Path) -> Path:
    return Path(out_dir) / job.kind / f"{job.id}.json"


def contact_path(out_dir: Path, stem: str, force: bool) -> Path:
    """contact-<stem>.png, or -2, -3... when it exists and --force is off."""
    path = Path(out_dir) / f"contact-{stem}.png"
    number = 2
    while path.exists() and not force:
        path = Path(out_dir) / f"contact-{stem}-{number}.png"
        number += 1
    return path


def conflicts(jobs: Iterable[Job], out_dir: Path) -> list[Path]:
    found = []
    for job in jobs:
        for path in [*output_paths(job, out_dir), sidecar_path(job, out_dir)]:
            if path.exists():
                found.append(path)
    return found


def select_jobs(jobs_file: JobsFile, only: list[str] | None) -> list[Job]:
    if not only:
        return list(jobs_file.jobs)
    ids = {job.id for job in jobs_file.jobs}
    missing = [job_id for job_id in only if job_id not in ids]
    if missing:
        raise ValueError(f"--only: no job with id {', '.join(missing)}")
    return [job for job in jobs_file.jobs if job.id in only]


# --------------------------------------------------------------------------- images


def native_size(width: int, height: int) -> tuple[int, int]:
    """The SDXL bucket whose aspect ratio is closest to width:height."""
    target = width / height
    return min(NATIVE_SIZES, key=lambda size: abs((size[0] / size[1]) / target - 1.0))


def fit(image: Image.Image, width: int, height: int, resample: int | None = None) -> Image.Image:
    """Scale to cover width x height, then center-crop to exactly that size."""
    if image.size == (width, height):
        return image.copy()
    method = Image.Resampling.LANCZOS if resample is None else resample
    return ImageOps.fit(image, (width, height), method=method, centering=(0.5, 0.5))


def flatten(image: Image.Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """RGB copy with any alpha composited over background."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        base = Image.new("RGBA", rgba.size, background + (255,))
        return Image.alpha_composite(base, rgba).convert("RGB")
    return image.convert("RGB")


def load_init(path: Path, size: tuple[int, int]) -> Image.Image:
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    with Image.open(path) as image:
        return fit(flatten(image), *size)


def load_mask(path: Path, size: tuple[int, int]) -> Image.Image:
    """Grayscale mask at size: white repaints, black keeps. Alpha masks use their alpha."""
    if not path.is_file():
        raise FileNotFoundError(f"mask not found: {path}")
    with Image.open(path) as image:
        if image.mode in ("RGBA", "LA"):
            gray = image.getchannel("A")
        else:
            gray = image.convert("L")
        return fit(gray, *size, resample=Image.Resampling.NEAREST)


SOLID_ALPHA = 250  # mattes such as u2net leave most of the subject at 250..254


def alpha_stats(image: Image.Image) -> dict[str, float]:
    """Share of pixels that are clear (alpha 0), edge (1..249), and solid (250..255)."""
    if image.mode != "RGBA":
        return {"transparent": 0.0, "edge": 0.0, "solid": 1.0}
    histogram = image.getchannel("A").histogram()
    total = float(sum(histogram)) or 1.0
    return {
        "transparent": round(histogram[0] / total, 4),
        "edge": round(sum(histogram[1:SOLID_ALPHA]) / total, 4),
        "solid": round(sum(histogram[SOLID_ALPHA:]) / total, 4),
    }


def checkerboard(size: tuple[int, int], cell: int = 12) -> Image.Image:
    board = Image.new("RGB", size, (204, 204, 204))
    draw = ImageDraw.Draw(board)
    for y in range(0, size[1], cell):
        for x in range((y // cell) % 2 * cell, size[0], cell * 2):
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(255, 255, 255))
    return board


def _label_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def contact_sheet(
    items: list[tuple[str, Path]],
    columns: int = 4,
    thumb: tuple[int, int] = (320, 240),
    label_height: int = 28,
) -> Image.Image:
    """Labelled thumbnails; a checkerboard shows through transparent pixels."""
    if not items:
        raise ValueError("contact sheet needs at least one image")
    columns = max(1, min(columns, len(items)))
    rows = (len(items) + columns - 1) // columns
    pad = 8
    cell_w, cell_h = thumb[0] + pad * 2, thumb[1] + label_height + pad * 2
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _label_font(16)
    for index, (label, path) in enumerate(items):
        col, row = index % columns, index // columns
        x0, y0 = col * cell_w + pad, row * cell_h + pad
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
        rgba.thumbnail(thumb, Image.Resampling.LANCZOS)
        tile = checkerboard(rgba.size).convert("RGBA")
        tile = Image.alpha_composite(tile, rgba).convert("RGB")
        sheet.paste(tile, (x0 + (thumb[0] - tile.width) // 2, y0 + (thumb[1] - tile.height) // 2))
        draw.text((x0, y0 + thumb[1] + 6), label, fill=(240, 240, 240), font=font)
    return sheet


# --------------------------------------------------------------------------- backends


class Backend(Protocol):
    """What ``run_art`` needs from the image models. Tests pass a fake."""

    def generate(
        self,
        job: Job,
        seed: int,
        size: tuple[int, int],
        init: Image.Image | None,
        mask: Image.Image | None,
    ) -> Image.Image: ...

    def matte(self, image: Image.Image, model: str) -> Image.Image: ...

    def stats(self) -> dict[str, Any]: ...


def require_art_extra() -> None:
    missing = []
    for module in ("torch", "diffusers", "rembg"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise ArtExtraMissing(f"{EXTRA_HINT} (missing: {', '.join(missing)})")


class DiffusersBackend:
    """SDXL-family pipelines through diffusers in fp16, and rembg for mattes.

    Pipelines load on first use and are cached by (model, task). Img2img and
    inpaint reuse the text-to-image weights through ``from_pipe`` when they use
    the same model id. ``offload`` is ``auto`` (model CPU offload when the GPU
    has under 16 GB), ``model``, ``sequential`` (slowest, smallest), or ``none``.
    """

    def __init__(self, offload: str = "auto", device: str | None = None):
        require_art_extra()
        import torch

        self.torch = torch
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.dtype = torch.float16 if device == "cuda" else torch.float32
        if offload == "auto":
            if device == "cuda":
                total = torch.cuda.get_device_properties(0).total_memory
                offload = "model" if total < 16 * 1024**3 else "none"
            else:
                offload = "none"
        self.offload = offload
        self._pipes: dict[tuple[str, str], Any] = {}
        self._mattes: dict[str, Any] = {}
        self.load_seconds: dict[str, float] = {}
        if device == "cuda":
            torch.cuda.reset_peak_memory_stats()

    def _dtype_key(self) -> str:
        """diffusers 0.42 renamed torch_dtype to dtype (the old name warns)."""
        import diffusers

        parts = re.findall(r"\d+", diffusers.__version__)[:2]
        version = tuple(int(part) for part in parts) if len(parts) == 2 else (0, 0)
        return "dtype" if version >= (0, 42) else "torch_dtype"

    def _from_pretrained(self, cls, model: str):
        kwargs = {self._dtype_key(): self.dtype, "use_safetensors": True}
        if self.dtype == self.torch.float16:
            try:
                return cls.from_pretrained(model, variant="fp16", **kwargs)
            except (OSError, ValueError):
                pass  # no fp16 files in the repo: load the full weights in fp16
        return cls.from_pretrained(model, **kwargs)

    def _place(self, pipe) -> None:
        if self.device == "cuda" and self.offload == "model":
            pipe.enable_model_cpu_offload()
        elif self.device == "cuda" and self.offload == "sequential":
            pipe.enable_sequential_cpu_offload()
        else:
            pipe.to(self.device)
        vae = getattr(pipe, "vae", None)
        if vae is not None and hasattr(vae, "enable_tiling"):
            vae.enable_tiling()  # diffusers 0.42 dropped pipe.enable_vae_tiling()
        elif hasattr(pipe, "enable_vae_tiling"):
            pipe.enable_vae_tiling()

    def _pipe(self, model: str, task: str):
        key = (model, task)
        if key in self._pipes:
            return self._pipes[key]
        from diffusers import (
            AutoPipelineForImage2Image,
            AutoPipelineForInpainting,
            AutoPipelineForText2Image,
        )

        classes = {
            "txt2img": AutoPipelineForText2Image,
            "img2img": AutoPipelineForImage2Image,
            "inpaint": AutoPipelineForInpainting,
        }
        started = time.perf_counter()
        sibling = next((pipe for (m, _t), pipe in self._pipes.items() if m == model), None)
        if sibling is not None:
            pipe = classes[task].from_pipe(sibling)
        else:
            pipe = self._from_pretrained(classes[task], model)
            self._place(pipe)
        pipe.set_progress_bar_config(disable=True)
        self.load_seconds[f"{model} ({task})"] = round(time.perf_counter() - started, 2)
        self._pipes[key] = pipe
        return pipe

    def generate(self, job, seed, size, init, mask):
        pipe = self._pipe(job.diffusion_model, job.task)
        generator = self.torch.Generator(device="cpu").manual_seed(seed)
        kwargs: dict[str, Any] = {
            "prompt": job.prompt,
            "negative_prompt": job.negative or None,
            "num_inference_steps": job.steps,
            "guidance_scale": job.cfg,
            "generator": generator,
        }
        if job.task == "txt2img":
            kwargs.update(width=size[0], height=size[1])
        elif job.task == "img2img":
            kwargs.update(image=init, strength=job.strength)
        else:
            kwargs.update(
                image=init, mask_image=mask, strength=job.strength, width=size[0], height=size[1]
            )
        return pipe(**kwargs).images[0]

    def matte(self, image, model):
        from rembg import new_session, remove

        session = self._mattes.get(model)
        if session is None:
            started = time.perf_counter()
            session = new_session(model)
            self.load_seconds[f"{model} (matte)"] = round(time.perf_counter() - started, 2)
            self._mattes[model] = session
        return remove(image, session=session).convert("RGBA")

    def stats(self):
        out: dict[str, Any] = {
            "device": self.device,
            "dtype": str(self.dtype).replace("torch.", ""),
            "offload": self.offload,
            "load_seconds": dict(self.load_seconds),
            "torch": self.torch.__version__,
        }
        if self.device == "cuda":
            out["gpu"] = self.torch.cuda.get_device_name(0)
            out["vram_peak_gb"] = round(self.torch.cuda.max_memory_allocated() / 1024**3, 2)
            out["vram_reserved_peak_gb"] = round(self.torch.cuda.max_memory_reserved() / 1024**3, 2)
        try:
            import diffusers

            out["diffusers"] = diffusers.__version__
        except ImportError:  # pragma: no cover
            pass
        return out


# --------------------------------------------------------------------------- run


def _load_total(backend: Any) -> float:
    """Seconds the backend has spent loading models so far (0 when it does not say)."""
    loads = getattr(backend, "load_seconds", None)
    return float(sum(loads.values())) if isinstance(loads, dict) else 0.0


@dataclass
class ImageResult:
    path: Path
    seed: int
    seconds: float
    generated_size: tuple[int, int]
    alpha: dict[str, float] | None = None
    load_seconds: float = 0.0


@dataclass
class JobResult:
    job: Job
    images: list[ImageResult]
    sidecar: Path


@dataclass
class RunResult:
    jobs: list[JobResult]
    contact: Path | None
    seconds: float
    backend: dict[str, Any]
    download_bytes: int | None = None
    dry_run: bool = False


def describe(job: Job, out_dir: Path) -> str:
    gen = native_size(job.width, job.height)
    parts = [
        f"{job.id}: {job.kind} {job.task} {job.width}x{job.height} (generate {gen[0]}x{gen[1]})",
        f"seeds {', '.join(str(seed) for seed in job.seeds())}",
        f"steps {job.steps} cfg {job.cfg:g}",
        f"model {job.diffusion_model}",
    ]
    if job.strength is not None:
        parts.append(f"strength {job.strength:g}")
    if job.alpha:
        parts.append(f"alpha via {job.matte_model}")
    lines = ["  ".join(parts)]
    for path in output_paths(job, out_dir):
        lines.append(f"    -> {path}")
    return "\n".join(lines)


def _sidecar(
    job: Job,
    images: list[ImageResult],
    infos: dict[str, ModelInfo],
    backend_stats: dict[str, Any],
    jobs_path: Path,
) -> dict[str, Any]:
    def info(model_id: str) -> dict[str, str]:
        return asdict(infos[model_id])

    data: dict[str, Any] = {
        "id": job.id,
        "kind": job.kind,
        "jobs_file": str(jobs_path),
        "mode": job.mode,
        "task": job.task,
        "prompt": job.prompt,
        "negative": job.negative,
        "size": [job.width, job.height],
        "steps": job.steps,
        "cfg": job.cfg,
        "strength": job.strength,
        "reference": str(job.reference) if job.reference else None,
        "init": str(job.init) if job.init else None,
        "mask": str(job.mask) if job.mask else None,
        "alpha": job.alpha,
        "model": info(job.diffusion_model),
        "matte_model": info(job.matte_model) if job.alpha else None,
        "variants": [
            {
                "file": image.path.name,
                "seed": image.seed,
                "seconds": image.seconds,
                "load_seconds": image.load_seconds,
                "generated_size": list(image.generated_size),
                "alpha_stats": image.alpha,
            }
            for image in images
        ],
        "seconds_total": round(sum(image.seconds for image in images), 2),
        "load_seconds_total": round(sum(image.load_seconds for image in images), 2),
        "runtime": backend_stats,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return data


def run_art(
    jobs_path: Path,
    out_dir: Path | None = None,
    only: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    backend: Backend | None = None,
    backend_factory: Callable[[], Backend] | None = None,
    models_dir: Path | None = None,
    allowed_origins: Iterable[str] | None = None,
    model: str | None = None,
    inpaint_model: str | None = None,
    matte_model: str | None = None,
    log: Callable[[str], None] = print,
) -> RunResult:
    started = time.perf_counter()
    jobs_file = load_jobs(Path(jobs_path))
    jobs = select_jobs(jobs_file, only)
    for job in jobs:
        if model:
            job.model = model
        if inpaint_model:
            job.inpaint_model = inpaint_model
        if matte_model:
            job.matte_model = matte_model
    allowed = tuple(allowed_origins or env_origins() or jobs_file.allowed_origins)
    infos = check_policy(jobs_file, jobs, allowed)
    out_dir = Path(out_dir) if out_dir else default_out_dir()
    models_dir = Path(models_dir) if models_dir else default_models_dir()

    for model_id, info in infos.items():
        log(f"model {model_id}: {info.org}, origin {info.origin}, license {info.license}")
    for job in jobs:
        for path in [job.reference, job.init, job.mask]:
            if path is not None and not path.is_file():
                raise FileNotFoundError(f"job {job.id}: {path} not found")
    existing = [] if force else conflicts(jobs, out_dir)

    if dry_run:
        for job in jobs:
            log(describe(job, out_dir))
        for path in existing:
            log(f"exists (use --force to replace): {path}")
        log(
            f"dry run: {len(jobs)} job(s), {sum(job.count for job in jobs)} image(s), out {out_dir}"
        )
        return RunResult([], None, round(time.perf_counter() - started, 2), {}, None, True)

    if existing:
        listed = "\n  ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite (use --force):\n  {listed}")

    before = folder_bytes(models_dir)
    if backend is None:
        prepare_model_env(models_dir)
        backend = backend_factory() if backend_factory else DiffusersBackend()

    results: list[JobResult] = []
    sheet_items: list[tuple[str, Path]] = []
    for job in jobs:
        gen = native_size(job.width, job.height)
        init = load_init(job.source_image, gen) if job.source_image else None
        mask = load_mask(job.mask, gen) if job.mask else None
        folder = out_dir / job.kind
        folder.mkdir(parents=True, exist_ok=True)
        images: list[ImageResult] = []
        for index, (seed, path) in enumerate(zip(job.seeds(), output_paths(job, out_dir))):
            loaded_before = _load_total(backend)
            t0 = time.perf_counter()
            raw = backend.generate(job, seed, gen, init, mask)
            generated_size = tuple(raw.size)
            image = fit(raw.convert("RGB"), job.width, job.height)
            stats = None
            if job.alpha:
                image = backend.matte(image, job.matte_model)
                stats = alpha_stats(image)
            image.save(path)
            # Model download and load time is reported apart from making the image.
            load = round(_load_total(backend) - loaded_before, 2)
            seconds = round(max(0.0, time.perf_counter() - t0 - load), 2)
            images.append(ImageResult(path, seed, seconds, generated_size, stats, load))
            label = f"{variant_name(job.id, index)}  {job.kind}  seed {seed}"
            sheet_items.append((label, path))
            note = f"  alpha {stats['transparent']:.0%} clear" if stats else ""
            loading = f" + {load:.1f}s model load" if load else ""
            log(f"wrote {path} ({seconds:.1f}s{loading}, seed {seed}){note}")
        sidecar = sidecar_path(job, out_dir)
        data = _sidecar(job, images, infos, backend.stats(), jobs_file.path)
        sidecar.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        results.append(JobResult(job, images, sidecar))

    contact = contact_path(out_dir, jobs_file.stem, force)
    contact_sheet(sheet_items).save(contact)
    log(f"contact sheet {contact}")
    seconds = round(time.perf_counter() - started, 2)
    stats = backend.stats()
    downloaded = max(0, folder_bytes(models_dir) - before)
    count = sum(len(result.images) for result in results)
    per_image = sum(i.seconds for r in results for i in r.images) / max(1, count)
    loading = sum(i.load_seconds for r in results for i in r.images)
    summary = (
        f"done: {count} image(s) in {seconds:.1f}s ({per_image:.1f} s/image, "
        f"{loading:.1f}s loading models)"
    )
    if "vram_peak_gb" in stats:
        summary += f", VRAM peak {stats['vram_peak_gb']:.2f} GB"
    if downloaded:
        summary += f", downloaded {downloaded / 1024**3:.2f} GB to {models_dir}"
    log(summary)
    return RunResult(results, contact, seconds, stats, downloaded)
