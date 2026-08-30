from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydub import AudioSegment, silence as pydub_silence

from pipeline.config import Settings, require_ffmpeg
from pipeline.hidden_process import run_hidden
from pipeline.media import MediaError, concat_keep_ranges, extract_audio, probe_duration
from pipeline.models import SilenceCutMap, SilenceTrimResult, TimeRange


def remove_silence(
    input_path: Path,
    settings: Settings,
    *,
    output_path: Path | None = None,
    use_auto_editor: bool = False,
) -> SilenceTrimResult:
    """Strip pauses longer than 0.7s, leaving 0.15s pad on each keep edge.

    pydub energy detection plus ffmpeg concat is the source of truth so the
    cut map matches the rendered file. auto-editor is opt-in and more
    aggressive (it can cut gaps shorter than 0.7s).
    """
    require_ffmpeg(settings)
    input_path = input_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input video not found: {input_path}")

    settings.ensure_dirs()
    dest = (output_path or settings.work_dir / f"{input_path.stem}_trimmed.mp4").resolve()
    original_duration = probe_duration(input_path, settings)
    kept = detect_keep_ranges(input_path, settings)
    if not kept:
        raise MediaError(
            "Silence detection found no speech. Lower SILENCE_THRESHOLD_DB "
            "(e.g. -50) or use --skip-silence."
        )
    cut_map = _build_cut_map(kept, original_duration)

    if use_auto_editor and shutil.which("auto-editor"):
        try:
            return _render_auto_editor(input_path, dest, settings, original_duration)
        except (MediaError, OSError, subprocess.CalledProcessError):
            pass

    return _render_ffmpeg(input_path, dest, kept, cut_map, original_duration, settings)


def detect_keep_ranges(input_path: Path, settings: Settings) -> list[TimeRange]:
    """Speech islands on the original timeline. Gaps under 0.7s stay intact."""
    wav_path = settings.work_dir / f"{input_path.stem}_detect.wav"
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    extract_audio(input_path, wav_path, settings)
    try:
        audio = AudioSegment.from_file(wav_path)
    finally:
        if wav_path.exists():
            wav_path.unlink()
    return compute_keep_ranges(
        audio,
        min_silence_ms=int(settings.silence_min_duration * 1000),
        padding_ms=int(settings.silence_padding * 1000),
        threshold_db=settings.silence_threshold_db,
    )


def compute_keep_ranges(
    audio: AudioSegment,
    *,
    min_silence_ms: int,
    padding_ms: int,
    threshold_db: float,
) -> list[TimeRange]:
    """Return padded, merged speech ranges in seconds.

    ``min_silence_ms`` is the shortest gap that may be removed. Shorter gaps
    are treated as speech rhythm and are not split.
    """
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
    return SilenceCutMap(
        kept_ranges=kept,
        removed_ranges=_removed_from_kept(kept, original_duration),
        original_duration=original_duration,
        trimmed_duration=sum(span.duration for span in kept),
    )


def _render_ffmpeg(
    input_path: Path,
    dest: Path,
    kept: list[TimeRange],
    cut_map: SilenceCutMap,
    original_duration: float,
    settings: Settings,
) -> SilenceTrimResult:
    total_keep = cut_map.trimmed_duration
    if total_keep >= original_duration - 0.05 and len(kept) == 1:
        if dest != input_path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(input_path.read_bytes())
        return SilenceTrimResult(
            output_path=dest if dest != input_path else input_path,
            cut_map=cut_map,
            backend="pydub-ffmpeg",
        )

    concat_keep_ranges(
        input_path,
        [(span.start, span.end) for span in kept],
        dest,
        settings,
    )
    return SilenceTrimResult(output_path=dest, cut_map=cut_map, backend="pydub-ffmpeg")


def cut_map_for_rendered_output(
    original_duration: float, rendered_duration: float
) -> SilenceCutMap:
    """Cut map that describes the file handed to the director/compositor.

    auto-editor's keep list is not the pydub map. Downstream timestamps are
    on this rendered file, so trimmed_duration must match it.
    """
    rendered = max(0.0, float(rendered_duration))
    original = max(0.0, float(original_duration))
    return SilenceCutMap(
        kept_ranges=[TimeRange(start=0.0, end=rendered)] if rendered > 0 else [],
        removed_ranges=[],
        original_duration=original,
        trimmed_duration=rendered,
    )


def _render_auto_editor(
    input_path: Path,
    dest: Path,
    settings: Settings,
    original_duration: float,
) -> SilenceTrimResult:
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
    rendered = run_hidden(render, capture_output=True, text=True)
    if rendered.returncode != 0 or not dest.exists():
        raise MediaError(rendered.stderr or "auto-editor render failed")
    actual = probe_duration(dest, settings)
    cut_map = cut_map_for_rendered_output(original_duration, actual)
    return SilenceTrimResult(output_path=dest, cut_map=cut_map, backend="auto-editor")


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
