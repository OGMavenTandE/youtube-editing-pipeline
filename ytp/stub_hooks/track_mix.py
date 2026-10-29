"""Stub for Track's mix script. Copies the voice wav through.

The real script is Track's and is called with the same argv. This stub does
not mix, duck, or measure loudness.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track mix stub")
    parser.add_argument("--voice", type=Path, required=True)
    parser.add_argument("--cues", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not args.voice.is_file():
        print(f"track-mix-stub: voice not found: {args.voice}", file=sys.stderr)
        return 1
    if args.voice.suffix.lower() == ".wav":
        shutil.copyfile(args.voice, args.out)
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-nostdin",
                "-i",
                str(args.voice),
                "-c:a",
                "pcm_s16le",
                str(args.out),
            ],
            check=True,
        )
    print("track-mix-stub")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
