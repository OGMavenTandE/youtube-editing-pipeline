"""One YAML rules file per series.

Sections are owned by the room that enforces them:

- picture: Studio
- audio: Track
- edit: Booth
- script_copy: Galley
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SECTIONS: tuple[str, ...] = ("picture", "audio", "edit", "script_copy")

OWNERS: dict[str, str] = {
    "picture": "Studio",
    "audio": "Track",
    "edit": "Booth",
    "script_copy": "Galley",
}

# Channel-wide vendors. The on-camera line is "some vendors".
VENDOR_NAMES: tuple[str, ...] = (
    "Grok",
    "Google",
    "Anthropic",
    "ChatGPT",
    "OpenAI",
    "Claude",
    "Gemini",
    "Copilot",
    "Meta AI",
    "Llama",
    "Mistral",
    "xAI",
    "DeepSeek",
    "Perplexity",
)

DEFAULT_LINT: dict[str, Any] = {
    "banned_phrases": [
        "stick figure",
        "@scottmastin",
        "former",
        "Pentagon AI Tester",
        "like and subscribe",
        "Department of Defense",
        "DoD",
    ],
    "vendors": {
        "say_instead": "some vendors",
        "names": list(VENDOR_NAMES),
    },
    "banned_sounds": [
        "buzz",
        "buzz-style",
        "film clip",
        "film-clip",
        "star wars",
        "star-wars",
        "terminator",
        "i, robot",
        "i-robot",
        "lightsaber",
        "imperial-march",
    ],
    "closers": [
        "like and subscribe",
        "smash that like",
        "hit the like",
        "hit subscribe",
        "leave a comment",
        "comment below",
        "thanks for watching",
        "see you next time",
        "don't forget to subscribe",
    ],
    "wpm": {
        "pause_gap_ms": 250,
        "source_type": "narration",
        "narration": {
            "overall": {"min": 155, "max": 185, "severity": "warn"},
            "articulation": {"min": 175, "max": 185, "severity": "warn"},
        },
        "desk": {
            "overall": {"min": 155, "max": 185, "severity": "warn"},
            "articulation": {"min": 175, "max": 185, "severity": "warn"},
        },
    },
    "loudness": {
        "voice_lufs": -16,
        "shorts_lufs": -14,
        "true_peak_dbtp": -1.5,
        "music_under_voice_db": 17,
        "music_tolerance_db": 3,
        "lufs_tolerance": 1,
    },
    "ban_em_dash": True,
    "ban_emoji": True,
    "splice_window_seconds": 0.5,
    "language": "en",
}

_BAN_SENTENCE = re.compile(r"\b(never|do not|don't|banned|forbid)\b", re.I)
_QUOTED = re.compile(r'"([^"]+)"')
_VENDOR_LIST = re.compile(r"\(([^)]*)\)")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def rules_path(series: str, root: Path | None = None) -> Path:
    slug = series.strip().lower().replace(" ", "-")
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
        raise ValueError(f"series slug must be a file-safe name, got {series!r}")
    return (root or repo_root()) / "rules" / f"{slug}.yaml"


@dataclass
class Rule:
    id: str
    text: str
    source: str
    added: str
    status: str = ""


@dataclass
class SeriesRules:
    series: str
    path: Path
    owners: dict[str, str]
    sections: dict[str, list[Rule]]
    lint: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


def _as_rule(item: dict[str, Any]) -> Rule:
    return Rule(
        id=str(item["id"]),
        text=str(item["text"]),
        source=str(item.get("source") or ""),
        added=str(item.get("added") or ""),
        status=str(item.get("status") or ""),
    )


def load_raw(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping")
    return data


def parse_rules(data: dict[str, Any], path: Path) -> SeriesRules:
    sections: dict[str, list[Rule]] = {}
    for name in SECTIONS:
        items = data.get(name) or []
        sections[name] = [_as_rule(item) for item in items]
    owners = dict(OWNERS)
    owners.update({str(k): str(v) for k, v in (data.get("owners") or {}).items()})
    lint = dict(DEFAULT_LINT)
    loaded = data.get("lint") or {}
    if isinstance(loaded, dict):
        lint.update(loaded)
    return SeriesRules(
        series=str(data.get("series") or path.stem),
        path=path,
        owners=owners,
        sections=sections,
        lint=lint,
        raw=data,
    )


def load_rules(series: str, root: Path | None = None) -> SeriesRules:
    path = rules_path(series, root)
    if not path.exists():
        raise FileNotFoundError(f"no rules file for series {series!r} at {path}")
    return parse_rules(load_raw(path), path)


def _dump(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, width=100)


def _next_id(items: list[dict[str, Any]], section: str) -> str:
    highest = 0
    prefix = f"{section}-"
    for item in items:
        raw_id = str(item.get("id") or "")
        if raw_id.startswith(prefix):
            suffix = raw_id[len(prefix) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{section}-{highest + 1:03d}"


def blank_series(series: str) -> dict[str, Any]:
    """New series file. Loudness, pace, and the vendor ban are channel rules."""
    vendor_text = (
        'Never name or criticize an AI vendor on camera. Never say '
        + ", ".join(f'"{name}"' for name in VENDOR_NAMES)
        + '. Say "some vendors" instead.'
    )
    return {
        "series": series,
        "owners": dict(OWNERS),
        "picture": [],
        "audio": [],
        "edit": [
            {
                "id": "edit-001",
                "text": vendor_text,
                "source": "channel",
                "added": date.today().isoformat(),
            }
        ],
        "script_copy": [],
        "lint": DEFAULT_LINT,
    }


def add_rule(
    series: str,
    section: str,
    text: str,
    *,
    root: Path | None = None,
    source: str = "ytp rule add",
) -> Rule:
    """Append one note. This is how a line from Scott becomes a rule."""
    if section not in SECTIONS:
        known = ", ".join(SECTIONS)
        raise ValueError(f"section must be one of {known}, got {section!r}")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("rule text is empty")
    path = rules_path(series, root)
    if path.exists():
        data = load_raw(path)
    else:
        data = blank_series(series)
    items = list(data.get(section) or [])
    rule = {
        "id": _next_id(items, section),
        "text": cleaned,
        "source": source,
        "added": date.today().isoformat(),
    }
    items.append(rule)
    data[section] = items
    data.setdefault("series", series)
    data.setdefault("owners", dict(OWNERS))
    _dump(data, path)
    return _as_rule(rule)


def extract_banned_phrases(text: str) -> list[str]:
    """Pull quoted bans out of a note.

    A sentence counts when it says never / do not / banned / forbid.
    Text after ``instead`` is the replacement line, not a ban.
    """
    phrases: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not _BAN_SENTENCE.search(sentence):
            continue
        head = re.split(r"\binstead\b", sentence, maxsplit=1, flags=re.I)[0]
        phrases.extend(match.strip() for match in _QUOTED.findall(head) if match.strip())
    return phrases


def extract_vendors(text: str) -> list[str]:
    """Parenthetical vendor list on a sentence that bans naming vendors."""
    if not re.search(r"vendor", text, re.I) or not _BAN_SENTENCE.search(text):
        return []
    match = _VENDOR_LIST.search(text)
    if match is None:
        return []
    names = []
    for part in re.split(r",|\bor\b", match.group(1)):
        name = part.strip(" .")
        if name and name.lower() not in {"etc", "and others"}:
            names.append(name)
    return names


@dataclass
class Bans:
    phrases: list[str]
    vendors: list[str]
    say_instead: str
    sounds: list[str]
    ban_em_dash: bool
    ban_emoji: bool


def collected_bans(rules: SeriesRules) -> Bans:
    """Union of the lint block and bans written into rule sentences."""
    phrases: list[str] = []
    vendors: list[str] = []
    for section in SECTIONS:
        for rule in rules.sections.get(section, []):
            phrases.extend(extract_banned_phrases(rule.text))
            vendors.extend(extract_vendors(rule.text))
    lint = rules.lint
    phrases.extend(str(item) for item in lint.get("banned_phrases") or [])
    vendor_block = lint.get("vendors") or {}
    vendors.extend(str(item) for item in vendor_block.get("names") or [])
    sounds = [str(item) for item in lint.get("banned_sounds") or []]
    return Bans(
        phrases=_dedupe(phrases),
        vendors=_dedupe(vendors),
        say_instead=str(vendor_block.get("say_instead") or "some vendors"),
        sounds=_dedupe(sounds),
        ban_em_dash=bool(lint.get("ban_em_dash", True)),
        ban_emoji=bool(lint.get("ban_emoji", True)),
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered
