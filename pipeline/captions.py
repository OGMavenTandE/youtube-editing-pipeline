"""Map a trimmed-timeline transcript onto Studio caption files."""

from __future__ import annotations

from pathlib import Path

from pipeline.models import TimedTranscript, TranscriptCue


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_timestamp(seconds: float) -> str:
    return format_srt_timestamp(seconds).replace(",", ".")


def caption_cues(transcript: TimedTranscript) -> list[TranscriptCue]:
    cues = [
        TranscriptCue(start=cue.start, end=cue.end, text=cue.text.strip())
        for cue in transcript.cues
        if cue.text.strip() and cue.end > cue.start
    ]
    if cues:
        return cues
    text = transcript.text.strip()
    if not text:
        return []
    return [TranscriptCue(start=0.0, end=max(transcript.duration, 0.1), text=text)]


def format_srt(transcript: TimedTranscript) -> str:
    lines: list[str] = []
    for index, cue in enumerate(caption_cues(transcript), start=1):
        lines.append(str(index))
        lines.append(
            f"{format_srt_timestamp(cue.start)} --> {format_srt_timestamp(cue.end)}"
        )
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def format_vtt(transcript: TimedTranscript) -> str:
    lines = ["WEBVTT", ""]
    for cue in caption_cues(transcript):
        lines.append(
            f"{format_vtt_timestamp(cue.start)} --> {format_vtt_timestamp(cue.end)}"
        )
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def write_caption_files(transcript: TimedTranscript, dest_dir: Path) -> tuple[Path, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    srt_path = dest_dir / "captions.srt"
    vtt_path = dest_dir / "captions.vtt"
    srt_path.write_text(format_srt(transcript), encoding="utf-8")
    vtt_path.write_text(format_vtt(transcript), encoding="utf-8")
    return srt_path, vtt_path
