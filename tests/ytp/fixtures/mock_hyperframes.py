"""A stand-in for the HyperFrames CLI in tests.

``lint`` checks the project has one composition root; ``render`` reads the root's
size and duration from index.html and writes a matching mp4 with ffmpeg: the ink
background plus a bright box inside the type stage. Env switches:

MOCK_HF_LOG      append one JSON line per call (argv and the telemetry env)
MOCK_HF_FAIL_GPU fail any render that passes --gpu
MOCK_HF_FAIL_LINT fail lint
MOCK_HF_MODE     black_start | black_end | fade_end | outside | none
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

INK = "0x0f1115"


def main(argv: list[str]) -> int:
    log = os.environ.get("MOCK_HF_LOG")
    if log:
        keys = ["HYPERFRAMES_NO_TELEMETRY", "DO_NOT_TRACK", "HYPERFRAMES_NO_UPDATE_CHECK"]
        with open(log, "a", encoding="utf-8") as handle:
            env = {k: os.environ.get(k) for k in keys}
            handle.write(json.dumps({"argv": argv, "env": env}) + "\n")
    if argv[:1] == ["--version"]:
        print("0.0.0-mock")
        return 0
    command, rest = argv[0], argv[1:]
    project = Path(rest[0]) if rest and not rest[0].startswith("-") else Path.cwd()
    html = (project / "index.html").read_text(encoding="utf-8")
    if command == "lint":
        if os.environ.get("MOCK_HF_FAIL_LINT"):
            print("x mock lint error")
            return 1
        if html.count("data-composition-id") != 1 or "window.__timelines" not in html:
            print("x missing composition root or timeline registry")
            return 1
        print("0 error(s), 0 warning(s)")
        return 0
    if command != "render":
        return 2
    if "--gpu" in rest and os.environ.get("MOCK_HF_FAIL_GPU"):
        print("NVENC not available", file=sys.stderr)
        return 1
    out = rest[rest.index("--output") + 1]
    fps = int(rest[rest.index("--fps") + 1])

    def attr(name: str) -> str:
        return re.search(rf'{name}="([^"]+)"', html).group(1)

    width, height, duration = (
        int(attr("data-width")),
        int(attr("data-height")),
        attr("data-duration"),
    )
    plan = json.loads((project / "ytp-plan.json").read_text(encoding="utf-8"))
    stage = plan["stage"]
    mode = os.environ.get("MOCK_HF_MODE", "none")
    x = 40 if mode == "outside" else stage["x"] + 20
    box = f"drawbox=x={x}:y={stage['y'] + 40}:w=200:h=120:color=white:t=fill"
    filters = [box]
    total = float(duration)
    if mode == "black_start":
        filters.append("drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(n,1)'")
    if mode == "black_end":
        filters.append(
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='gte(t,{total - 0.2})'"
        )
    if mode == "fade_end":
        filters.append(f"fade=t=out:st={total - 0.5}:d=0.5:color={INK}")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={INK}:s={width}x{height}:r={fps}:d={duration}",
        "-frames:v",
        str(round(total * fps)),
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        out,
    ]
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
