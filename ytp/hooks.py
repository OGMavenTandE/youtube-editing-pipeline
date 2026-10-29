"""Hooks that call Track and Studio scripts unchanged.

ytp does not import those scripts and does not reimplement their mixes or
templates. A script is invoked as a process with the argv below.

Audio mix (Track), any executable or a ``.py`` file run with this interpreter:

    <script> --voice VOICE.wav --cues CUES.json --out MIX.wav --plan PLAN.yaml

``CUES.json`` is the plan's music list (id, sfx, at, gain_db, source,
duration, duck_under_voice, note). The script writes ``MIX.wav`` and owns
the mix. ytp muxes that wav as-is.

Graphics (Studio):

    <script> --template NAME --out STILL.png --width W --height H
             --palette PALETTE --text TEXT --id SEGMENT_ID

The script writes one PNG. ytp holds it for that segment's duration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ytp.ff import run_cmd
from ytp.plan import EditPlan

TRACK_MIX_STUB = Path(__file__).resolve().parent / "stub_hooks" / "track_mix.py"
STUDIO_GRAPHICS_STUB = Path(__file__).resolve().parent / "stub_hooks" / "studio_graphics.py"


def script_command(script: Path) -> list[str]:
    if script.suffix.lower() == ".py":
        return [sys.executable, str(script)]
    return [str(script)]


def write_cue_file(plan: EditPlan, path: Path) -> None:
    payload = {
        "voice": plan.voice,
        "lead_in": plan.lead_in,
        "music": [
            {
                "id": cue.id,
                "sfx": cue.sfx,
                "at": cue.at,
                "gain_db": cue.gain_db,
                "source": cue.source,
                "duration": cue.duration,
                "duck_under_voice": cue.duck_under_voice,
                "note": cue.note,
            }
            for cue in plan.music
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def audio_hook_command(
    script: Path,
    *,
    voice: Path,
    cues: Path,
    out: Path,
    plan: Path,
) -> list[str]:
    return script_command(script) + [
        "--voice",
        str(voice),
        "--cues",
        str(cues),
        "--out",
        str(out),
        "--plan",
        str(plan),
    ]


def graphics_hook_command(
    script: Path,
    *,
    template: str,
    out: Path,
    width: int,
    height: int,
    palette: str,
    text: str,
    segment_id: str,
) -> list[str]:
    return script_command(script) + [
        "--template",
        template,
        "--out",
        str(out),
        "--width",
        str(width),
        "--height",
        str(height),
        "--palette",
        palette,
        "--text",
        text,
        "--id",
        segment_id,
    ]


def run_hook(command: list[str]) -> None:
    run_cmd(command)
