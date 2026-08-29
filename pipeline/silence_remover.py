from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydub import AudioSegment, silence as pydub_silence

from pipeline.config import Settings, require_ffmpeg
from pipeline.media import MediaError, concat_keep_ranges, extract_audio, probe_duration
from pipeline.models import SilenceCutMap, SilenceTrimResult, TimeRange


def remove_silence(
    input_path: Path,
    settings: Settings,
    *,
    output_path: Path | None = None,
    prefer_auto_editor: bool = True,
) -> SilenceTrimResult:
    """Strip dead air longer than ``settings.silence_min_duration``.

    Keep-ranges are padded by ``settings.silence_padding`` on each side so
    speech attacks and tails are not clipped. Prefers auto-editor when it is
    installed; otherwise uses pydub detection plus ffmpeg concat.
    """
    require_ffmpeg(settings)
    input_path = input_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input video not found: {input_path}")

    settings.ensure_dirs()
    dest = output_path or settings.work_dir / f"{input_path.stem}_trimmed.mp4"
    dest = dest.resolve()
    original_duration = probe_duration(input_path, settings)

    if prefer_auto_editor and shutil.which("auto-editor"):
        try:
            return _trim_with_auto_editor(input_path, dest, settings, original_duration)
        except (MediaError, OSError, subprocess.CalledProcessError):
            pass

    return _trim_with_pydub(input_path, dest, settings, original_duration)


def compute_keep_ranges(
    audio: AudioSegment,
    *,
    min_silence_ms: int,
    padding_ms: int,
    threshold_db: float,
) -> list[TimeRange]:
    """Return padded, merged speech ranges in seconds."""
    raw = pydub_silence.detect_nonsilent(
        audio,
        min_silence_len=max(min_silence_ms, 1),
        silence_thresh=threshold_db,
    )
    duration_ms = len(audio)
    padded: list[tuple[int, int]] = []
    for start_ms, end_ms in raw:
        padded.append(
            (max(0, start_ms - padding_ms), min(duration_ms, end_ms + padding_ms))
        )
    return [TimeRange(start=s / 1000.0, end=e / 1000.0) for s, e in _merge_ms(padded)]


def _merge_ms(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda item: item[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _removed_from_kept(kept: list[TimeRange], duration: float) -> list[TimeRange]:
    removed: list[TimeRange] = []
    cursor = 0.0
    for span in kept:
        if span.start > cursor:
            removed.append(TimeRange(start=cursor, end=span.start))
        cursor = max(cursor, span.end)
    if duration > cursor:
        removed.append(TimeRange(start=cursor, end=duration))
    return removed


def _build_cut_map(kept: list[TimeRange], original_duration: float) -> SilenceCutMap:
    trimmed = sum(span.duration for span in kept)
    return SilenceCutMap(
        kept_ranges=kept,
        removed_ranges=_removed_from_kept(kept, original_duration),
        original_duration=original_duration,
        trimmed_duration=trimmed,
    )


def _trim_with_pydub(
    input_path: Path,
    dest: Path,
    settings: Settings,
    original_duration: float,
) -> SilenceTrimResult:
    wav_path = settings.work_dir / f"{input_path.stem}_detect.wav"
    extract_audio(input_path, wav_path, settings)
    try:
        audio = AudioSegment.from_file(wav_path)
    finally:
        if wav_path.exists():
            wav_path.unlink()

    min_silence_ms = int(settings.silence_min_duration * 1000)
    padding_ms = int(settings.silence_padding * 1000)
    kept = compute_keep_ranges(
        audio,
        min_silence_ms=min_silence_ms,
        padding_ms=padding_ms,
        threshold_db=settings.silence_threshold_db,
    )
    if not kept:
        raise MediaError(
            "Silence detection found no speech. Lower SILENCE_THRESHOLD_DB "
            "(e.g. -50) or use --skip-silence."
        )

    # Nothing meaningful to cut: keep the original file.
    total_keep = sum(span.duration for span in kept)
    if total_keep >= original_duration - 0.05 and len(kept) == 1:
        if dest != input_path:
            dest.write_bytes(input_path.read_bytes())
        return SilenceTrimResult(
            output_path=dest if dest != input_path else input_path,
            cut_map=_build_cut_map(kept, original_duration),
            backend="pydub-ffmpeg",
        )

    concat_keep_ranges(
        input_path,
        [(span.start, span.end) for span in kept],
        dest,
        settings,
    )
    return SilenceTrimResult(
        output_path=dest,
        cut_map=_build_cut_map(kept, original_duration),
        backend="pydub-ffmpeg",
    )


def _trim_with_auto_editor(
    input_path: Path,
    dest: Path,
    settings: Settings,
    original_duration: float,
) -> SilenceTrimResult:
    """Render with auto-editor; build the cut map with the same pydub detector."""
    wav_path = settings.work_dir / f"{input_path.stem}_detect.wav"
    extract_audio(input_path, wav_path, settings)
    try:
        audio = AudioSegment.from_file(wav_path)
    finally:
        if wav_path.exists():
            wav_path.unlink()
    kept = compute_keep_ranges(
        audio,
        min_silence_ms=int(settings.silence_min_duration * 1000),
        padding_ms=int(settings.silence_padding * 1000),
        threshold_db=settings.silence_threshold_db,
    )
    if not kept:
        raise MediaError("Silence detection found no speech before auto-editor render.")

    render = [
        "auto-editor",
        str(input_path),
        "--edit",
        "audio",
        "--margin",
        f"{settings.silence_padding}s",
        "--output",
        str(dest),
        "--no-open",
    ]
    rendered = subprocess.run(render, capture_output=True, text=True)
    if rendered.returncode != 0 or not dest.exists():
        raise MediaError(rendered.stderr or "auto-editor render failed")

    kept = _fold_short_gaps(kept, settings.silence_min_duration, original_duration)
    return SilenceTrimResult(
        output_path=dest,
        cut_map=_build_cut_map(kept, original_duration),
        backend="auto-editor",
    )


def _fold_short_gaps(
    kept: list[TimeRange], min_silence: float, duration: float
) -> list[TimeRange]:
    if not kept:
        return []
    folded: list[TimeRange] = [kept[0]]
    for span in kept[1:]:
        gap = span.start - folded[-1].end
        if 0 <= gap < min_silence:
            folded[-1] = TimeRange(start=folded[-1].start, end=span.end)
        else:
            folded.append(span)
    if folded[-1].end < duration and (duration - folded[-1].end) < min_silence:
        folded[-1] = TimeRange(start=folded[-1].start, end=duration)
    return folded
