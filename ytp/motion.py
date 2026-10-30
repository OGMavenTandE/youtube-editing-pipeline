"""Motion clips: ``kind: motion`` scene frames rendered through HyperFrames.

A motion frame names a template under ``templates/motion/`` and fills its vars.
Python is the source of truth for layout: it measures every line with the real
fonts (Pillow), wraps and sizes it for the stage, works out the cue times and
the clip length, and lints the copy before anything renders. The template's
HTML, CSS and GSAP code only plays back that plan. HyperFrames turns the page
into an mp4 (NVENC with ``--gpu``, x264 if that fails), the result is checked
frame by frame, and the clip is cached by a hash of everything that went in.
See docs/motion.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import yaml

from ytp.ff import ffmpeg_bin, ffprobe_bin, hidden_kwargs
from ytp.rules import repo_root

MOTION_VERSION = "1"
ASPECTS = ("16x9", "9x16")
LAYOUTS = ("safe", "wide")
COMPOSITION_ID = "ytp-motion"
SAFE_SLICE = (656, 1264)  # x range a 9:16 Shorts crop keeps from a 1920 frame
PORTRAIT_SCALE = 1.4  # 9:16 type is this much bigger than the 16:9 minimums
INK = (15, 17, 21)
INK_HEX = "#0f1115"
# Minimum type sizes at 1080 (16:9). Lint fails anything smaller.
ROLE_MIN = {"label": 26, "body": 40, "subline": 40, "source": 24, "headline": 56, "figure": 40}
SOURCE_MIN_OPACITY = 0.7
MIN_HOLD = 1.5  # seconds the last line holds, at least
HOLD_PER_WORD = 0.3
# Frame 1 must already show the label (a 26px label is a few hundred lit pixels at
# quarter size); this catches an empty or fading-in opening.
FIRST_FRAME_MIN_PIXELS = 40
FIRST_ELEMENT_FRAMES = 6

# face key -> (CSS family, file name, italic)
FACES = {
    "display": ("Fraunces", "Fraunces-Variable.ttf", False),
    "display-italic": ("Fraunces", "Fraunces-Italic-Variable.ttf", True),
    "sans": ("IBM Plex Sans", "IBMPlexSans-Variable.ttf", False),
    "mono": ("IBM Plex Mono", "IBMPlexMono-Regular.ttf", False),
    "mono-bold": ("IBM Plex Mono", "IBMPlexMono-SemiBold.ttf", False),
}
LINE_HEIGHT = {"display": 1.08, "sans": 1.25, "mono": 1.3}

HYPERFRAMES_ENV = {
    "HYPERFRAMES_NO_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "HYPERFRAMES_NO_UPDATE_CHECK": "1",
    "HYPERFRAMES_NO_AUTO_INSTALL": "1",
    "HYPERFRAMES_SKIP_SKILLS": "1",
    "HYPERFRAMES_AGENT": "1",
}

PLACEHOLDER_PATTERNS = [
    r"\bplaceholder\b",
    r"\btodo\b",
    r"\btbd\b",
    r"\blorem\b",
    r"\bipsum\b",
    r"\bx{3,}\b",
    r"\bsample (text|copy|headline|label)\b",
    r"\[[^\]]*\]",
    r"\{\{|\}\}",
]
# Pilot slogans: never ship them again.
PILOT_COPY = ["short prompts. fresh chats. smaller bills.", "three habits. no new tools."]
# The motion set never shows prices: no currency symbols or money words, ever.
CURRENCY_PATTERN = re.compile(
    r"[$\u00a2\u00a3\u00a5\u20ac\u20b9\u20a9\u20bd]"
    r"|\b(usd|eur|gbp|dollars?|cents?|bucks|euros?|pounds? sterling)\b",
    re.IGNORECASE,
)
VENDOR_PATTERN = re.compile(
    r"\b(open\s?ai|chat\s?gpt|gpt-?\d\w*|anthropic|claude|gemini|google|deep\s?mind|"
    r"microsoft|copilot|meta ai|llama|mistral|xai|grok|deepseek|qwen|perplexity|"
    r"cohere|hugging\s?face)\b",
    re.IGNORECASE,
)


class MotionError(ValueError):
    """A motion clip could not be planned, rendered or checked."""


class MotionLintError(MotionError):
    def __init__(self, where: str, errors: list[str]):
        self.errors = errors
        super().__init__(f"{where}: " + "; ".join(errors))


# --------------------------------------------------------------------------- lookups


def templates_dir() -> Path:
    return repo_root() / "templates" / "motion"


def list_templates() -> list[str]:
    root = templates_dir()
    return sorted(p.name for p in root.iterdir() if (p / "template.json").is_file())


@cache
def load_manifest(name: str) -> dict[str, Any]:
    path = templates_dir() / name / "template.json"
    if not path.is_file():
        known = ", ".join(list_templates())
        raise MotionError(f"unknown motion template {name!r} (known: {known})")
    return json.loads(path.read_text(encoding="utf-8"))


def _config_path(value: Any, spec_path: Path | None) -> Path | None:
    if not value:
        return None
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute() and spec_path is not None:
        path = spec_path.parent / path
    return path


def _search_roots() -> list[Path]:
    root = repo_root().resolve()
    return [root, *list(root.parents)[:4]]


def find_fonts_dir(config: dict[str, Any] | None = None, spec_path: Path | None = None) -> Path:
    """motion.fonts_dir, then YTP_MOTION_FONTS, then <ancestor>/assets/fonts."""
    config = config or {}
    explicit = _config_path(config.get("fonts_dir"), spec_path)
    env = os.environ.get("YTP_MOTION_FONTS", "").strip().strip('"')
    candidates = [p for p in (explicit, Path(env) if env else None) if p is not None]
    if not candidates:
        candidates = [base / "assets" / "fonts" for base in _search_roots()]
    needed = sorted({face[1] for face in FACES.values()})
    for folder in candidates:
        if folder.is_dir() and all((folder / name).is_file() for name in needed):
            return folder
    looked = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        "motion fonts not found. Put " + ", ".join(needed) + " (Fraunces and IBM Plex, OFL, "
        "from github.com/google/fonts) in one folder and set motion.fonts_dir in the scenes "
        f"file or YTP_MOTION_FONTS. Looked in: {looked}"
    )


def _hyperframes_target(path: Path) -> Path:
    if path.is_dir():
        for sub in (
            Path("node_modules/hyperframes/bin/hyperframes.mjs"),
            Path("bin/hyperframes.mjs"),
        ):
            if (path / sub).is_file():
                return path / sub
        raise FileNotFoundError(f"no hyperframes CLI under {path}")
    if path.suffix.lower() in {".cmd", ".bat"} or (path.name == "hyperframes" and not path.suffix):
        mjs = path.parent.parent / "hyperframes" / "bin" / "hyperframes.mjs"
        if mjs.is_file():
            return mjs
    return path


def hyperframes_command(config: dict[str, Any] | None = None, spec_path=None) -> list[str]:
    """The argv prefix that runs HyperFrames.

    motion.hyperframes in the scenes file, then YTP_HYPERFRAMES, then a
    ``hyperframes`` npm folder beside the repo (C:\\AI Eval\\YouTube\\hyperframes),
    then ``hyperframes`` on PATH. A .py path runs with this Python (the test mock).
    """
    config = config or {}
    path = _config_path(config.get("hyperframes"), spec_path)
    if path is None:
        env = os.environ.get("YTP_HYPERFRAMES", "").strip().strip('"')
        path = Path(env) if env else None
    if path is None:
        for base in _search_roots():
            folder = base / "hyperframes"
            if (folder / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs").is_file():
                path = folder
                break
    if path is None:
        found = shutil.which("hyperframes")
        if not found:
            raise FileNotFoundError(
                "hyperframes CLI not found. npm install hyperframes in a folder beside the repo, "
                "or set YTP_HYPERFRAMES or motion.hyperframes to it."
            )
        path = Path(found)
    if not path.exists():
        raise FileNotFoundError(f"hyperframes CLI not found at {path}")
    target = _hyperframes_target(path)
    suffix = target.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(target)]
    if suffix in {".mjs", ".js"}:
        node = os.environ.get("YTP_NODE", "").strip().strip('"') or shutil.which("node")
        if not node:
            raise FileNotFoundError("node not found on PATH (HyperFrames needs Node 22+)")
        return [node, str(target)]
    return [str(target)]


def find_gsap(config: dict[str, Any] | None, command: list[str], spec_path=None) -> Path:
    config = config or {}
    explicit = _config_path(config.get("gsap"), spec_path)
    env = os.environ.get("YTP_GSAP", "").strip().strip('"')
    candidates = [p for p in (explicit, Path(env) if env else None) if p is not None]
    if not candidates:
        start = Path(command[-1]).resolve()
        for parent in start.parents:
            candidates.append(parent / "node_modules" / "gsap" / "dist" / "gsap.min.js")
            candidates.append(parent / "gsap" / "dist" / "gsap.min.js")
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "gsap.min.js not found. npm install gsap beside hyperframes, or set YTP_GSAP. "
        "Templates load it locally, never from a CDN."
    )


def hyperframes_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(HYPERFRAMES_ENV)
    return env


def _run(cmd: list[str], cwd: Path, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=hyperframes_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **hidden_kwargs(),
    )


@lru_cache(maxsize=8)
def hyperframes_version(command: tuple[str, ...]) -> str:
    proc = _run([*command, "--version"], Path.cwd(), timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else "unknown"


# --------------------------------------------------------------------------- stage


@dataclass(frozen=True)
class Stage:
    aspect: str
    layout: str
    width: int
    height: int
    x: int
    y: int
    w: int
    h: int
    scale: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "aspect": self.aspect,
            "layout": self.layout,
            "width": self.width,
            "height": self.height,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "scale": self.scale,
        }


def stage_for(aspect: str, layout: str = "safe") -> Stage:
    """Where type may go. 16:9 safe keeps it inside the Shorts slice x 656-1264."""
    if aspect not in ASPECTS:
        raise MotionError(f"aspect must be one of {', '.join(ASPECTS)}, got {aspect!r}")
    if layout not in LAYOUTS:
        raise MotionError(f"layout must be safe or wide, got {layout!r}")
    if aspect == "9x16":
        return Stage("9x16", "portrait", 1080, 1920, 90, 200, 900, 1520, PORTRAIT_SCALE)
    if layout == "wide":
        return Stage("16x9", "wide", 1920, 1080, 160, 100, 1600, 880, 1.0)
    left, right = SAFE_SLICE
    return Stage("16x9", "safe", 1920, 1080, left + 8, 90, right - left - 16, 900, 1.0)


# --------------------------------------------------------------------------- fonts and text


class FontBook:
    def __init__(self, folder: Path):
        self.folder = folder

    def path(self, face: str) -> Path:
        return self.folder / FACES[face][1]

    def family(self, face: str) -> str:
        return FACES[face][0]

    def width(self, text: str, face: str, size: float, weight: int, tracking: float = 0.0) -> float:
        if not text:
            return 0.0
        font = _font(str(self.path(face)), round(size * 4) / 4, weight)
        return float(font.getlength(text)) + tracking * size * len(text)

    def css(self) -> str:
        rules = [
            ("display", "normal", "100 900"),
            ("display-italic", "italic", "100 900"),
            ("sans", "normal", "100 700"),
            ("mono", "normal", "400"),
            ("mono-bold", "normal", "600"),
        ]
        out = []
        for face, style, weight in rules:
            family, name, _ = FACES[face]
            out.append(
                f'@font-face{{font-family:"{family}";src:url("assets/fonts/{name}") '
                f'format("truetype");font-style:{style};font-weight:{weight};font-display:block}}'
            )
        return "\n".join(out)

    def fingerprint(self) -> list[list[Any]]:
        out = []
        for face in sorted(FACES):
            path = self.path(face)
            out.append([path.name, path.stat().st_size])
        return out


@lru_cache(maxsize=256)
def _font(path: str, size: float, weight: int):
    from PIL import ImageFont

    font = ImageFont.truetype(path, size)
    try:
        axes = font.get_variation_axes()
    except Exception:  # static font
        return font
    values = []
    for axis in axes:
        name = axis.get("name", b"")
        name = name.decode("latin-1") if isinstance(name, bytes) else str(name)
        name = name.lower()
        if "weight" in name:
            value = weight
        elif "optical" in name:
            value = size
        elif "width" in name:
            value = 100
        else:
            value = axis.get("default", axis["minimum"])
        values.append(max(axis["minimum"], min(axis["maximum"], value)))
    font.set_variation_by_axes(values)
    return font


def parse_runs(text: str) -> tuple[list[tuple[str, bool, bool]], int]:
    """Split ``Step one: *Find* the waste`` into (word, amber, glue) tokens.

    ``glue`` marks a token that follows the previous one with no space, like the
    period in ``*code*.``.
    """
    tokens: list[tuple[str, bool, bool]] = []
    phrases = 0
    parts = re.split(r"(\*[^*]+\*)", text)
    previous = ""
    for part in parts:
        if not part:
            continue
        joined = not previous[-1:].isspace() and not part[:1].isspace()
        previous = part
        amber = part.startswith("*") and part.endswith("*") and len(part) > 2
        if amber:
            phrases += 1
            part = part[1:-1]
        glue_first = bool(tokens) and joined
        for i, word in enumerate(part.split()):
            tokens.append((word, amber, glue_first and i == 0))
    if text.count("*") % 2:
        raise MotionError(f"unbalanced * in {text!r}")
    return tokens, phrases


def plain(text: str) -> str:
    return join_tokens(parse_runs(text)[0])


def join_tokens(tokens) -> str:
    out = ""
    for word, _amber, glue in tokens:
        out += word if glue or not out else " " + word
    return out


def word_count(text: str) -> int:
    return len(plain(text).split())


def fmt_number(value: float, decimals: int = 0) -> str:
    return f"{value:,.{decimals}f}"


# GSAP's names: power1 is quad, power2 is cubic.
EASES = {
    "power1.out": lambda q: 1 - (1 - q) ** 2,
    "power2.out": lambda q: 1 - (1 - q) ** 3,
}


def count_value(a: float, b: float, t0: float, t1: float, t: float, ease: str) -> float:
    """What the count shows at time t (GSAP's tween, same ease)."""
    if t <= t0:
        return a
    q = min(1.0, (t - t0) / max(1e-9, t1 - t0))
    return a + (b - a) * EASES[ease](q)


def count_show(
    a: float, b: float, t0: float, t1: float, fps: int, decimals: int, ease: str = "power2.out"
) -> float:
    """When a counting number appears: a quarter frame before the first rendered frame
    whose value differs from the start. The start value ("0 tokens") never shows, not even
    for one frame; the count appears already moving."""
    start = fmt_number(a, decimals)
    n = math.floor(t0 * fps)
    while n / fps <= t1 + 1.0 / fps:
        t = n / fps
        if t > t0 + 1e-9 and fmt_number(count_value(a, b, t0, t1, t, ease), decimals) != start:
            return round(t - 0.25 / fps, 4)
        n += 1
    return round(t0, 4)


# --------------------------------------------------------------------------- planning


@dataclass
class MotionJob:
    """One motion frame, resolved: what to render and where."""

    id: str
    template: str
    vars: dict[str, Any]
    layout: str = "safe"
    clip_duration: float | None = None
    fps: int = 30
    series: str | None = None
    episode: int | None = None
    # "aspect/layout" -> element -> largest size, so a set of cards shares one size
    caps: dict[str, dict[str, float]] = field(default_factory=dict)
    # the episode folder: relative file vars (receipt-image's image) resolve here
    base: Path | None = None


@dataclass
class MotionPlan:
    job: MotionJob
    stage: Stage
    payload: dict[str, Any]
    duration: float
    frames: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"motion {self.job.id} ({self.job.template}, {self.stage.aspect})"


class Planner:
    def __init__(self, job: MotionJob, stage: Stage, fonts: FontBook, allow_placeholder: bool):
        self.job = job
        self.stage = stage
        self.fonts = fonts
        self.manifest = load_manifest(job.template)
        self.vars = dict(job.vars)
        self.allow_placeholder = allow_placeholder
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.el: dict[str, dict[str, Any]] = {}
        self.fps = job.fps

    # -- vars
    def resolve_vars(self) -> None:
        schema: dict[str, dict[str, Any]] = self.manifest["vars"]
        for name in self.vars:
            if name not in schema:
                self.errors.append(f"unknown var {name!r} (known: {', '.join(schema)})")
        for name, rule in schema.items():
            value = self.vars.get(name)
            if (value is None or value == "" or value == []) and rule.get("auto") == "series":
                if self.job.series and self.job.episode is not None:
                    value = f"{self.job.series.upper()} \u00b7 EP {int(self.job.episode):02d}"
                    self.vars[name] = value
            if value is None or value == "" or value == []:
                if rule.get("required"):
                    hint = (
                        " (or set series and episode at the top of the scenes file)"
                        if rule.get("auto") == "series"
                        else ""
                    )
                    self.errors.append(f"var {name!r} is required{hint}")
                continue
            kind = rule.get("type", "text")
            if kind == "number":
                try:
                    float(str(value).replace(",", ""))
                except ValueError:
                    self.errors.append(f"var {name!r} must be a number, got {value!r}")
            elif kind == "lines" and not isinstance(value, list):
                self.errors.append(f"var {name!r} must be a list")
            elif kind == "items":
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(
                        (isinstance(item, str) and item.strip())
                        or (isinstance(item, dict) and item.get("label"))
                        for item in value
                    )
                ):
                    self.errors.append(
                        f"var {name!r} must be a list of lines (text, or {{label, amount}})"
                    )
            elif kind == "text" and not isinstance(value, (str, int, float)):
                self.errors.append(f"var {name!r} must be text")
        if self.errors:
            return
        self.lint_copy(schema)

    def texts(self, schema: dict[str, dict[str, Any]]) -> list[tuple[str, str, str]]:
        """(var, role, text) for every visible string."""
        out = []
        for name, rule in schema.items():
            value = self.vars.get(name)
            if value is None or rule.get("visible") is False:
                continue  # file paths and timings never reach the screen
            role = rule.get("role", "body")
            if isinstance(value, list):
                for item in value:
                    out.append((name, role, str(item["label"] if isinstance(item, dict) else item)))
            else:
                out.append((name, role, str(value)))
        return out

    def lint_copy(self, schema: dict[str, dict[str, Any]]) -> None:
        texts = self.texts(schema)
        figures = bool(self.manifest.get("figures"))
        for name, role, text in texts:
            lowered = text.lower()
            if VENDOR_PATTERN.search(text):
                self.errors.append(
                    f"var {name!r} names an AI vendor ({VENDOR_PATTERN.search(text).group(0)}); "
                    "say 'a major AI lab'"
                )
            hits = [p for p in PLACEHOLDER_PATTERNS if re.search(p, lowered)]
            if any(slogan in lowered for slogan in PILOT_COPY):
                hits.append("pilot copy")
            if hits:
                message = f"var {name!r} looks like placeholder or sample copy: {text!r}"
                if self.allow_placeholder:
                    self.warnings.append(message + " (allowed by --allow-placeholder)")
                else:
                    self.errors.append(message + " (pass --allow-placeholder for a test render)")
            if CURRENCY_PATTERN.search(text):
                self.errors.append(
                    f"var {name!r} shows a price or currency "
                    f"({CURRENCY_PATTERN.search(text).group(0)}); motion copy never shows money"
                )
            if "\u2014" in text:
                self.errors.append(f"var {name!r} has an em dash; the on-screen style bans them")
            if role != "label" and re.search(r"\d", plain(text)):
                figures = True
        if figures and not str(self.vars.get("source") or "").strip():
            self.errors.append("figures on screen need a source line: set var 'source'")

    # -- fitting
    def spec(self, name: str) -> dict[str, Any]:
        elements = self.manifest["elements"]
        if name not in elements:
            raise MotionError(f"{self.job.template}: no element {name!r} in template.json")
        return elements[name]

    def size_range(self, spec: dict[str, Any]) -> tuple[float, float]:
        size = spec["size"]
        low, high = (size, size) if isinstance(size, (int, float)) else (size[0], size[1])
        if self.stage.aspect == "9x16":
            low, high = low * self.stage.scale, high * self.stage.scale
        elif self.stage.layout == "wide":
            high = high * float(spec.get("wide", 1.3))
        return float(low), float(high)

    def lint_spec(self, name: str, spec: dict[str, Any]) -> None:
        role = spec["role"]
        low, _ = self.size_range(spec)
        need = ROLE_MIN[role] * self.stage.scale
        if low + 1e-6 < need:
            self.errors.append(
                f"element {name}: {role} type {low:.0f}px is under the {need:.0f}px minimum"
            )
        if role == "label" and float(spec.get("tracking", 0)) <= 0:
            self.errors.append(f"element {name}: labels must be letter-spaced")
        if role == "source" and float(spec.get("opacity", 1)) < SOURCE_MIN_OPACITY:
            self.errors.append(f"element {name}: source opacity must be at least 70%")

    def fit(
        self,
        name: str,
        text: str,
        width: float | None = None,
        amber: str = "none",
        max_size: float | None = None,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        self.lint_spec(name, spec)
        tokens, phrases = parse_runs(str(text))
        if amber == "one" and phrases != 1:
            self.errors.append(
                f"{name}: a headline needs exactly one *amber* word, found {phrases} in {text!r}"
            )
        elif amber == "optional" and phrases > 1:
            self.errors.append(f"{name}: one *amber* word at most, found {phrases}")
        elif amber == "optional" and phrases == 0:
            self.warnings.append(f"{name}: no *amber* word in {text!r}")
        elif amber == "none" and phrases:
            self.errors.append(f"{name}: amber is for the headline only; drop the * in {text!r}")
        if spec.get("upper"):
            tokens = [(word.upper(), flag, glue) for word, flag, glue in tokens]
        font = spec["font"]
        weight = int(spec.get("weight", 400))
        tracking = float(spec.get("tracking", 0))
        max_lines = int(spec.get("max_lines", 1))
        avail = (width if width is not None else self.stage.w) * 0.97
        low, high = self.size_range(spec)
        cap = (self.job.caps.get(f"{self.stage.aspect}/{self.stage.layout}") or {}).get(name)
        for limit in (cap, max_size):
            if limit is not None:
                high = max(low, min(high, float(limit)))
        size = high
        while True:
            lines = self._wrap(tokens, font, weight, size, tracking, avail)
            if lines is not None and len(lines) <= max_lines:
                break
            if size <= low:
                self.errors.append(
                    f"{name}: {plain(str(text))!r} does not fit {max_lines} line(s) "
                    f"of {avail:.0f}px at the {low:.0f}px minimum; shorten it"
                )
                lines = lines or [[(join_tokens(tokens), False, False)]]
                break
            size = max(low, size - 2)
        if lines and len(lines) > 1:
            lines = self._balance(tokens, font, weight, size, tracking, avail, len(lines))
        fitted = {
            "size": round(size, 2),
            "family": self.fonts.family(font),
            "face": font,
            "weight": weight,
            "tracking": round(tracking * size, 2),
            "lh": float(spec.get("lh", LINE_HEIGHT[font.split("-")[0]])),
            "opacity": float(spec.get("opacity", 1)),
            "role": spec["role"],
            "lines": [[[w, a, g] for w, a, g in line] for line in lines],
            "width": round(
                max(
                    (self._line_width(line, font, weight, size, tracking) for line in lines),
                    default=0.0,
                ),
                1,
            ),
            "words": sum(1 for _w, _a, g in tokens if not g),
        }
        fitted["height"] = round(len(fitted["lines"]) * size * fitted["lh"], 1)
        self.el[name] = fitted
        return fitted

    def _token_width(self, word, amber, font, weight, size, tracking) -> float:
        face = "display-italic" if amber and font == "display" else font
        if font == "mono" and weight >= 600:
            face = "mono-bold"
        return self.fonts.width(word, face, size, weight, tracking)

    def _line_width(self, line, font, weight, size, tracking) -> float:
        if not line:
            return 0.0
        space = self.fonts.width(
            " ", "mono" if font.startswith("mono") else font, size, weight, tracking
        )
        widths = [self._token_width(w, a, font, weight, size, tracking) for w, a, _g in line]
        spaces = sum(1 for _w, _a, g in line[1:] if not g)
        return sum(widths) + space * spaces

    def _wrap(self, tokens, font, weight, size, tracking, avail):
        groups: list[list[tuple[str, bool, bool]]] = []
        for token in tokens:
            if token[2] and groups:
                groups[-1].append(token)
            else:
                groups.append([token])
        lines: list[list[tuple[str, bool, bool]]] = []
        current: list[tuple[str, bool, bool]] = []
        for group in groups:
            trial = [*current, *group]
            if self._line_width(trial, font, weight, size, tracking) <= avail:
                current = trial
            elif not current:
                return None
            else:
                lines.append(current)
                if self._line_width(group, font, weight, size, tracking) > avail:
                    return None
                current = list(group)
        if current:
            lines.append(current)
        return lines

    def _balance(self, tokens, font, weight, size, tracking, avail, count):
        """Narrowest wrap with the same line count, so no line is a lone word."""
        best = self._wrap(tokens, font, weight, size, tracking, avail)
        low, high = avail * 0.4, avail
        for _ in range(14):
            mid = (low + high) / 2
            trial = self._wrap(tokens, font, weight, size, tracking, mid)
            if trial is not None and len(trial) <= count:
                best, high = trial, mid
            else:
                low = mid
        return best

    def hold(self, words: int) -> float:
        return max(MIN_HOLD, HOLD_PER_WORD * words)

    def frame(self, n: int = 1) -> float:
        return n / self.fps

    def number(self, name: str) -> float:
        return float(str(self.vars[name]).replace(",", ""))


def _plan_count_up(p: Planner) -> tuple[dict, dict, float]:
    v = p.vars
    p.fit("label", v["label"])
    p.fit("title", v["title"], amber="one")
    p.fit("before_label", v["before_label"])
    p.fit("after_label", v["after_label"])
    decimals = int(v.get("decimals") or 0)
    unit = str(v["unit"]).strip()
    before, after = p.number("before"), p.number("after")
    rows = []
    biggest = max(before, after, 1e-9)
    for key, value in (("before", before), ("after", after)):
        text = f"{fmt_number(value, decimals)} {unit}".strip()
        num = p.fit(f"{key}_num", text)
        pad = round(num["size"] * 0.45)
        fill = max(value / biggest, 0.014) * p.stage.w
        inside = fill >= num["width"] + 2 * pad
        x = pad if inside else fill + pad
        if not inside and x + num["width"] > p.stage.w:
            inside, x = True, pad
        rows.append(
            {
                "key": key,
                "value": value,
                "pct": round(fill / p.stage.w * 100, 3),
                "inside": inside,
                "num_x": round(x, 1),
                "bar_h": round(num["size"] * 1.6),
            }
        )
    p.fit("source", v["source"])
    tag = str(v.get("tag") or "").strip()
    if tag:
        p.fit("tag", tag)
    cues = {
        "label": 0.0,
        "title": 0.15,
        "before_row": 0.5,
        "source": 0.6,
        "before_count": [1.0, 2.6],
        "after_row": 2.7,
        "after_count": [3.0, 4.0],
        "before_show": count_show(0, rows[0]["value"], 1.0, 2.6, p.job.fps, decimals),
        "after_show": count_show(0, rows[1]["value"], 3.0, 4.0, p.job.fps, decimals),
        "tag": 4.15 if tag else None,
    }
    land = 4.5 if tag else 4.0
    last_words = p.el["tag"]["words"] if tag else p.el["after_num"]["words"]
    data = {"rows": rows, "unit": unit, "decimals": decimals}
    return data, cues, land + p.hold(last_words)


def _plan_kinetic(p: Planner) -> tuple[dict, dict, float]:
    v = p.vars
    p.fit("label", v["label"])
    lines = [str(line) for line in v["lines"]]
    amber_word = str(v["amber_word"]).strip().strip("*")
    key = re.sub(r"\W", "", amber_word.lower())
    marked = False
    out_lines = []
    for line in lines:
        if "*" in line:
            marked = True
            out_lines.append(line)
            continue
        if not marked:
            words = line.split()
            for i, word in enumerate(words):
                core = re.sub(r"\W", "", word.lower())
                if core == key:
                    lead = re.match(r"^\W*", word).group(0)
                    tail = re.search(r"\W*$", word).group(0)
                    words[i] = f"{lead}*{word[len(lead) : len(word) - len(tail)]}*{tail}"
                    marked = True
                    break
            line = " ".join(words)
        out_lines.append(line)
    if not marked:
        p.errors.append(f"amber_word {amber_word!r} is not in any line")
    beats = []
    for i, line in enumerate(out_lines):
        fitted = _fit_as(p, "line", f"line{i}", line)
        beats.append({"id": f"line{i}", "words": fitted["words"]})
    sub = str(v.get("subline") or "").strip()
    if sub:
        p.fit("subline", sub)
        beats.append({"id": "subline", "words": p.el["subline"]["words"]})
    enter, leave, gap = p.frame(10), p.frame(7), p.frame(2)
    t = p.frame(1)
    cue_beats = []
    end = t
    for i, beat in enumerate(beats):
        last = i == len(beats) - 1
        start = t
        if last:
            cue_beats.append({"id": beat["id"], "in": round(start, 4), "out": None})
            end = start + enter + p.hold(beat["words"])
        else:
            out = start + enter + max(0.9, HOLD_PER_WORD * beat["words"])
            cue_beats.append({"id": beat["id"], "in": round(start, 4), "out": round(out, 4)})
            t = out + leave + gap
    cues = {"label": 0.0, "beats": cue_beats, "enter": enter, "exit": leave}
    return {"count": len(beats)}, cues, end


def _fit_as(p: Planner, spec_name: str, el_name: str, text: str) -> dict[str, Any]:
    fitted = p.fit(spec_name, text, amber="optional" if "*" in text else "none")
    p.el[el_name] = p.el.pop(spec_name)
    return fitted


def _plan_chapter_card(p: Planner) -> tuple[dict, dict, float]:
    v = p.vars
    p.fit("label", v["label"])
    title = p.fit("title", v["title"], amber="optional")
    sub = str(v.get("subline") or "").strip()
    if sub:
        p.fit("subline", sub)
    if v.get("source"):
        p.fit("source", v["source"])
    stagger = 0.07
    title_in = 0.2
    title_end = title_in + stagger * max(0, title["words"] - 1) + 0.5
    cues = {
        "label": 0.0,
        "rule": 0.0,
        "title": title_in,
        "stagger": stagger,
        "subline": title_end + 0.05 if sub else None,
        "source": 0.6,
    }
    land = title_end + (0.45 if sub else 0)
    words = title["words"] + (p.el["subline"]["words"] if sub else 0)
    return {}, cues, land + p.hold(words)


def _plan_receipt(p: Planner) -> tuple[dict, dict, float]:
    """The paper hangs from a fixed header and grows down one line at a time. It never
    scrolls: every line must be on screen when the total lands, or the plan fails."""
    v = p.vars
    s = p.stage
    k = s.scale
    gap = round(28 * k)
    label = p.fit("label", v["label"]) if v.get("label") else None
    title = p.fit("title", v["title"], amber="one") if v.get("title") else None
    source = p.fit("source", v["source"])
    unit = str(v["unit"]).strip()
    decimals = int(v.get("decimals") or 0)
    pad = round(26 * k)
    inner = s.w - 2 * pad
    line_gap = 12 * k
    y = pad
    header = None
    header_rule_y = None
    if v.get("header"):
        header = _refit(p, "header", "header", str(v["header"]), inner)
        y += header["height"] + 14 * k
        header_rule_y = round(y, 1)
        y += 3 + 16 * k
    paper0 = round(y if header else pad * 1.6, 1)
    items = []
    total_sum = 0.0
    amounts = 0
    for i, raw in enumerate(v["lines"]):
        item = raw if isinstance(raw, dict) else {"label": raw}
        has_amount = item.get("amount") not in (None, "")
        width = inner
        amount = None
        amt = None
        if has_amount:
            amounts += 1
            amount = float(str(item["amount"]).replace(",", ""))
            total_sum += amount
            amount_text = fmt_number(amount, decimals)
            amt = _fit_as(p, "amount", f"amount{i}", amount_text)
            if amt["width"] + 24 > inner * 0.45:
                p.errors.append(f"receipt amount {amount_text} is too wide")
            width = inner - amt["width"] - 24 * k
        lab = _refit(p, "item", f"item{i}", str(item["label"]), width)
        h = max(lab["height"], amt["height"] if amt else 0)
        items.append({"y": round(y, 1), "h": round(h, 1), "amount": amount})
        y += h + line_gap
    total = p.number("total")
    if amounts and abs(total - total_sum) > 0.5:
        p.warnings.append(
            f"receipt lines add up to {fmt_number(total_sum, decimals)}, total says "
            f"{fmt_number(total, decimals)}"
        )
    rule_y = y + 4 * k
    total_y = rule_y + 20 * k
    total_text = f"{fmt_number(total, decimals)} {unit}".strip()
    # One row ("TOTAL:   150,000 TOKENS") when both fit at 40px or more, else stacked.
    spec = p.spec("total_amount")
    low, high = p.size_range(spec)
    one_row = None
    size = high
    while size >= low - 1e-6:
        tl_w = p._line_width(parse_runs(str(v["total_label"]))[0], "mono", 600, size, 0.0)
        ta_w = p._line_width(parse_runs(total_text)[0], "mono", 600, size, 0.0)
        if tl_w + ta_w + 24 * k <= inner * 0.97:
            one_row = size
            break
        size -= 2
    if one_row is not None:
        tamt = p.fit("total_amount", total_text, max_size=one_row)
        tlab = _refit(
            p, "total_label", "total_label", str(v["total_label"]), inner, max_size=tamt["size"]
        )
        amount_y = total_y
        content_h = total_y + max(tlab["height"], tamt["height"]) + pad
    else:
        tamt = p.fit("total_amount", total_text)
        tlab = _refit(p, "total_label", "total_label", str(v["total_label"]), inner)
        amount_y = total_y + tlab["height"] + 4 * k
        content_h = amount_y + tamt["height"] + pad
    above = sum(el["height"] + gap for el in (label, title) if el)
    room = s.h - source["height"] - gap - above
    if content_h > room + 0.5:
        p.errors.append(
            f"receipt is {content_h - room:.0f}px too tall to show every line at the total "
            f"({len(items)} lines); shorten lines, drop the title, or use fewer lines"
        )
    top = max(0.0, (s.h - source["height"] - gap - above - content_h) / 2)
    step = 0.45
    first = 0.6
    item_cues = []
    for i, item in enumerate(items):
        t = round(first + i * step, 3)
        cue = {"t": t, "paper_h": round(item["y"] + item["h"] + pad, 1)}
        if item["amount"] is not None:
            cue["show"] = count_show(
                0, item["amount"], t + 0.05, t + 0.35, p.job.fps, decimals, "power1.out"
            )
        item_cues.append(cue)
    total_t = first + len(items) * step + 0.15
    cues = {
        "label": 0.0,
        "title": 0.15 if label else 0.0,
        "source": 0.5,
        "items": item_cues,
        "rule": round(total_t, 3),
        "total": round(total_t + 0.2, 3),
        "total_paper_h": round(content_h, 1),
    }
    data = {
        "items": items,
        "pad": pad,
        "gap": gap,
        "top": round(top, 1),
        "paper0": paper0,
        "header_rule_y": header_rule_y,
        "rule_y": round(rule_y, 1),
        "total_y": round(total_y, 1),
        "amount_y": round(amount_y, 1),
        "one_row": one_row is not None,
        "content_h": round(content_h, 1),
        "room": round(room, 1),
        "decimals": decimals,
        "total": total,
        "unit": unit,
    }
    land = total_t + 0.2 + 0.35
    words = tlab["words"] + tamt["words"]
    return data, cues, land + p.hold(words)


# 16:9 paper top. Studio's spec says y 60, but then the slot and the frame-1 lip sit above the
# stage (y 90) and frame 1 reads as empty; at 72 the lip reaches 10px into the stage and the
# tallest Studio strip (918px) ends on the stage bottom (990).
RECEIPT_IMAGE_TOP = 72
RECEIPT_IMAGE_WIDTH = 608  # Studio's 1x strip width (the Shorts slice)
RECEIPT_IMAGE_LIP = 28  # image px of blank paper out of the slot on frame 1 (first ink is row 31)


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG header (no Pillow needed)."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise MotionError(f"{path} is not a PNG")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def image_path(job: MotionJob, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and job.base is not None:
        path = job.base / path
    return path


def receipt_image_var(vars: dict, aspect: str) -> Any:
    """The image for this aspect: `image_9x16` (e.g. Studio's 2x strip) when set, else `image`."""
    if aspect == "9x16" and vars.get("image_9x16") not in (None, ""):
        return vars["image_9x16"]
    return vars["image"]


def _plan_receipt_image(p: Planner) -> tuple[dict, dict, float]:
    """Studio's finished receipt prints in from a fixed top edge, then holds. The image
    is the art: shown 1:1 in 16:9 (the strip is the 608px Shorts slice), and in 9:16 at
    one scale that fits the safe area. Never wider than the slice or the safe area."""
    v = p.vars
    s = p.stage
    path = image_path(p.job, receipt_image_var(v, s.aspect))
    if not path.is_file():
        p.errors.append(f"image {path} not found")
        return {}, {}, 0.0
    try:
        iw, ih = png_size(path)
    except MotionError as exc:
        p.errors.append(str(exc))
        return {}, {}, 0.0
    print_s = float(v.get("print") if v.get("print") not in (None, "") else 3.0)
    hold_s = float(v.get("hold") if v.get("hold") not in (None, "") else 2.0)
    if not 0.5 <= print_s <= 10:
        p.errors.append(f"print {print_s}s is out of range (0.5 to 10 s)")
    if hold_s < 0:
        p.errors.append("hold must be 0 or more seconds")
    slot_h = round(10 * s.scale)
    if s.aspect == "16x9":
        left, right = SAFE_SLICE
        top = RECEIPT_IMAGE_TOP
        max_w = right - left
        max_h = s.y + s.h - top
    else:
        top = s.y + slot_h
        max_w, max_h = s.w, s.h - slot_h
    scale = min(max_w / iw, max_h / ih)
    if s.aspect == "16x9":
        scale = min(1.0, scale)  # never enlarged in 16:9; the strip is drawn at size
    if scale < 1.0 - 1e-6 and s.aspect == "16x9":
        p.warnings.append(
            f"image {iw}x{ih} is shrunk to {scale:.0%} to fit the safe slice "
            f"({max_w}x{max_h}); Studio's strips are 608 wide and at most {max_h} tall"
        )
    w, h = round(iw * scale, 1), round(ih * scale, 1)
    x = round((s.width - w) / 2, 1)
    # the lip is set in 608px-strip px, so a 2x strip shows the same blank paper
    lip = round(min(h, RECEIPT_IMAGE_LIP * w / RECEIPT_IMAGE_WIDTH), 1)
    slot_w = w if s.aspect == "16x9" else round(w + 24 * s.scale, 1)
    data = {
        "image": path.name,
        "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "image_size": [iw, ih],
        "scale": round(scale, 4),
        "x": x,
        "y": top,
        "w": w,
        "h": h,
        "lip": lip,
        "slot_x": round((s.width - slot_w) / 2, 1),
        "slot_y": top - slot_h,
        "slot_w": slot_w,
        "slot_h": slot_h,
    }
    cues = {"print": round(print_s, 3), "land": round(print_s, 3)}
    return data, cues, print_s + hold_s


def _refit(
    p: Planner,
    spec_name: str,
    el_name: str,
    text: str,
    width: float,
    max_size: float | None = None,
) -> dict[str, Any]:
    p.fit(spec_name, text, width=width, max_size=max_size)
    fitted = p.el.pop(spec_name)
    p.el[el_name] = fitted
    return fitted


PLANNERS = {
    "count-up": _plan_count_up,
    "kinetic": _plan_kinetic,
    "chapter-card": _plan_chapter_card,
    "receipt-unroll": _plan_receipt,
    "receipt-image": _plan_receipt_image,
}


def plan_motion(
    job: MotionJob, aspect: str, fonts: FontBook, allow_placeholder: bool = False
) -> MotionPlan:
    layout = "safe" if aspect == "9x16" else job.layout
    stage = stage_for(aspect, layout)
    p = Planner(job, stage, fonts, allow_placeholder)
    p.resolve_vars()
    data: dict[str, Any] = {}
    cues: dict[str, Any] = {}
    duration = 0.0
    if not p.errors:
        planner = PLANNERS.get(job.template)
        if planner is None:
            raise MotionError(f"template {job.template} has no planner in ytp/motion.py")
        data, cues, duration = planner(p)
    if job.clip_duration is not None:
        if duration and job.clip_duration + 1e-6 < duration:
            p.errors.append(
                f"clip_duration {job.clip_duration:.2f}s is shorter than the {duration:.2f}s "
                "the template needs (entries plus the end hold)"
            )
        duration = max(duration, job.clip_duration)
    frames = max(1, int(-(-round(duration * job.fps, 6) // 1)))
    duration = round(frames / job.fps, 4)
    payload = {
        "id": job.id,
        "template": job.template,
        "fps": job.fps,
        "duration": render_duration(frames, job.fps),
        "frames": frames,
        "stage": stage.to_dict(),
        "el": p.el,
        "data": data,
        "cues": cues,
        "colors": {
            "ink": INK_HEX,
            "amber": "#E8A33D",
            "text": "#ECE7DD",
            "slate": "#5B6472",
            "red": "#E63946",
            "paper": "#F2EDE3",
        },
    }
    return MotionPlan(job, stage, payload, duration, frames, p.errors, p.warnings)


# --------------------------------------------------------------------------- build and render


def render_duration(frames: int, fps: int) -> float:
    """The length written into the composition. HyperFrames rounds the frame count up, so
    152/30 written as 5.0667 came back as 153 frames; a hair under the last frame's end
    always gives exactly `frames`."""
    return math.floor((frames - 0.02) / fps * 1e4) / 1e4


def _template_files(name: str) -> list[Path]:
    files = []
    for folder in (templates_dir() / "_shared", templates_dir() / name):
        files += sorted(p for p in folder.rglob("*") if p.is_file())
    return files


def plan_hash(plan: MotionPlan, fonts: FontBook, version: str, gsap: Path) -> str:
    digest = hashlib.sha256()
    digest.update(MOTION_VERSION.encode())
    digest.update(json.dumps(plan.payload, sort_keys=True).encode())
    for path in _template_files(plan.job.template):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update(json.dumps(fonts.fingerprint()).encode())
    digest.update(version.encode())
    digest.update(str(gsap.stat().st_size).encode())
    return digest.hexdigest()[:12]


def build_project(plan: MotionPlan, work: Path, fonts: FontBook, gsap: Path) -> Path:
    if work.exists():
        shutil.rmtree(work)
    (work / "assets" / "fonts").mkdir(parents=True)
    tdir = templates_dir() / plan.job.template
    shared = templates_dir() / "_shared"
    for face in sorted(FACES):
        shutil.copy2(fonts.path(face), work / "assets" / "fonts" / fonts.path(face).name)
    if plan.job.template == "receipt-image":
        src = receipt_image_var(plan.job.vars, plan.stage.aspect)
        shutil.copy2(image_path(plan.job, src), work / "assets" / "receipt.png")
    shutil.copy2(gsap, work / "assets" / "gsap.min.js")
    shutil.copy2(shared / "house.js", work / "assets" / "house.js")
    shutil.copy2(tdir / "template.js", work / "assets" / "template.js")
    css = (shared / "house.css").read_text(encoding="utf-8")
    tcss = tdir / "template.css"
    if tcss.is_file():
        css += "\n" + tcss.read_text(encoding="utf-8")
    vars_js = json.dumps(plan.payload, ensure_ascii=False).replace("</", "<\\/")
    head = (
        f"<style>\n{fonts.css()}\n{css}\n</style>\n"
        f'<script src="assets/gsap.min.js"></script>\n'
        f"<script>window.YTP = {vars_js};</script>"
    )
    preload = "".join(
        f"<span style=\"font-family:'{family}';font-style:{'italic' if italic else 'normal'};"
        f'font-weight:{700 if face == "sans" else 600 if "bold" in face else 500}">Aa1</span>'
        for face, (family, _name, italic) in sorted(FACES.items())
    )
    preload = f'<div class="ytp-preload" aria-hidden="true">{preload}</div>'
    html = (tdir / "index.html").read_text(encoding="utf-8")
    stage = plan.stage
    for key, value in {
        "{{YTP_HEAD}}": head,
        "{{YTP_PRELOAD}}": preload,
        "{{COMPOSITION_ID}}": COMPOSITION_ID,
        "{{DURATION}}": f"{plan.payload['duration']:g}",
        "{{WIDTH}}": str(stage.width),
        "{{HEIGHT}}": str(stage.height),
        "{{FPS}}": str(plan.job.fps),
        "{{ASPECT}}": stage.aspect,
        "{{LAYOUT}}": stage.layout,
    }.items():
        html = html.replace(key, value)
    if "{{" in html:
        raise MotionError(f"{plan.job.template}/index.html has an unknown {{{{...}}}} field")
    (work / "index.html").write_text(html, encoding="utf-8", newline="\n")
    (work / ".env").write_text("HYPERFRAMES_NO_TELEMETRY=1\nDO_NOT_TRACK=1\n", encoding="utf-8")
    (work / "ytp-plan.json").write_text(json.dumps(plan.payload, indent=1), encoding="utf-8")
    return work


def _tail(proc: subprocess.CompletedProcess[str], lines: int = 25) -> str:
    text = "\n".join(x for x in (proc.stdout, proc.stderr) if x)
    return "\n".join(text.strip().splitlines()[-lines:])


def hyperframes_lint(command: list[str], work: Path) -> str:
    proc = _run([*command, "lint", str(work)], work, timeout=300)
    if proc.returncode != 0:
        raise MotionError(f"hyperframes lint failed in {work}:\n{_tail(proc)}")
    return _tail(proc, 8)


def hyperframes_render(
    command: list[str], work: Path, out: Path, fps: int, gpu: bool = True, log=print
) -> tuple[str, float]:
    """Render; NVENC first when ``gpu``, x264 if that fails. Returns (encoder, seconds)."""
    base = [
        *command,
        "render",
        str(work),
        "--output",
        str(out),
        "--quality",
        "high",
        "--fps",
        str(fps),
    ]
    attempts = [("h264_nvenc", [*base, "--gpu"])] if gpu else []
    attempts.append(("libx264", base))
    errors = []
    for encoder, cmd in attempts:
        if out.exists():
            out.unlink()
        started = time.monotonic()
        proc = _run(cmd, work)
        took = time.monotonic() - started
        (work / "render.log").write_text(
            "\n".join(x for x in (proc.stdout, proc.stderr) if x), encoding="utf-8"
        )
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            tag = probe_clip(out)["encoder"].lower()
            actual = "h264_nvenc" if "nvenc" in tag else "libx264" if "264" in tag else encoder
            if gpu and actual != "h264_nvenc":
                log("  no NVENC here; HyperFrames encoded with x264")
            return actual, took
        errors.append(f"{encoder}: {_tail(proc, 12)}")
        if encoder == "h264_nvenc":
            log("  --gpu render failed, retrying with x264")
    raise MotionError(f"hyperframes render failed in {work}:\n" + "\n".join(errors))


# --------------------------------------------------------------------------- frame checks


def _luma(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def probe_clip(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=width,height,r_frame_rate,nb_read_packets,codec_name:stream_tags=encoder:"
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        **hidden_kwargs(),
    )
    if proc.returncode != 0:
        raise MotionError(f"ffprobe failed on {path}: {proc.stderr.strip()[-400:]}")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    num, _, den = str(stream.get("r_frame_rate", "0/1")).partition("/")
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "fps": float(num) / float(den or 1) if float(den or 1) else 0.0,
        "frames": int(stream.get("nb_read_packets", 0)),
        "duration": float((data.get("format") or {}).get("duration") or 0.0),
        "codec": stream.get("codec_name"),
        "encoder": str((stream.get("tags") or {}).get("encoder") or ""),
    }


def grab_gray(path: Path, indices: list[int], width: int, height: int, tmp: Path):
    """Frames at ``indices`` as quarter-size grayscale Pillow images."""
    from PIL import Image

    indices = sorted(set(indices))
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.glob("g*.png"):
        old.unlink()
    select = "+".join(f"eq(n\\,{i})" for i in indices)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        f"select='{select}',scale={width // 4}:{height // 4},format=gray",
        "-fps_mode",
        "passthrough",
        str(tmp / "g%04d.png"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, **hidden_kwargs())
    if proc.returncode != 0:
        raise MotionError(f"frame grab failed: {proc.stderr.strip()[-400:]}")
    files = sorted(tmp.glob("g*.png"))
    out = {}
    for index, file in zip(indices, files):
        with Image.open(file) as img:
            out[index] = img.copy()
    return out


def _mean(img, box) -> float:
    from PIL import ImageStat

    return ImageStat.Stat(img.crop(box)).mean[0]


def _max(img, box) -> float:
    return float(img.crop(box).getextrema()[1])


def _coverage(img, box, threshold: float) -> int:
    """Pixels in box brighter than threshold."""
    crop = img.crop(box)
    cut = max(0, min(255, int(threshold)))
    return sum(crop.histogram()[cut + 1 :])


def check_frames(path: Path, plan: MotionPlan, tmp: Path) -> list[str]:
    """Post-render checks: length, first/last frame, fades, and the safe slice."""
    errors: list[str] = []
    info = probe_clip(path)
    stage = plan.stage
    if (info["width"], info["height"]) != (stage.width, stage.height):
        errors.append(
            f"size {info['width']}x{info['height']}, expected {stage.width}x{stage.height}"
        )
    if abs(info["frames"] - plan.frames) > 0:
        errors.append(f"{info['frames']} frames, expected {plan.frames}")
    if abs(info["duration"] - plan.duration) > 1.5 / plan.job.fps:
        errors.append(f"duration {info['duration']:.3f}s, expected {plan.duration:.3f}s")
    count = info["frames"] or plan.frames
    last = count - 1
    fps = plan.job.fps
    hold_ref = max(0, last - round(1.4 * fps))
    samples = {0, min(FIRST_ELEMENT_FRAMES - 1, last), last, hold_ref}
    samples |= set(range(0, count, max(1, fps // 2)))
    frames = grab_gray(path, sorted(samples), stage.width, stage.height, tmp)
    q = 4
    w, h = stage.width // q, stage.height // q
    corner = 8
    corners = [
        (0, 0, corner, corner),
        (w - corner, 0, w, corner),
        (0, h - corner, corner, h),
        (w - corner, h - corner, w, h),
    ]
    sbox = (stage.x // q, stage.y // q, (stage.x + stage.w) // q, (stage.y + stage.h) // q)
    ink = _luma(INK)

    def corner_luma(img) -> float:
        return sum(_mean(img, box) for box in corners) / len(corners)

    def content(img) -> float:
        return _max(img, sbox) - corner_luma(img)

    for name, index in (("first", 0), ("last", last)):
        img = frames.get(index)
        if img is None:
            errors.append(f"could not read the {name} frame")
            continue
        level = corner_luma(img)
        if level < ink - 6:
            errors.append(
                f"{name} frame is black (corner luma {level:.1f}, ink is {ink:.1f}); "
                "frame 0 and the last frame must show the ink background"
            )
        elif level > ink + 45:
            errors.append(f"{name} frame corners ({level:.1f}) are not the ink background")
    opening = frames.get(0)
    if opening is not None:
        lit = _coverage(opening, sbox, corner_luma(opening) + 40)
        if lit < FIRST_FRAME_MIN_PIXELS:
            errors.append(
                f"frame 1 is empty ({lit} lit pixels in the stage at quarter size, need "
                f"{FIRST_FRAME_MIN_PIXELS}); the label must be on screen from the first frame"
            )
    early = frames.get(min(FIRST_ELEMENT_FRAMES - 1, last))
    if early is not None and content(early) < 35:
        errors.append(f"nothing is visible by frame {FIRST_ELEMENT_FRAMES}")
    end, ref = frames.get(last), frames.get(hold_ref)
    if end is not None and content(end) < 35:
        errors.append("the last frame has no type on it")
    if end is not None and ref is not None:
        drop = _mean(ref, sbox) - _mean(end, sbox)
        peak = _max(ref, sbox) - _max(end, sbox)
        if drop > 1.0 or peak > 12:
            errors.append(
                "the end fades (the last frame is darker than 1.4s before it); "
                "hold the last line and hard-cut"
            )
    if stage.layout == "safe" and stage.aspect == "16x9":
        left, right = SAFE_SLICE[0] // q - 2, SAFE_SLICE[1] // q + 2
        worst = 0.0
        for img in frames.values():
            level = corner_luma(img)
            outside = max(_max(img, (0, 0, left, h)), _max(img, (right, 0, w, h)))
            worst = max(worst, outside - level)
        if worst > 30:
            errors.append(
                f"something is drawn outside the Shorts safe slice x {SAFE_SLICE[0]}-"
                f"{SAFE_SLICE[1]} (peak +{worst:.0f} luma over the ink)"
            )
    return errors


# --------------------------------------------------------------------------- one clip


@dataclass
class MotionResult:
    id: str
    aspect: str
    path: Path
    duration: float
    frames: int
    cached: bool
    encoder: str | None = None
    render_seconds: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Toolkit:
    command: list[str]
    fonts: FontBook
    gsap: Path
    version: str

    @classmethod
    def find(cls, config: dict[str, Any] | None, spec_path: Path | None) -> Toolkit:
        command = hyperframes_command(config, spec_path)
        fonts = FontBook(find_fonts_dir(config, spec_path))
        gsap = find_gsap(config, command, spec_path)
        return cls(command, fonts, gsap, hyperframes_version(tuple(command)))


def clip_path(episode: Path, plan: MotionPlan, key: str) -> Path:
    return episode / "clips" / "motion" / f"{plan.job.id}-{plan.stage.aspect}-{key}.mp4"


def ensure_clip(
    job: MotionJob,
    aspect: str,
    episode: Path,
    kit: Toolkit,
    *,
    allow_placeholder: bool = False,
    force: bool = False,
    gpu: bool = True,
    lint_only: bool = False,
    render: bool = True,
    log=print,
) -> MotionResult | None:
    """Plan, lint, and (unless cached) render one clip. Returns None for lint-only."""
    plan = plan_motion(job, aspect, kit.fonts, allow_placeholder)
    if plan.errors:
        raise MotionLintError(plan.where, plan.errors)
    key = plan_hash(plan, kit.fonts, kit.version, kit.gsap)
    out = clip_path(episode, plan, key)
    sidecar = out.with_suffix(".json")
    if out.is_file() and sidecar.is_file() and not force and not lint_only:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        return MotionResult(
            job.id,
            aspect,
            out,
            meta["duration"],
            meta["frames"],
            True,
            meta.get("encoder"),
            None,
            plan.warnings,
        )
    if not render and not lint_only:
        return None
    work = episode / ".ytp-motion" / f"{job.id}-{aspect}"
    build_project(plan, work, kit.fonts, kit.gsap)
    hyperframes_lint(kit.command, work)
    if lint_only:
        log(f"lint ok: {plan.where}, {plan.duration:.2f}s")
        return None
    tmp_out = work / "render.mp4"
    encoder, took = hyperframes_render(kit.command, work, tmp_out, job.fps, gpu, log)
    errors = check_frames(tmp_out, plan, work / "check")
    if errors:
        raise MotionLintError(plan.where + f" render {tmp_out}", errors)
    out.parent.mkdir(parents=True, exist_ok=True)
    for stale in out.parent.glob(f"{job.id}-{aspect}-*.mp4"):
        stale.unlink()
        stale.with_suffix(".json").unlink(missing_ok=True)
        stale.with_suffix(".last.png").unlink(missing_ok=True)
    shutil.move(str(tmp_out), out)
    meta = {
        "id": job.id,
        "template": job.template,
        "aspect": aspect,
        "layout": plan.stage.layout,
        "duration": plan.duration,
        "frames": plan.frames,
        "fps": job.fps,
        "encoder": encoder,
        "render_seconds": round(took, 2),
        "hash": key,
        "hyperframes": kit.version,
        "warnings": plan.warnings,
    }
    sidecar.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return MotionResult(
        job.id, aspect, out, plan.duration, plan.frames, False, encoder, took, plan.warnings
    )


def last_frame_png(clip: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-sseof",
        "-0.5",
        "-i",
        str(clip),
        "-update",
        "1",
        "-frames:v",
        "999",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, **hidden_kwargs())
    if proc.returncode != 0 or not dest.is_file():
        raise MotionError(f"could not grab the last frame of {clip}: {proc.stderr[-300:]}")
    return dest


# --------------------------------------------------------------------------- command


def job_for(frame, spec, caps: dict[str, dict[str, dict[str, float]]] | None = None) -> MotionJob:
    from ytp import scenes

    base = scenes.episode_for(spec.path) if getattr(spec, "path", None) else None
    return MotionJob(
        id=frame.id,
        template=frame.template,
        vars=dict(frame.vars or {}),
        layout=frame.motion_layout or str((spec.motion or {}).get("layout") or "safe"),
        clip_duration=frame.clip_duration,
        fps=spec.fps,
        series=spec.series,
        episode=spec.episode,
        caps=dict((caps or {}).get(frame.template) or {}),
        base=base,
    )


def set_caps(spec, fonts: FontBook) -> dict[str, dict[str, dict[str, float]]]:
    """Shared sizes for a set: for templates that list ``set_size`` elements (chapter
    cards' titles), every frame of that template in the scenes file uses the smallest
    size any of them needs, per aspect and layout. Computed over the whole file, so
    ``--only`` renders the same sizes as a full run."""
    sizes: dict[str, dict[str, dict[str, list[float]]]] = {}
    for frame in spec.frames:
        if frame.kind != "motion" or not frame.template:
            continue
        try:
            shared = load_manifest(frame.template).get("set_size") or []
        except (OSError, ValueError, MotionError):
            continue
        if not shared:
            continue
        job = job_for(frame, spec)
        for aspect in ASPECTS:
            plan = plan_motion(job, aspect, fonts, allow_placeholder=True)
            if plan.errors:
                continue
            key = f"{aspect}/{plan.stage.layout}"
            for name in shared:
                if name in plan.payload["el"]:
                    sizes.setdefault(frame.template, {}).setdefault(key, {}).setdefault(
                        name, []
                    ).append(plan.payload["el"][name]["size"])
    return {
        tpl: {key: {name: min(vals) for name, vals in names.items()} for key, names in by.items()}
        for tpl, by in sizes.items()
    }


@dataclass
class MotionRun:
    results: list[MotionResult]
    segments: list
    warnings: list[str]
    plan_path: Path | None
    fragment_path: Path | None


def run_motion(
    spec_path: Path,
    *,
    episode_dir: Path | None = None,
    only: list[str] | None = None,
    aspect: str = "16x9",
    allow_placeholder: bool = False,
    plan_path: Path | None = None,
    force: bool = False,
    gpu: bool = True,
    lint_only: bool = False,
    log=print,
) -> MotionRun:
    from ytp import scenes
    from ytp.plan import load_plan, save_plan

    spec = scenes.load_spec(spec_path)
    episode = (episode_dir or scenes.episode_for(spec_path)).resolve()
    motion_frames = [f for f in spec.frames if f.kind == "motion"]
    if only:
        missing = sorted(set(only) - {f.id for f in motion_frames})
        if missing:
            raise ValueError(f"no motion frame with id {', '.join(missing)}")
        motion_frames = [f for f in motion_frames if f.id in only]
    if not motion_frames:
        raise ValueError(f"{spec_path} has no kind: motion frames")
    aspects = list(ASPECTS) if aspect == "both" else [aspect]
    kit = Toolkit.find(spec.motion, spec.path)
    caps = set_caps(spec, kit.fonts)
    results: list[MotionResult] = []
    warnings: list[str] = []
    failures: list[str] = []
    for frame in motion_frames:
        job = job_for(frame, spec, caps)
        for one in aspects:
            try:
                result = ensure_clip(
                    job,
                    one,
                    episode,
                    kit,
                    allow_placeholder=allow_placeholder,
                    force=force,
                    gpu=gpu,
                    lint_only=lint_only,
                    log=log,
                )
            except MotionLintError as exc:
                failures.append(str(exc))
                continue
            if result is None:
                continue
            results.append(result)
            warnings.extend(
                f"{job.id}: {w}" for w in result.warnings if f"{job.id}: {w}" not in warnings
            )
            how = "cached" if result.cached else (f"{result.encoder}, {result.render_seconds:.1f}s")
            log(
                f"{'kept' if result.cached else 'wrote'} {result.path} "
                f"({result.duration:.2f}s, {how})"
            )
    if failures:
        raise MotionLintError("ytp motion", failures)
    segments = []
    fragment = written = None
    if not lint_only:
        # A hold still beside every clip, both aspects, for cuts that hold the end.
        for result in results:
            hold = result.path.with_suffix(".last.png")
            if not hold.is_file():
                last_frame_png(result.path, hold)
        clips16 = {r.id: r for r in results if r.aspect == "16x9"}
        if clips16:
            segments, seg_warn = motion_segments(spec, episode, clips16, plan_path)
            warnings.extend(seg_warn)
            if plan_path is not None:
                plan = load_plan(plan_path)
                ids = {f"{scenes.SCENE_PREFIX}{i}" for i in clips16} | {
                    f"{scenes.SCENE_PREFIX}{i}-hold" for i in clips16
                }
                warnings.extend(scenes.merge_segments(plan, segments, replace_ids=ids))
                save_plan(plan, plan_path)
                written = plan_path
                log(f"merged {len(segments)} motion segments into {plan_path}")
            else:
                fragment = episode / "clips" / "motion" / "motion-plan.yaml"
                fragment.write_text(
                    yaml.safe_dump(
                        scenes.fragment_dict(segments), sort_keys=False, allow_unicode=True
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                log(f"wrote {fragment} ({len(segments)} segments)")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return MotionRun(results, segments, warnings, written, fragment)


def motion_segments(spec, episode: Path, clips: dict[str, MotionResult], plan_path):
    """Clip segments (plus a held last frame when the slot is longer) for motion frames."""
    from ytp import scenes
    from ytp.plan import load_plan

    narration = []
    read_plan = plan_path or episode / "edit" / "plan.yaml"
    if read_plan.is_file():
        narration = load_plan(read_plan).narration
    for frame in spec.frames:
        if (
            frame.kind == "motion"
            and frame.id in clips
            and frame.duration is None
            and frame.end is None
            and frame.line is None
        ):
            frame.duration = clips[frame.id].duration
    timed, warnings = scenes.frame_timing(spec.frames, narration)
    timed = [t for t in timed if t.frame.id in clips]
    holds = {}
    for item in timed:
        clip = clips[item.frame.id]
        if item.duration > clip.duration + 0.0005:
            holds[item.frame.id] = last_frame_png(clip.path, clip.path.with_suffix(".last.png"))
    segs = scenes.scene_segments(
        timed, holds, {}, episode, motion={k: (v.path, v.duration) for k, v in clips.items()}
    )
    return segs, warnings


__all__ = [
    "ASPECTS",
    "FontBook",
    "MotionError",
    "MotionJob",
    "MotionLintError",
    "SAFE_SLICE",
    "Toolkit",
    "check_frames",
    "ensure_clip",
    "hyperframes_command",
    "plan_motion",
    "run_motion",
    "stage_for",
]
