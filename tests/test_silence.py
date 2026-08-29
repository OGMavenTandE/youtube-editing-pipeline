from pydub import AudioSegment, generators

from pipeline.models import TimeRange
from pipeline.silence_remover import compute_keep_ranges, _fold_short_gaps


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
