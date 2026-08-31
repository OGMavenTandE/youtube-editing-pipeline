from pydub import AudioSegment, generators, silence as pydub_silence

from pipeline.config import Settings
from pipeline.models import TimeRange
from pipeline.silence_remover import (
    compute_keep_ranges,
    cut_map_for_rendered_output,
    _drop_isolated_fillers,
    _fold_short_gaps,
    _build_cut_map,
)


def _default_keep(audio: AudioSegment) -> list[TimeRange]:
    settings = Settings()
    return compute_keep_ranges(
        audio,
        min_silence_ms=int(settings.silence_min_duration * 1000),
        padding_ms=int(settings.silence_padding * 1000),
        threshold_db=settings.silence_threshold_db,
    )


def _covers(kept: list[TimeRange], at: float) -> bool:
    return any(span.start <= at < span.end for span in kept)


def _legacy_keep(audio: AudioSegment) -> list[TimeRange]:
    """Old defaults: 700ms RMS window, 150ms pad, -40 dB."""
    raw = pydub_silence.detect_nonsilent(
        audio, min_silence_len=700, silence_thresh=-40.0
    )
    duration_ms = len(audio)
    padded = [
        (max(0, start - 150), min(duration_ms, end + 150)) for start, end in raw
    ]
    return [TimeRange(start=s / 1000.0, end=e / 1000.0) for s, e in padded]


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


def test_settings_silence_defaults() -> None:
    settings = Settings()
    assert settings.silence_min_duration == 1.0
    assert settings.silence_padding == 0.30
    assert settings.silence_threshold_db == -45.0


def _word_after_pause(
    *,
    pause_ms: int,
    onset_ms: int,
    onset_db: float,
    body_ms: int,
    body_db: float = -6.0,
    follow_gap_ms: int = 0,
    follow_ms: int = 0,
) -> tuple[AudioSegment, float]:
    """Breath/pause, then a quiet word onset, then a louder body."""
    audio = AudioSegment.silent(duration=pause_ms)
    audio += generators.Sine(2500).to_audio_segment(duration=onset_ms).apply_gain(onset_db)
    if body_ms:
        audio += generators.Sine(440).to_audio_segment(duration=body_ms).apply_gain(body_db)
    if follow_ms:
        audio += AudioSegment.silent(duration=follow_gap_ms)
        audio += generators.Sine(330).to_audio_segment(duration=follow_ms).apply_gain(-6)
    onset_s = pause_ms / 1000.0
    return audio, onset_s


def test_skynet_onset_after_pause_is_kept() -> None:
    audio, onset_s = _word_after_pause(
        pause_ms=1000, onset_ms=180, onset_db=-38, body_ms=420
    )
    assert not _covers(_legacy_keep(audio), onset_s)
    kept = _default_keep(audio)
    assert _covers(kept, onset_s)
    assert kept[0].start > 0.2
    assert kept[0].start <= onset_s


def test_it_onset_after_pause_is_kept() -> None:
    audio, onset_s = _word_after_pause(
        pause_ms=1000,
        onset_ms=140,
        onset_db=-37,
        body_ms=0,
        follow_gap_ms=70,
        follow_ms=400,
    )
    # Quiet "it" then a short rest then louder speech. Old window+pad misses it.
    assert not _covers(_legacy_keep(audio), onset_s)
    kept = _default_keep(audio)
    assert _covers(kept, onset_s)
    assert kept[0].start <= onset_s


def test_strategic_onset_after_pause_is_kept() -> None:
    audio, onset_s = _word_after_pause(
        pause_ms=1100, onset_ms=220, onset_db=-39, body_ms=500
    )
    assert not _covers(_legacy_keep(audio), onset_s)
    kept = _default_keep(audio)
    assert _covers(kept, onset_s)
    assert kept[0].start <= onset_s


def test_isolated_uh_um_are_removed() -> None:
    speech = generators.Sine(440).to_audio_segment(duration=500).apply_gain(-6)
    dead = AudioSegment.silent(duration=1200)
    uh = generators.Sine(180).to_audio_segment(duration=220).apply_gain(-12)
    um = generators.Sine(160).to_audio_segment(duration=280).apply_gain(-12)
    audio = speech + dead + uh + dead + um + dead + speech
    uh_at = 0.5 + 1.2
    um_at = uh_at + 0.22 + 1.2
    kept = _default_keep(audio)
    assert len(kept) == 2
    assert not _covers(kept, uh_at + 0.05)
    assert not _covers(kept, um_at + 0.05)
    assert _covers(kept, 0.1)
    assert _covers(kept, (len(audio) / 1000.0) - 0.2)


def test_sentence_beat_under_one_second_stays() -> None:
    speech = generators.Sine(440).to_audio_segment(duration=400).apply_gain(-6)
    beat = AudioSegment.silent(duration=800)
    audio = speech + beat + speech
    kept = _default_keep(audio)
    assert len(kept) == 1
    assert kept[0].start == 0.0
    assert kept[0].end >= 1.5


def test_long_dead_air_still_removed() -> None:
    speech = generators.Sine(440).to_audio_segment(duration=400).apply_gain(-6)
    gap = AudioSegment.silent(duration=1600)
    audio = speech + gap + speech
    kept = _default_keep(audio)
    assert len(kept) == 2
    removed = kept[1].start - kept[0].end
    # 1.6s gap minus 0.30s pad on each edge. ~0.6s of rest remains.
    assert 0.95 <= removed <= 1.05


def test_drop_isolated_fillers_keeps_attached_short_word() -> None:
    kept = [
        TimeRange(start=1.0, end=1.14),
        TimeRange(start=1.21, end=1.80),
        TimeRange(start=3.2, end=3.42),
    ]
    folded = _fold_short_gaps(kept, min_silence=1.0, duration=5.0)
    cleaned = _drop_isolated_fillers(
        folded, max_duration=0.35, min_silence=1.0, duration=5.0
    )
    assert len(cleaned) == 1
    assert cleaned[0].start == 1.0
    assert cleaned[0].end == 1.80
