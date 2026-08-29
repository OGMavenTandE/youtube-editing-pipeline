from pipeline.captions import format_srt, format_vtt
from pipeline.models import TimedTranscript, TranscriptCue


def test_srt_and_vtt_from_trimmed_cues() -> None:
    transcript = TimedTranscript(
        duration=4.0,
        full_text="Hello later",
        cues=[
            TranscriptCue(start=0.0, end=1.5, text="Hello"),
            TranscriptCue(start=1.5, end=3.2, text="later"),
        ],
    )
    srt = format_srt(transcript)
    assert "00:00:00,000 --> 00:00:01,500" in srt
    assert "Hello" in srt
    assert "later" in srt
    vtt = format_vtt(transcript)
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in vtt
