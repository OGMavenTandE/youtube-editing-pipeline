from pydub import AudioSegment, generators

from pipeline.models import TimeRange
from pipeline.silence_remover import (
    compute_keep_ranges,
    cut_map_for_rendered_output,
    _fold_short_gaps,
    _build_cut_map,
)


def test_keep_ranges_drop_long_silence_and_pad() -> None:
    speech = generators.Sine(440).to_audio_segment(duration=400).apply_gain(-6)
    gap = AudioSegment.silent(duration=1000)
    audio = speech + gap + speech

    kept = compute_keep_ranges(
        audio,
        min_silence_ms=700,
        padding_ms=150,
        threshold_db=-30,
    )
    assert len(kept) == 2
    assert kept[0].start == 0.0
    assert 0.5 <= kept[0].end <= 0.6
    assert 1.2 <= kept[1].start <= 1.3
    assert abs(kept[1].end - 1.8) < 0.05


def test_short_silence_is_kept() -> None:
    speech = generators.Sine(440).to_audio_segment(duration=300).apply_gain(-6)
    gap = AudioSegment.silent(duration=400)
    audio = speech + gap + speech

    kept = compute_keep_ranges(
        audio,
        min_silence_ms=700,
        padding_ms=150,
        threshold_db=-30,
    )
    assert len(kept) == 1
    assert kept[0].start == 0.0
    assert kept[0].end >= 0.9


def test_cut_map_remaining_gap_is_padding_sum() -> None:
    speech = generators.Sine(440).to_audio_segment(duration=500).apply_gain(-6)
    gap = AudioSegment.silent(duration=1200)
    audio = speech + gap + speech
    kept = compute_keep_ranges(
        audio,
        min_silence_ms=700,
        padding_ms=150,
        threshold_db=-30,
    )
    cut_map = _build_cut_map(kept, 2.2)
    assert len(cut_map.removed_ranges) == 1
    removed = cut_map.removed_ranges[0]
    # 1.2s gap minus 0.15s pad on each speech edge. The two pads sit
    # next to each other in the output (~0.3s of rest).
    assert 0.85 <= removed.duration <= 0.95
    assert abs((kept[1].start - kept[0].end) - removed.duration) < 0.02


def test_fold_short_gaps() -> None:
    kept = [
        TimeRange(start=0.0, end=1.0),
        TimeRange(start=1.4, end=2.0),
        TimeRange(start=3.5, end=4.0),
    ]
    folded = _fold_short_gaps(kept, min_silence=0.7, duration=4.0)
    assert len(folded) == 2
    assert folded[0].end == 2.0
    assert folded[1].start == 3.5


def test_auto_editor_cut_map_matches_rendered_duration() -> None:
    cut = cut_map_for_rendered_output(10.0, 6.5)
    assert cut.trimmed_duration == 6.5
    assert cut.original_duration == 10.0
    assert abs(sum(span.duration for span in cut.kept_ranges) - 6.5) < 1e-9
    assert cut.kept_ranges[0].end == 6.5
