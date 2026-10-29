"""Sidecar SRT. Burn-in uses this file with ffmpeg's subtitles filter."""

from __future__ import annotations

from pathlib import Path

from ytp.plan import CaptionCue


def format_srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_timestamp(seconds: float) -> str:
    """Clock time for reports: HH:MM:SS.mmm."""
    return format_srt_time(seconds).replace(",", ".")


def write_srt(cues: list[CaptionCue], path: Path) -> None:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = cue.text.strip()
        blocks.append(
            f"{index}\n{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}\n{text}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8", newline="\n")
