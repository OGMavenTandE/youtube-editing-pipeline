"""Scene frames: a small spec file in, finished 1920x1080 PNG frames out.

``ytp scenes`` draws each frame from a background plate, optional art layers,
and Ep01's text look (big word, caption pill, red tags). Then it writes the
still segments for the edit plan so ``ytp finish`` can assemble the episode
without hand edits. Art stays on the PC: the spec names asset ids, and the
paths are resolved locally. See docs/scene-spec.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ytp.ff import ffmpeg_bin, hidden_kwargs
from ytp.plan import EditPlan, NarrationLine, Segment, load_plan, plan_to_dict, save_plan
from ytp.rules import repo_root

try:  # Pillow is a core dependency, but keep the import error readable.
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError as exc:  # pragma: no cover - exercised only without Pillow
    raise ImportError("ytp scenes needs Pillow: pip install Pillow") from exc

SPEC_VERSION = 1
SCENE_PREFIX = "scene-"
FRAME_KINDS = ("key", "pause", "card", "endcard", "motion")
PAN_DIRECTIONS = ("none", "left", "right", "up", "down")
LAYER_ROLES = ("prop", "host", "text")

# Ep01 colors (comp.py). Bright is for tip beats, dark for threat and joke beats.
DEFAULT_PALETTES: dict[str, dict[str, str]] = {
    "bright": {
        "big": "#FFD23F",
        "text": "#FFFFFF",
        "stroke": "#000000",
        "pill": "#000000",
        "label": "#E63946",
        "label_text": "#FFFFFF",
        "strike": "#E63946",
        "source": "#FFFFFF",
        "card": "#14213D",
        "endcard": "#FFFFFF",
    },
    "dark": {
        "big": "#FF8A2B",
        "text": "#FFFFFF",
        "stroke": "#000000",
        "pill": "#000000",
        "label": "#E63946",
        "label_text": "#FFFFFF",
        "strike": "#E63946",
        "source": "#FFFFFF",
        "card": "#111111",
        "endcard": "#FFFFFF",
    },
}

# Ep01 placements (comp.py) on a 1920x1080 frame.
DEFAULT_LAYOUT: dict[str, dict[str, Any]] = {
    "big": {
        "x": 960,
        "y": 150,
        "size": 200,
        "long_size": 160,
        "long_at": 9,
        "min_size": 72,
        "fit": "safe",
    },
    "caption": {
        "x": 960,
        "y": 990,
        "size": 64,
        "long_size": 50,
        "long_at": 40,
        "pad": 40,
        "radius": 24,
        "height": 1.6,
        "margin": 20,
        "min_size": 30,
    },
    "label": {"size": 56, "pad": 25, "height": 90, "radius": 16, "outline": 5},
    "source": {"size": 26, "corner": "right", "margin_x": 36, "margin_y": 18, "stroke": 3},
    "card": {"y": 470, "size": 260},
    "endcard": {"x": 620, "y": 480, "size": 150, "stroke": 11},
    "was": {"scale": 0.55, "gap": 20},
    "strike": {"thickness": 0.12, "overhang": 0.04},
    "safe": {"aspect": "9:16", "margin": 24},
}

FONT_FILE_NAMES = (
    "Inter-VariableFont_opsz,wght.ttf",
    "InterVariable.ttf",
    "Inter[opsz,wght].ttf",
    "Inter-Black.ttf",
    "Inter-Black.otf",
)


# --------------------------------------------------------------------------- spec


@dataclass
class Layer:
    asset: str
    x: float
    y: float
    scale: float = 1.0
    flip: str = "none"
    z: int = 0
    anchor: str = "center"
    role: str = "prop"
    opacity: float = 1.0


@dataclass
class Label:
    text: str
    x: float = 0.0
    y: float = 0.0
    size: int | None = None
    box: tuple[float, float, float, float] | None = None


@dataclass
class Count:
    start: float
    end: float
    duration: float
    decimals: int = 0
    separator: bool = True
    prefix: str = ""
    suffix: str = ""
    ease: str = "linear"

    def value_at(self, progress: float) -> float:
        p = min(1.0, max(0.0, progress))
        if self.ease == "out":
            p = 1 - (1 - p) ** 3
        elif self.ease == "inout":
            p = p * p * (3 - 2 * p)
        return self.start + (self.end - self.start) * p

    def text(self, value: float) -> str:
        sep = "," if self.separator else ""
        number = f"{value:{sep}.{self.decimals}f}"
        return f"{self.prefix}{number}{self.suffix}"

    def text_at(self, progress: float) -> str:
        return self.text(self.value_at(progress))


@dataclass
class Motion:
    zoom_from: float = 1.0
    zoom_to: float = 1.08
    pan: str = "none"

    def to_plan(self) -> dict[str, Any]:
        return {"zoom": [self.zoom_from, self.zoom_to], "pan": self.pan}


@dataclass
class Frame:
    id: str
    kind: str = "key"
    art: str | None = None
    layers: list[Layer] = field(default_factory=list)
    palette: str = "bright"
    accent: str | None = None
    big: str = ""
    big_x: float | None = None
    big_y: float | None = None
    big_size: int | None = None
    strike: bool = False
    was: str | None = None
    count: Count | None = None
    caption: str | None = None
    source: str | None = None
    source_corner: str | None = None
    labels: list[Label] = field(default_factory=list)
    start: float | None = None
    end: float | None = None
    duration: float | None = None
    line: str | None = None
    beat: int | None = None
    motion: Motion | None = None
    file: str | None = None
    # kind: motion (a HyperFrames template, see docs/motion.md)
    template: str | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    motion_layout: str | None = None
    clip_duration: float | None = None


@dataclass
class SceneSpec:
    path: Path | None
    width: int = 1920
    height: int = 1080
    fps: int = 30
    font_path: str | None = None
    font_weight: float = 900
    font_opsz: float = 32
    trim_plates: bool = False
    palettes: dict[str, dict[str, str]] = field(default_factory=dict)
    layout: dict[str, dict[str, Any]] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)
    asset_dirs: list[str] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    series: str | None = None
    episode: int | None = None
    motion: dict[str, Any] = field(default_factory=dict)

    def palette(self, name: str) -> dict[str, str]:
        try:
            return self.palettes[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.palettes))
            raise ValueError(f"unknown palette {name!r} (known: {known})") from exc


def _merge(base: dict[str, dict[str, Any]], extra: Any) -> dict[str, dict[str, Any]]:
    out = {key: dict(value) for key, value in base.items()}
    if not extra:
        return out
    if not isinstance(extra, dict):
        raise ValueError("palettes and layout must be mappings")
    for key, value in extra.items():
        key = str(key).lower()
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a mapping")
        out.setdefault(key, {}).update(value)
    return out


def _opt_float(value: Any, what: str) -> float | None:
    if value is None or value == "" or str(value).upper() == "TBD":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a number, got {value!r}") from exc


def parse_zoom(value: Any) -> tuple[float, float]:
    """``1.0->1.08``, ``[1.0, 1.08]`` or a single number for a fixed zoom."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        pair = (float(value[0]), float(value[1]))
    elif isinstance(value, (int, float)):
        pair = (1.0, float(value))
    elif isinstance(value, str) and "->" in value:
        left, right = value.split("->", 1)
        pair = (float(left), float(right))
    elif isinstance(value, str):
        pair = (1.0, float(value))
    else:
        raise ValueError(f"motion zoom must look like 1.0->1.08, got {value!r}")
    for number in pair:
        if not 1.0 <= number <= 3.0:
            raise ValueError(f"motion zoom must stay between 1.0 and 3.0, got {value!r}")
    return pair


def parse_motion(value: Any, where: str) -> Motion | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{where}: motion must be a mapping like {{zoom: 1.0->1.08, pan: left}}")
    zoom = parse_zoom(value.get("zoom", "1.0->1.08"))
    pan = str(value.get("pan") or "none").lower()
    if pan not in PAN_DIRECTIONS:
        raise ValueError(f"{where}: motion pan must be one of {', '.join(PAN_DIRECTIONS)}")
    return Motion(zoom_from=zoom[0], zoom_to=zoom[1], pan=pan)


def _parse_flip(value: Any) -> str:
    if value in (None, False, "none", ""):
        return "none"
    if value is True:
        return "h"
    text = str(value).lower()
    aliases = {"x": "h", "horizontal": "h", "y": "v", "vertical": "v", "both": "hv"}
    text = aliases.get(text, text)
    if text not in {"h", "v", "hv"}:
        raise ValueError(f"layer flip must be true, h, v or hv, got {value!r}")
    return text


def _parse_layer(item: Any, where: str) -> Layer:
    if not isinstance(item, dict) or "asset" not in item:
        raise ValueError(f"{where}: each layer needs an asset id")
    anchor = str(item.get("anchor") or "center").lower()
    if anchor not in {"center", "topleft", "bottom"}:
        raise ValueError(f"{where}: layer anchor must be center, topleft or bottom")
    role = str(item.get("role") or "prop").lower()
    if role not in LAYER_ROLES:
        raise ValueError(f"{where}: layer role must be one of {', '.join(LAYER_ROLES)}")
    scale = float(item.get("scale", 1.0))
    if scale <= 0:
        raise ValueError(f"{where}: layer scale must be above 0")
    return Layer(
        asset=str(item["asset"]),
        x=float(item.get("x", 960)),
        y=float(item.get("y", 540)),
        scale=scale,
        flip=_parse_flip(item.get("flip")),
        z=int(item.get("z", 0)),
        anchor=anchor,
        role=role,
        opacity=float(item.get("opacity", 1.0)),
    )


def _parse_label(item: Any, where: str) -> Label:
    if isinstance(item, str):
        raise ValueError(f"{where}: a label needs text and x/y or a box")
    if not isinstance(item, dict) or "text" not in item:
        raise ValueError(f"{where}: each label needs text")
    box = item.get("box")
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError(f"{where}: label box must be [x1, y1, x2, y2]")
        box = tuple(float(v) for v in box)
    size = item.get("size")
    return Label(
        text=str(item["text"]),
        x=float(item.get("x", 0)),
        y=float(item.get("y", 0)),
        size=int(size) if size else None,
        box=box,
    )


def _parse_count(item: Any, where: str) -> Count:
    if not isinstance(item, dict):
        raise ValueError(f"{where}: count must be a mapping with from, to and duration")
    try:
        start = float(item["from"])
        end = float(item["to"])
    except KeyError as exc:
        raise ValueError(f"{where}: count needs from and to") from exc
    duration = float(item.get("duration", 1.5))
    if duration <= 0:
        raise ValueError(f"{where}: count duration must be above 0")
    ease = str(item.get("ease") or "linear").lower()
    if ease not in {"linear", "out", "inout"}:
        raise ValueError(f"{where}: count ease must be linear, out or inout")
    return Count(
        start=start,
        end=end,
        duration=duration,
        decimals=int(item.get("decimals", 0)),
        separator=bool(item.get("separator", True)),
        prefix=str(item.get("prefix") or ""),
        suffix=str(item.get("suffix") or ""),
        ease=ease,
    )


def _parse_frame(item: Any, index: int, defaults: dict[str, Any]) -> Frame:
    if not isinstance(item, dict) or "id" not in item:
        raise ValueError(f"frame {index + 1}: needs an id")
    frame_id = str(item["id"])
    where = f"frame {frame_id}"
    kind = str(item.get("kind") or "key").lower()
    if kind not in FRAME_KINDS:
        raise ValueError(f"{where}: kind must be one of {', '.join(FRAME_KINDS)}")
    art = item.get("art", item.get("plate"))
    if kind == "motion":
        return _parse_motion_frame(item, frame_id, defaults)
    if kind != "card" and not art:
        raise ValueError(f"{where}: needs art (a plate asset id), or use kind: card")
    caption = item.get("caption", item.get("cap"))
    count = _parse_count(item["count"], where) if item.get("count") is not None else None
    big = item.get("big")
    big_text = "" if big is None else str(big)
    if count is not None and not big_text:
        big_text = count.text(count.end)
    corner = item.get("source_corner")
    if corner is not None and str(corner).lower() not in {"left", "right"}:
        raise ValueError(f"{where}: source_corner must be left or right")
    motion_value = item.get("motion", defaults.get("motion"))
    beat = item.get("beat")
    return Frame(
        id=frame_id,
        kind=kind,
        art=str(art) if art else None,
        layers=[_parse_layer(layer, where) for layer in item.get("layers") or []],
        palette=str(item.get("palette") or defaults.get("palette") or "bright").lower(),
        accent=str(item["accent"]) if item.get("accent") else None,
        big=big_text,
        big_x=_opt_float(item.get("big_x"), f"{where} big_x"),
        big_y=_opt_float(item.get("big_y"), f"{where} big_y"),
        big_size=int(item["big_size"]) if item.get("big_size") else None,
        strike=bool(item.get("strike", False)),
        was=str(item["was"]) if item.get("was") is not None else None,
        count=count,
        caption=str(caption) if caption else None,
        source=str(item["source"]) if item.get("source") else None,
        source_corner=str(corner).lower() if corner else None,
        labels=[_parse_label(label, where) for label in item.get("labels") or []],
        start=_opt_float(item.get("start"), f"{where} start"),
        end=_opt_float(item.get("end"), f"{where} end"),
        duration=_opt_float(item.get("duration"), f"{where} duration"),
        line=str(item["line"]) if item.get("line") else None,
        beat=int(beat) if beat not in (None, "") else None,
        motion=parse_motion(motion_value, where),
        file=str(item["file"]) if item.get("file") else None,
    )


def _parse_motion_frame(item: dict[str, Any], frame_id: str, defaults: dict[str, Any]) -> Frame:
    where = f"frame {frame_id}"
    template = item.get("template")
    if not template:
        raise ValueError(f"{where}: kind: motion needs a template (see docs/motion.md)")
    variables = item.get("vars") or {}
    if not isinstance(variables, dict):
        raise ValueError(f"{where}: vars must be a mapping")
    layout = item.get("layout")
    if layout is not None and str(layout).lower() not in {"safe", "wide"}:
        raise ValueError(f"{where}: layout must be safe or wide")
    beat = item.get("beat")
    return Frame(
        id=frame_id,
        kind="motion",
        palette=str(item.get("palette") or defaults.get("palette") or "bright").lower(),
        start=_opt_float(item.get("start"), f"{where} start"),
        end=_opt_float(item.get("end"), f"{where} end"),
        duration=_opt_float(item.get("duration"), f"{where} duration"),
        line=str(item["line"]) if item.get("line") else None,
        beat=int(beat) if beat not in (None, "") else None,
        template=str(template),
        vars=dict(variables),
        motion_layout=str(layout).lower() if layout else None,
        clip_duration=_opt_float(item.get("clip_duration"), f"{where} clip_duration"),
    )


def spec_from_dict(data: Any, path: Path | None = None) -> SceneSpec:
    if not isinstance(data, dict):
        raise ValueError("scene spec must be a mapping")
    version = int(data.get("version") or SPEC_VERSION)
    if version != SPEC_VERSION:
        raise ValueError(f"scene spec version must be {SPEC_VERSION}, got {version}")
    size = data.get("size") or [1920, 1080]
    font = data.get("font") or {}
    if isinstance(font, str):
        font = {"path": font}
    plate = data.get("plate") or {}
    assets = data.get("assets") or {}
    if not isinstance(assets, dict):
        raise ValueError("assets must map an asset id to a file path")
    defaults = data.get("defaults") or {}
    frames_raw = data.get("frames") or []
    if not isinstance(frames_raw, list) or not frames_raw:
        raise ValueError("scene spec has no frames")
    frames = [_parse_frame(item, index, defaults) for index, item in enumerate(frames_raw)]
    seen: set[str] = set()
    for frame in frames:
        if frame.id in seen:
            raise ValueError(f"duplicate frame id {frame.id}")
        seen.add(frame.id)
    spec = SceneSpec(
        path=path,
        width=int(size[0]),
        height=int(size[1]),
        fps=int(data.get("fps") or 30),
        font_path=str(font["path"]) if font.get("path") else None,
        font_weight=float(font.get("weight", 900)),
        font_opsz=float(font.get("opsz", 32)),
        trim_plates=bool(plate.get("trim", False)),
        palettes=_merge(DEFAULT_PALETTES, data.get("palettes")),
        layout=_merge(DEFAULT_LAYOUT, data.get("layout")),
        assets={str(key): str(value) for key, value in assets.items()},
        asset_dirs=[str(item) for item in data.get("asset_dirs") or []],
        frames=frames,
        series=str(data["series"]) if data.get("series") else None,
        episode=int(data["episode"]) if data.get("episode") not in (None, "") else None,
        motion=dict(data.get("motion") or {}),
    )
    if not isinstance(data.get("motion") or {}, dict):
        raise ValueError("motion must be a mapping like {hyperframes: ..., fonts_dir: ...}")
    for frame in frames:
        spec.palette(frame.palette)
    if spec.layout["big"].get("fit") not in {"safe", "fixed"}:
        raise ValueError("layout.big.fit must be safe or fixed")
    return spec


def load_spec(path: Path) -> SceneSpec:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return spec_from_dict(data, path)


# --------------------------------------------------------------------------- lookups


def font_candidates(spec: SceneSpec | None = None) -> list[Path]:
    """Explicit paths first, then the usual font folders on Windows, macOS and Linux."""
    paths: list[Path] = []
    if spec is not None and spec.font_path:
        configured = Path(os.path.expandvars(spec.font_path)).expanduser()
        if not configured.is_absolute() and spec.path is not None:
            configured = spec.path.parent / configured
        paths.append(configured)
    env = os.environ.get("YTP_SCENE_FONT", "").strip().strip('"')
    if env:
        paths.append(Path(env))
    return paths


def font_dirs() -> list[Path]:
    dirs: list[Path] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    home = Path.home()
    dirs += [
        home / ".local" / "share" / "fonts",
        home / ".fonts",
        home / "Library" / "Fonts",
        Path("/Library/Fonts"),
        Path("/usr/local/share/fonts"),
        Path("/usr/share/fonts"),
    ]
    return dirs


def find_font(spec: SceneSpec | None = None, dirs: list[Path] | None = None) -> Path:
    explicit = font_candidates(spec)
    for path in explicit:
        if path.is_file():
            return path
    if explicit:
        missing = ", ".join(str(path) for path in explicit)
        raise FileNotFoundError(f"scene font not found: {missing}")
    search = dirs if dirs is not None else font_dirs()
    found: dict[str, Path] = {}
    wanted = {name.lower(): name for name in FONT_FILE_NAMES}
    for folder in search:
        if not folder.is_dir():
            continue
        for root, _subdirs, files in os.walk(folder):
            for name in files:
                key = name.lower()
                if key in wanted and wanted[key] not in found:
                    found[wanted[key]] = Path(root) / name
    for name in FONT_FILE_NAMES:
        if name in found:
            return found[name]
    looked = ", ".join(str(folder) for folder in search)
    raise FileNotFoundError(
        "Inter not found. ytp scenes draws Ep01's look with Inter Black (weight 900). "
        "Install Inter (Google Fonts 'Inter' or rsms.me/inter, the variable TTF), or set "
        f"font.path in the spec or YTP_SCENE_FONT to the .ttf. Looked for "
        f"{', '.join(FONT_FILE_NAMES)} in: {looked}"
    )


class Fonts:
    """Inter at weight 900 and optical size 32, cached per point size."""

    def __init__(self, path: Path, weight: float = 900, opsz: float = 32):
        self.path = path
        self.weight = weight
        self.opsz = opsz
        self._cache: dict[int, ImageFont.FreeTypeFont] = {}

    def __call__(self, size: int) -> ImageFont.FreeTypeFont:
        size = int(size)
        cached = self._cache.get(size)
        if cached is not None:
            return cached
        font = ImageFont.truetype(str(self.path), size)
        try:
            axes = font.get_variation_axes()
        except (OSError, AttributeError):
            axes = []
        if axes:
            values = []
            for axis in axes:
                name = axis.get("name", b"")
                name = name.decode("latin-1") if isinstance(name, bytes) else str(name)
                name = name.lower()
                if "weight" in name or name == "wght":
                    value = self.weight
                elif "optical" in name or name == "opsz":
                    value = self.opsz
                else:
                    value = axis.get("default", axis["minimum"])
                values.append(min(axis["maximum"], max(axis["minimum"], value)))
            font.set_variation_by_axes(values)
        self._cache[size] = font
        return font


def asset_search_dirs(spec: SceneSpec, episode_dir: Path) -> list[Path]:
    """Where relative asset paths are looked up, first match wins.

    Order: folders listed in the spec's ``asset_dirs`` (relative to the episode),
    the episode folder, the spec's folder, ``YTP_SCENE_ASSETS`` (path list),
    ``<repo parent>/assets/scenes``, the episode's ``assets/scenes``, then the
    gitignored in-repo ``assets/scenes`` for local development. Same idea as the
    end card: the art lives on the PC, never in the public repo.
    """
    dirs: list[Path] = []
    for item in spec.asset_dirs:
        folder = Path(os.path.expandvars(item)).expanduser()
        dirs.append(folder if folder.is_absolute() else episode_dir / folder)
    dirs.append(episode_dir)
    if spec.path is not None:
        dirs.append(spec.path.parent)
    env = os.environ.get("YTP_SCENE_ASSETS", "").strip().strip('"')
    if env:
        dirs += [Path(part) for part in env.split(os.pathsep) if part.strip()]
    root = repo_root()
    dirs.append(root.parent / "assets" / "scenes")
    dirs.append(episode_dir / "assets" / "scenes")
    dirs.append(root / "assets" / "scenes")
    unique: list[Path] = []
    for folder in dirs:
        if folder not in unique:
            unique.append(folder)
    return unique


def resolve_asset(asset_id: str, spec: SceneSpec, episode_dir: Path) -> Path:
    value = spec.assets.get(asset_id, asset_id)
    path = Path(os.path.expandvars(value)).expanduser()
    if path.is_absolute():
        if path.is_file():
            return path
        raise FileNotFoundError(f"asset {asset_id}: {path} not found")
    tried = []
    for folder in asset_search_dirs(spec, episode_dir):
        candidate = folder / value.replace("\\", "/")
        tried.append(str(candidate))
        if candidate.is_file():
            return candidate
    if asset_id not in spec.assets:
        raise FileNotFoundError(f"unknown asset id {asset_id!r}: add it to the spec's assets map")
    raise FileNotFoundError(f"asset {asset_id} ({value}) not found. Looked in: {'; '.join(tried)}")


# --------------------------------------------------------------------------- drawing


def safe_zone(width: int, height: int, aspect: str = "9:16") -> tuple[int, int, int, int]:
    """Centered vertical crop box (x1, y1, x2, y2). 1920x1080 gives 656..1264."""
    num, den = (float(part) for part in str(aspect).split(":"))
    zone_w = round(height * num / den)
    x1 = (width - zone_w) // 2
    return (x1, 0, x1 + zone_w, height)


def cover(path: Path, width: int, height: int, trim: bool) -> Image.Image:
    """Scale to fill and center-crop. ``trim`` first cuts near-white borders (comp.py)."""
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    if trim:
        box = ImageOps.invert(image).point(lambda v: 255 if v > 25 else 0).getbbox()
        if box:
            image = image.crop(box)
    scale = max(width / image.width, height / image.height)
    size = (round(image.width * scale) + 1, round(image.height * scale) + 1)
    image = image.resize(size, Image.LANCZOS)
    x = (image.width - width) // 2
    y = (image.height - height) // 2
    return image.crop((x, y, x + width, y + height))


def _layer_image(path: Path, layer: Layer) -> Image.Image:
    with Image.open(path) as opened:
        image = opened.convert("RGBA")
    if layer.scale != 1.0:
        size = (max(1, round(image.width * layer.scale)), max(1, round(image.height * layer.scale)))
        image = image.resize(size, Image.LANCZOS)
    if "h" in layer.flip:
        image = ImageOps.mirror(image)
    if "v" in layer.flip:
        image = ImageOps.flip(image)
    if layer.opacity < 1.0:
        alpha = image.getchannel("A").point(lambda v: round(v * max(0.0, layer.opacity)))
        image.putalpha(alpha)
    return image


def layer_box(layer: Layer, size: tuple[int, int]) -> tuple[int, int, int, int]:
    w, h = size
    if layer.anchor == "topleft":
        x, y = round(layer.x), round(layer.y)
    elif layer.anchor == "bottom":
        x, y = round(layer.x - w / 2), round(layer.y - h)
    else:
        x, y = round(layer.x - w / 2), round(layer.y - h / 2)
    return (x, y, x + w, y + h)


@dataclass
class Rendered:
    image: Image.Image
    boxes: dict[str, Any]
    warnings: list[str]


class SceneRenderer:
    def __init__(self, spec: SceneSpec, episode_dir: Path, fonts: Fonts | None = None):
        self.spec = spec
        self.episode_dir = episode_dir
        self.fonts = fonts or Fonts(find_font(spec), spec.font_weight, spec.font_opsz)
        self.layout = spec.layout
        aspect = str(self.layout["safe"].get("aspect", "9:16"))
        self.safe = safe_zone(spec.width, spec.height, aspect)

    # -- composition

    def base(self, frame: Frame) -> tuple[Image.Image, list[tuple[Layer, tuple]]]:
        spec = self.spec
        palette = spec.palette(frame.palette)
        if frame.art:
            plate = resolve_asset(frame.art, spec, self.episode_dir)
            image = cover(plate, spec.width, spec.height, spec.trim_plates)
        else:
            image = Image.new("RGB", (spec.width, spec.height), palette["card"])
        placed: list[tuple[Layer, tuple]] = []
        if frame.layers:
            canvas = image.convert("RGBA")
            for layer in sorted(frame.layers, key=lambda item: item.z):
                art = _layer_image(resolve_asset(layer.asset, spec, self.episode_dir), layer)
                box = layer_box(layer, art.size)
                _composite_clipped(canvas, art, box)
                placed.append((layer, box))
            image = canvas.convert("RGB")
        return image, placed

    def render(self, frame: Frame, big_text: str | None = None, fit_texts=None) -> Rendered:
        image, placed = self.base(frame)
        boxes, warnings = self.draw_text(image, frame, big_text, fit_texts)
        boxes["layers"] = [(layer.asset, layer.role, box) for layer, box in placed]
        warnings = self.safe_warnings(frame, boxes) + warnings
        return Rendered(image=image, boxes=boxes, warnings=warnings)

    # -- text

    def _fixed_size(self, text: str) -> int:
        cfg = self.layout["big"]
        return int(cfg["size"] if len(text) < int(cfg["long_at"]) else cfg["long_size"])

    def big_metrics(self, frame: Frame, fit_texts: list[str]) -> tuple[int, float, float]:
        """Font size and center for the big word. Fit uses the widest of ``fit_texts``."""
        cfg = self.layout["big"]
        x = frame.big_x if frame.big_x is not None else float(cfg["x"])
        y = frame.big_y if frame.big_y is not None else float(cfg["y"])
        if frame.kind == "card":
            card = self.layout["card"]
            y = frame.big_y if frame.big_y is not None else float(card["y"])
            size = int(card["size"])
        else:
            size = self._fixed_size(max(fit_texts, key=len))
        if frame.big_size:
            return frame.big_size, x, y
        if cfg.get("fit") == "fixed" and frame.kind != "card":
            return size, x, y
        margin = float(self.layout["safe"].get("margin", 24))
        room = (self.safe[2] - self.safe[0]) - 2 * margin
        minimum = int(cfg.get("min_size", 72))
        while size > minimum and max(self.text_width(t, size) for t in fit_texts) > room:
            size -= 2
        return size, x, y

    def text_width(self, text: str, size: int) -> float:
        font = self.fonts(size)
        x1, _y1, x2, _y2 = font.getbbox(text, stroke_width=size // 14)
        return x2 - x1

    def draw_text(self, image: Image.Image, frame: Frame, big_text=None, fit_texts=None):
        draw = ImageDraw.Draw(image)
        palette = self.spec.palette(frame.palette)
        boxes: dict[str, Any] = {}
        warnings: list[str] = []
        text = frame.big if big_text is None else big_text
        if frame.kind == "endcard":
            cfg = self.layout["endcard"]
            if text:
                font = self.fonts(int(cfg["size"]))
                pos = (float(cfg["x"]), float(cfg["y"]))
                stroke = int(cfg["stroke"])
                draw.text(
                    pos,
                    text,
                    font=font,
                    fill=palette["endcard"],
                    anchor="mm",
                    stroke_width=stroke,
                    stroke_fill=palette["stroke"],
                )
                boxes["big"] = draw.textbbox(pos, text, font=font, anchor="mm", stroke_width=stroke)
        elif text:
            fits = list(fit_texts) if fit_texts else [text]
            size, x, y = self.big_metrics(frame, fits)
            color = frame.accent or palette["big"]
            stroke = size // 14
            if frame.was:
                was_cfg = self.layout["was"]
                was_size = max(12, round(size * float(was_cfg["scale"])))
                was_font = self.fonts(was_size)
                was_box = draw.textbbox(
                    (x, y), frame.was, font=was_font, anchor="mm", stroke_width=was_size // 14
                )
                draw.text(
                    (x, y),
                    frame.was,
                    font=was_font,
                    fill=palette["text"],
                    anchor="mm",
                    stroke_width=was_size // 14,
                    stroke_fill=palette["stroke"],
                )
                self._strike(draw, was_box, was_size, palette)
                boxes["was"] = was_box
                y = was_box[3] + float(was_cfg["gap"]) + size * 0.55
            font = self.fonts(size)
            draw.text(
                (x, y),
                text,
                font=font,
                fill=color,
                anchor="mm",
                stroke_width=stroke,
                stroke_fill=palette["stroke"],
            )
            box = draw.textbbox((x, y), text, font=font, anchor="mm", stroke_width=stroke)
            boxes["big"] = box
            boxes["big_size"] = size
            if frame.strike:
                self._strike(draw, box, size, palette)
            if box[0] < 0 or box[2] > self.spec.width:
                warnings.append(f"frame {frame.id}: big word runs off the frame")
        if frame.caption:
            boxes["caption"] = self._caption(draw, frame.caption, palette)
        for label in frame.labels:
            boxes.setdefault("labels", []).append(self._label(draw, label, palette))
        if frame.source:
            boxes["source"] = self._source(draw, frame, palette)
            cap = boxes.get("caption")
            if cap and _overlaps(cap, boxes["source"]):
                warnings.append(f"frame {frame.id}: source line overlaps the caption")
        return boxes, warnings

    def _strike(self, draw, box, size: int, palette) -> None:
        cfg = self.layout["strike"]
        thick = max(4, round(size * float(cfg["thickness"])))
        over = (box[2] - box[0]) * float(cfg["overhang"])
        mid = (box[1] + box[3]) / 2
        rect = (box[0] - over, mid - thick / 2, box[2] + over, mid + thick / 2)
        outline = max(2, thick // 5)
        draw.rounded_rectangle(
            rect,
            radius=thick // 2,
            fill=palette["strike"],
            outline=palette["stroke"],
            width=outline,
        )

    def _caption(self, draw, text: str, palette) -> tuple[float, float, float, float]:
        cfg = self.layout["caption"]
        size = int(cfg["size"] if len(text) < int(cfg["long_at"]) else cfg["long_size"])
        pad = float(cfg["pad"])
        room = self.spec.width - 2 * float(cfg["margin"])
        while size > int(cfg["min_size"]):
            if draw.textlength(text, font=self.fonts(size)) + 2 * pad <= room:
                break
            size -= 2
        font = self.fonts(size)
        tw = draw.textlength(text, font=font)
        h = int(size * float(cfg["height"]))
        cx, cy = float(cfg["x"]), float(cfg["y"])
        box = (cx - tw / 2 - pad, cy - h / 2, cx + tw / 2 + pad, cy + h / 2)
        draw.rounded_rectangle(box, int(cfg["radius"]), fill=palette["pill"])
        draw.text((cx, cy), text, font=font, fill=palette["text"], anchor="mm")
        return box

    def _label(self, draw, label: Label, palette) -> tuple[float, float, float, float]:
        cfg = self.layout["label"]
        outline = int(cfg["outline"])
        radius = int(cfg["radius"])
        if label.box is not None:
            box = label.box
            font = self.fonts(label.size or int(cfg["size"]))
            center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        else:
            font = self.fonts(label.size or int(cfg["size"]))
            pad = float(cfg["pad"])
            w = draw.textlength(label.text, font=font)
            box = (label.x, label.y, label.x + w + 2 * pad, label.y + float(cfg["height"]))
            center = (label.x + pad + w / 2, label.y + float(cfg["height"]) / 2)
        draw.rounded_rectangle(
            box, radius, fill=palette["label"], outline=palette["stroke"], width=outline
        )
        draw.text(center, label.text, font=font, fill=palette["label_text"], anchor="mm")
        return tuple(box)

    def _source(self, draw, frame: Frame, palette) -> tuple[float, float, float, float]:
        cfg = self.layout["source"]
        font = self.fonts(int(cfg["size"]))
        corner = frame.source_corner or str(cfg["corner"])
        y = self.spec.height - float(cfg["margin_y"])
        if corner == "left":
            pos, anchor = (float(cfg["margin_x"]), y), "ld"
        else:
            pos, anchor = (self.spec.width - float(cfg["margin_x"]), y), "rd"
        stroke = int(cfg["stroke"])
        draw.text(
            pos,
            frame.source,
            font=font,
            fill=palette["source"],
            anchor=anchor,
            stroke_width=stroke,
            stroke_fill=palette["stroke"],
        )
        return draw.textbbox(pos, frame.source, font=font, anchor=anchor, stroke_width=stroke)

    # -- safe area

    def safe_warnings(self, frame: Frame, boxes: dict[str, Any]) -> list[str]:
        if frame.kind == "endcard":
            return []
        x1, _y1, x2, _y2 = self.safe
        out: list[str] = []
        for key, what in (("big", "big word"), ("was", "struck number")):
            box = boxes.get(key)
            if box and (box[0] < x1 or box[2] > x2):
                out.append(
                    f"frame {frame.id}: {what} sits outside the 9:16 safe area "
                    f"(x {round(box[0])}..{round(box[2])}, safe {x1}..{x2})"
                )
        for asset, role, box in boxes.get("layers", []):
            if role in {"host", "text"} and (box[0] < x1 or box[2] > x2):
                out.append(
                    f"frame {frame.id}: {role} layer {asset} sits outside the 9:16 safe area "
                    f"(x {box[0]}..{box[2]}, safe {x1}..{x2})"
                )
        return out

    def guides(self, image: Image.Image) -> Image.Image:
        """Debug copy: shade outside the 9:16 safe area and outline it."""
        x1, y1, x2, y2 = self.safe
        out = image.convert("RGBA")
        shade = Image.new("RGBA", out.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(shade)
        draw.rectangle((0, 0, x1 - 1, out.height), fill=(0, 0, 0, 110))
        draw.rectangle((x2, 0, out.width, out.height), fill=(0, 0, 0, 110))
        draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline=(0, 255, 255, 255), width=4)
        out.alpha_composite(shade)
        ImageDraw.Draw(out).text(
            (x1 + 12, 12), "9:16 safe", font=self.fonts(28), fill=(0, 255, 255, 255)
        )
        return out.convert("RGB")


def _composite_clipped(canvas: Image.Image, art: Image.Image, box: tuple) -> None:
    """Paste with alpha. alpha_composite rejects negative offsets, so crop those first."""
    x, y = box[0], box[1]
    left, top = max(0, -x), max(0, -y)
    if left >= art.width or top >= art.height:
        return
    piece = art.crop((left, top, art.width, art.height))
    canvas.alpha_composite(piece, dest=(max(0, x), max(0, y)))


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


# --------------------------------------------------------------------------- count clips


def count_texts(count: Count, fps: int) -> list[str]:
    frames = max(1, round(count.duration * fps))
    if frames == 1:
        return [count.text(count.end)]
    return [count.text_at(index / (frames - 1)) for index in range(frames)]


def write_count_clip(
    renderer: SceneRenderer, frame: Frame, dest: Path, fps: int
) -> tuple[int, list[str]]:
    """Encode the count-up or count-down as a short H.264 clip. Returns frame count."""
    assert frame.count is not None
    texts = count_texts(frame.count, fps)
    fit = [frame.count.text(frame.count.start), frame.count.text(frame.count.end)]
    base, _placed = renderer.base(frame)
    spec = renderer.spec
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{spec.width}x{spec.height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "14",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-an",
        str(dest),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_kwargs())
    warnings: list[str] = []
    try:
        assert proc.stdin is not None
        for text in texts:
            image = base.copy()
            _boxes, warn = renderer.draw_text(image, frame, text, fit)
            warnings.extend(w for w in warn if w not in warnings)
            proc.stdin.write(image.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed writing {dest}: {stderr.strip()}")
    return len(texts), warnings


# --------------------------------------------------------------------------- timing


@dataclass
class Timed:
    frame: Frame
    start: float
    duration: float


def frame_timing(
    frames: list[Frame], narration: list[NarrationLine] | None = None
) -> tuple[list[Timed], list[str]]:
    """Contiguous timeline: each frame holds until the next one starts.

    A frame's start comes from ``start`` or the narration line it names. The last
    frame ends at ``end``, ``start + duration``, its line's end, or the last line.
    Frames with no start (Ep01's TBD rows) are drawn but left out of the plan.
    """
    lines = {line.id: line for line in narration or []}
    warnings: list[str] = []
    starts: list[tuple[Frame, float, float | None]] = []
    untimed: list[str] = []
    for frame in frames:
        start, end = frame.start, frame.end
        if frame.line is not None:
            line = lines.get(frame.line)
            if line is None:
                warnings.append(f"frame {frame.id}: narration line {frame.line} not in the plan")
                if start is None:
                    untimed.append(frame.id)
                    continue
            else:
                start = line.start if start is None else start
                end = line.end if end is None else end
        if start is None:
            untimed.append(frame.id)
            continue
        if end is None and frame.duration is not None:
            end = start + frame.duration
        starts.append((frame, start, end))
    if untimed:
        warnings.append("no start time, left out of the plan: " + ", ".join(untimed))
    starts.sort(key=lambda item: item[1])
    timed: list[Timed] = []
    for index, (frame, start, end) in enumerate(starts):
        if index + 1 < len(starts):
            next_start = starts[index + 1][1]
            if next_start <= start:
                raise ValueError(f"frames {frame.id} and {starts[index + 1][0].id} start together")
            if frame.end is not None and frame.end < next_start - 0.001:
                warnings.append(
                    f"frame {frame.id}: ends at {frame.end:.3f} but the next frame starts at "
                    f"{next_start:.3f}; held until then"
                )
            stop = next_start
        else:
            stop = end
            if stop is None and lines:
                stop = max(line.end for line in lines.values())
            if stop is None or stop <= start:
                raise ValueError(
                    f"last frame {frame.id} needs an end, a duration, or a narration line"
                )
        timed.append(Timed(frame=frame, start=start, duration=round(stop - start, 3)))
    if timed and timed[0].start > 0:
        warnings.append(
            f"first frame {timed[0].frame.id} starts at {timed[0].start:.3f}; held from 0"
        )
        first = timed[0]
        timed[0] = Timed(first.frame, 0.0, round(first.duration + first.start, 3))
    return timed, warnings


def _source_path(path: Path, episode_dir: Path) -> str:
    try:
        return path.resolve().relative_to(episode_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def scene_segments(
    timed: list[Timed],
    outputs: dict[str, Path],
    clips: dict[str, Path],
    episode_dir: Path,
    motion: dict[str, tuple[Path, float]] | None = None,
) -> list[Segment]:
    """Plan segments. ``motion`` maps a motion frame id to its (clip, length); a slot
    longer than the clip holds the clip's last frame (``outputs[id]``) as a still."""
    segments: list[Segment] = []
    motion = motion or {}
    for item in timed:
        frame = item.frame
        narration_ids = [frame.line] if frame.line else []
        start, remaining = item.start, item.duration
        if frame.kind == "motion":
            if frame.id not in motion:
                continue
            path, length = motion[frame.id]
            if length - remaining > 0.05:
                raise ValueError(
                    f"frame {frame.id}: the motion clip is {length:.2f}s but its slot is only "
                    f"{remaining:.2f}s, which would cut its hold. Give it a longer slot or "
                    "shorter copy."
                )
            clip_len = round(min(length, remaining), 3)
            segments.append(
                Segment(
                    id=f"{SCENE_PREFIX}{frame.id}",
                    kind="clip",
                    source=_source_path(path, episode_dir),
                    start=round(start, 3),
                    duration=clip_len,
                    in_point=0.0,
                    out_point=clip_len,
                    palette=frame.palette,
                    beat=frame.beat,
                    narration_ids=list(narration_ids),
                )
            )
            remaining = round(remaining - clip_len, 3)
            if remaining > 0.0005 and frame.id in outputs:
                segments.append(
                    Segment(
                        id=f"{SCENE_PREFIX}{frame.id}-hold",
                        kind="still",
                        source=_source_path(outputs[frame.id], episode_dir),
                        start=round(start + clip_len, 3),
                        duration=remaining,
                        palette=frame.palette,
                        beat=frame.beat,
                        narration_ids=list(narration_ids),
                    )
                )
            continue
        clip = clips.get(frame.id)
        if clip is not None and frame.count is not None:
            clip_len = round(min(frame.count.duration, remaining), 3)
            segments.append(
                Segment(
                    id=f"{SCENE_PREFIX}{frame.id}-count",
                    kind="clip",
                    source=_source_path(clip, episode_dir),
                    start=round(start, 3),
                    duration=clip_len,
                    in_point=0.0,
                    out_point=clip_len,
                    palette=frame.palette,
                    beat=frame.beat,
                    narration_ids=list(narration_ids),
                )
            )
            start, remaining = start + clip_len, round(remaining - clip_len, 3)
        if remaining <= 0.0005:
            continue
        segments.append(
            Segment(
                id=f"{SCENE_PREFIX}{frame.id}",
                kind="still",
                source=_source_path(outputs[frame.id], episode_dir),
                start=round(start, 3),
                duration=round(remaining, 3),
                palette=frame.palette,
                beat=frame.beat,
                narration_ids=list(narration_ids),
                motion=frame.motion.to_plan() if frame.motion else None,
            )
        )
    return segments


def merge_segments(
    plan: EditPlan, segments: list[Segment], replace_ids: set[str] | None = None
) -> list[str]:
    """Replace earlier ``scene-`` segments; keep hand-made ones. Returns warnings.

    With ``replace_ids`` only those ids (and the new segments' ids) are replaced, so
    ``ytp motion --plan`` leaves the still frames from ``ytp scenes`` alone.
    """
    if replace_ids is None:
        kept = [s for s in plan.segments if not s.id.startswith(SCENE_PREFIX)]
    else:
        drop = set(replace_ids) | {s.id for s in segments}
        kept = [s for s in plan.segments if s.id not in drop]
    merged = sorted(kept + segments, key=lambda segment: segment.start)
    warnings: list[str] = []
    for before, after in zip(merged, merged[1:]):
        if before.start + before.duration > after.start + 0.001:
            warnings.append(
                f"segments {before.id} and {after.id} overlap; the picture is a straight concat"
            )
    plan.segments = merged
    return warnings


def fragment_dict(segments: list[Segment]) -> dict[str, Any]:
    plan = EditPlan(series="", segments=segments)
    return {"segments": plan_to_dict(plan)["segments"]}


# --------------------------------------------------------------------------- command


@dataclass
class SceneResult:
    outputs: dict[str, Path]
    clips: dict[str, Path]
    segments: list[Segment]
    warnings: list[str]
    plan_path: Path | None
    fragment_path: Path | None


def episode_for(spec_path: Path) -> Path:
    parent = spec_path.resolve().parent
    return parent.parent if parent.name == "edit" else parent


def frame_filename(frame: Frame) -> str:
    return frame.file or f"{frame.id}.png"


def run_scenes(
    spec_path: Path,
    *,
    episode_dir: Path | None = None,
    out_dir: Path | None = None,
    plan_path: Path | None = None,
    only: list[str] | None = None,
    safe_guides: bool = False,
    clips: bool = True,
    fonts: Fonts | None = None,
    log=print,
    motion: bool = True,
    allow_placeholder: bool = False,
) -> SceneResult:
    spec = load_spec(spec_path)
    episode = (episode_dir or episode_for(spec_path)).resolve()
    out = (out_dir or episode / "frames").resolve()
    out.mkdir(parents=True, exist_ok=True)
    plan: EditPlan | None = None
    narration: list[NarrationLine] = []
    read_plan = plan_path or episode / "edit" / "plan.yaml"
    if read_plan.is_file():
        plan = load_plan(read_plan)
        narration = plan.narration
        if (plan.width, plan.height) != (spec.width, spec.height):
            raise ValueError(
                f"spec size {spec.width}x{spec.height} differs from the plan's "
                f"{plan.width}x{plan.height}"
            )
    elif plan_path is not None:
        raise FileNotFoundError(f"no plan at {plan_path}; run ytp plan import first")
    fps = plan.fps if plan is not None else spec.fps
    renderer = SceneRenderer(spec, episode, fonts)
    warnings: list[str] = []
    outputs: dict[str, Path] = {}
    clip_paths: dict[str, Path] = {}
    selected = [f for f in spec.frames if not only or f.id in only]
    if only:
        missing = sorted(set(only) - {f.id for f in spec.frames})
        if missing:
            raise ValueError(f"no frame with id {', '.join(missing)}")
    motion_clips: dict[str, tuple[Path, float]] = {}
    kit = None
    for frame in selected:
        if frame.kind == "motion":
            from ytp import motion as motion_mod

            if kit is None:
                kit = motion_mod.Toolkit.find(spec.motion, spec.path)
            job = motion_mod.job_for(frame, spec)
            job.fps = fps
            result = motion_mod.ensure_clip(
                job,
                "16x9",
                episode,
                kit,
                allow_placeholder=allow_placeholder,
                render=motion,
                log=log,
            )
            if result is None:
                warnings.append(
                    f"frame {frame.id}: no rendered motion clip; run ytp motion "
                    f"(left out of the plan)"
                )
                continue
            log(f"{'kept' if result.cached else 'wrote'} {result.path}")
            warnings.extend(f"frame {frame.id}: {w}" for w in result.warnings)
            motion_clips[frame.id] = (result.path, result.duration)
            if frame.duration is None and frame.end is None and frame.line is None:
                frame.duration = result.duration
            continue
        fit = None
        if frame.count is not None:
            fit = [frame.count.text(frame.count.start), frame.count.text(frame.count.end)]
        result = renderer.render(frame, fit_texts=fit)
        dest = out / frame_filename(frame)
        dest.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(dest)
        outputs[frame.id] = dest
        log(f"wrote {dest}")
        warnings.extend(result.warnings)
        if safe_guides:
            guide = out / "guides" / frame_filename(frame)
            guide.parent.mkdir(parents=True, exist_ok=True)
            renderer.guides(result.image).save(guide)
        if frame.count is not None and clips:
            clip = dest.with_name(dest.stem + "-count.mp4")
            count, warn = write_count_clip(renderer, frame, clip, fps)
            warnings.extend(w for w in warn if w not in warnings)
            clip_paths[frame.id] = clip
            log(f"wrote {clip} ({count} frames)")
    timed, timing_warnings = frame_timing(selected, narration)
    warnings.extend(timing_warnings)
    for item in timed:
        found = motion_clips.get(item.frame.id)
        if found and item.duration > found[1] + 0.0005:
            from ytp.motion import last_frame_png

            outputs[item.frame.id] = last_frame_png(found[0], found[0].with_suffix(".last.png"))
    segments = scene_segments(timed, outputs, clip_paths, episode, motion=motion_clips)
    fragment_path = None
    written_plan = None
    if plan_path is not None and plan is not None:
        warnings.extend(merge_segments(plan, segments))
        save_plan(plan, plan_path)
        written_plan = plan_path
        log(f"merged {len(segments)} scene segments into {plan_path}")
    else:
        fragment_path = out / "scenes-plan.yaml"
        fragment_path.write_text(
            yaml.safe_dump(fragment_dict(segments), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
            newline="\n",
        )
        log(f"wrote {fragment_path} ({len(segments)} segments)")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return SceneResult(outputs, clip_paths, segments, warnings, written_plan, fragment_path)


__all__ = [
    "Count",
    "Fonts",
    "Frame",
    "Layer",
    "Motion",
    "SceneRenderer",
    "SceneSpec",
    "count_texts",
    "find_font",
    "frame_timing",
    "load_spec",
    "merge_segments",
    "parse_motion",
    "run_scenes",
    "safe_zone",
    "scene_segments",
    "spec_from_dict",
]
