"""Stub for a Studio graphics template. Writes a flat PNG of the requested size.

The real script is Studio's and is called with the same argv. This stub does
not draw the ghost host, cards, or type.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Studio graphics stub")
    parser.add_argument("--template", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--palette", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    color = "black" if args.palette.upper() == "DARK" else "0xE8E4DC"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={args.width}x{args.height}",
            "-frames:v",
            "1",
            str(args.out),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    print(f"studio-graphics-stub {args.template} {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
